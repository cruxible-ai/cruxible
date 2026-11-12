"""Served Claim prediction authoring and P2-C settlement orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentView,
    ResolutionContractAuthoringPayload,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidence,
    PredictionRefusalCode,
    PredictRequest,
    PredictResult,
    SettleRequest,
    SettleResult,
    TerminalSettlementEvidence,
)
from cruxible_client.contracts.procedures.windows import TriggerEventReference
from cruxible_client.contracts.projection import AcceptedCoordinate as PublicAcceptedCoordinate
from cruxible_client.contracts.repairs import ServedRepair, served_repair_for_refusal
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReference,
    InvestigationBinding,
    ResolutionContract,
    ResolutionContractReference,
    resolution_contract_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.evidence.claim_reobservations import (
    RecordedReobservation,
    ReobservationReader,
)
from cruxible_core.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.exhaust.records import (
    RESOLUTION_JOURNAL_FAMILY,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.procedures.egress import TerminalEgressReceiptV4
from cruxible_core.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionV2,
    ResolutionClaimEndpointV1,
    ResolutionContractActivationV3,
    append_procedure_resolution,
    build_independent_activation,
    build_procedure_resolution_v2,
    build_settled_outcome_relation,
    evaluate_prediction_correctness_condition,
    resolution_book_at_head,
    resolution_contract_partition_id,
)
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.resolution_contracts import (
    artifact_accepted_time,
    bind_window,
    canonical_contract_reference,
    read_claim_reference,
    read_resolution_contract,
    resolve_claim_version,
)
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

_PROCEDURE_JOURNAL = "procedure-runs"
_PROCEDURE_STREAM = "procedures"
_WRITER_TOKEN = "playbill-procedure-direct-run-v1"


class PredictionRefused(FormatError):
    """Closed, repair-carrying refusal for served prediction operations."""

    def __init__(
        self,
        code: PredictionRefusalCode,
        message: str,
        *,
        repair: ServedRepair,
        details: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.error_code = code
        self.repair = repair
        self.details = {} if details is None else details
        super().__init__(f"{code}: {message}")


def _refuse(
    code: PredictionRefusalCode,
    message: str,
    *,
    details: dict[str, str] | None = None,
) -> PredictionRefused:
    return PredictionRefused(code, message, repair=served_repair_for_refusal(code), details=details)


def service_predict_playbill(
    instance: PlaybillInstance,
    *,
    request: PredictRequest,
    actor: AuthenticatedActor,
    evaluation_time: datetime,
) -> PredictResult:
    """Submit a governed test of an already accepted exact hypothesis.

    A hypothesis named by Claim ID is resolved here to the exact version
    accepted at the head, so the authored contract pins one Claim version.
    """
    instance.require_writable()
    contract = ResolutionContract.model_validate(
        {
            **request.contract.model_dump(mode="json"),
            "hypothesis": resolve_claim_version(instance, request.contract.hypothesis).model_dump(
                mode="json"
            ),
        }
    )
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=actor,
        payload=ResolutionContractAuthoringPayload(resolution_contract=contract),
        canonical_timestamp=canonical_candidate_timestamp(ensure_utc(evaluation_time)),
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    if submitted.status.proposal_id is None or submitted.status.candidate_digest is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            "Resolution contract did not produce a valid proposal; repair the authoring "
            "diagnostics.",
        )
    return PredictResult(
        contract_identity=contract.identity.qualified,
        contract_digest=resolution_contract_digest(contract).tagged,
        proposal_id=submitted.status.proposal_id,
        intent=AuthoringIntentView(intent=submitted.intent).model_dump(mode="json"),
    )


def _observation_matches(
    declaration: ResolutionContract,
    claim: ClaimArtifact,
) -> bool:
    statement = claim.statement
    selector = declaration.observation
    return (
        statement.subject == selector.subject
        and statement.predicate == selector.predicate
        and statement.qualifier == selector.qualifier
        and statement.role == selector.role
    )


def _inside_window(activation: ResolutionContractActivationV3, observed_at: datetime) -> bool:
    """An observation counts after contract acceptance and inside its bound window."""

    return (
        observed_at > activation.activated_at
        and activation.check_at <= observed_at <= activation.expires_at
    )


def _window_refusal(
    activation: ResolutionContractActivationV3, observed_at: datetime
) -> PredictionRefused:
    """Why an observation at ``observed_at`` cannot settle: the bound it misses, with both times."""

    def stamp(value: datetime) -> str:
        text = format_datetime(value)
        assert text is not None  # a datetime always formats
        return text

    observed = stamp(observed_at)
    if observed_at <= activation.activated_at:
        accepted = stamp(activation.activated_at)
        return _refuse(
            "prediction_observation_predates_contract",
            f"The observation was made at {observed}, not after the prediction was accepted "
            f"at {accepted}. An unchanged value stated before then needs a re-observation "
            "recorded after the prediction was accepted.",
            details={"observed_at": observed, "contract_accepted_at": accepted},
        )
    if observed_at < activation.check_at:
        opens = stamp(activation.check_at)
        return _refuse(
            "prediction_window_not_open",
            f"The observation was made at {observed}, before the prediction's window "
            f"opens at {opens}.",
            details={"observed_at": observed, "window_opens_at": opens},
        )
    closed = stamp(activation.expires_at)
    return _refuse(
        "prediction_deadline_passed",
        f"The observation was made at {observed}, after the prediction's window closed "
        f"at {closed}.",
        details={"observed_at": observed, "window_closed_at": closed},
    )


def _version_reobservation(
    instance: PlaybillInstance, reference: ClaimVersionReference, *, observed_by: datetime
) -> RecordedReobservation | None:
    """This exact version's latest recorded re-observation observed by ``observed_by``.

    One indexed seek and one journal record read
    (``ReobservationReader.latest_version_record``): records observed later,
    and records of the lineage's other versions, are never visited. The Claim
    blob at the head the record was admitted at is read once more to confirm
    the version: admission records only a live Claim.
    """

    found = ReobservationReader.of(instance).latest_version_record(
        reference.identity.name, reference.artifact_digest, observed_by=observed_by
    )
    if found is None:
        return None
    path = claim_path(reference.identity.name)
    raw = instance.blob_at(found.accepted_coordinate.git_oid, path)
    if raw is None:
        return None
    if claim_artifact_digest(parse_claim(raw, path=path)).tagged != reference.artifact_digest:
        return None
    return found


def _journal(
    instance: PlaybillInstance, *, independent: bool = True
) -> tuple[LocalJournalBackend, JournalStreamIdentityV1]:
    root = instance.root / instance.descriptor.storage.exhaust / _PROCEDURE_JOURNAL
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return LocalJournalBackend(root), JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=RESOLUTION_JOURNAL_FAMILY
        if independent
        else PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id=_PROCEDURE_STREAM,
    )


def _terminal_record(
    instance: PlaybillInstance,
    *,
    evidence: TerminalSettlementEvidence,
    investigation: InvestigationBinding,
) -> StoredProcedureJournalRecordV1:
    from cruxible_core.service.procedures.procedure_runs import _state_from_records

    state = _state_from_records(instance, run_id=evidence.run_id)
    if state.investigation != investigation:
        raise _refuse(
            "settlement_evidence_mismatch",
            "Terminal run investigated a different contract or window.",
        )
    journal, stream = _journal(instance, independent=False)
    # Only the run's own terminal records are candidates: a digest naming any
    # other record (another kind or run) matches none of them and refuses.
    for stored in journal.select_records(
        stream, run_id=evidence.run_id, event_kind="terminal_egress"
    ):
        record = stored.record
        if stored.record_digest != evidence.terminal_record_digest:
            continue
        if record.procedure_artifact_digest != state.procedure_artifact_digest:
            break
        payload = parse_journal_payload(
            instance.body_store().read(
                record.payload_digest,
                access=BodyAccessContext(
                    principal_id="playbill-prediction-settlement",
                    can_read_body=True,
                ),
            )
        )
        if isinstance(payload, dict) and payload.get("verdict") == "delivered":
            receipt_payload = payload.get("receipt")
            if (
                not isinstance(receipt_payload, dict)
                or receipt_payload.get("tag") != "playbill-terminal-egress-receipt-v4"
            ):
                break
            try:
                receipt = TerminalEgressReceiptV4.model_validate(receipt_payload)
            except ValidationError:
                break
            # Only a settle terminal that actually settled counts; its
            # fallback proposal settled nothing.
            if (
                receipt.run_id == evidence.run_id
                and payload.get("node_id") == receipt.node_id
                and payload.get("kind") == receipt.kind
                and receipt.outcome == "settled"
            ):
                return stored
        break
    raise _refuse(
        "settlement_evidence_mismatch",
        "Terminal evidence is not one delivered, settled claims record under the "
        "prediction Procedure mandate.",
    )


def _append_settlement(
    instance: PlaybillInstance,
    *,
    activation: ResolutionContractActivationV3,
    resolution: ProcedureResolutionV2,
) -> ProcedureResolutionV2:
    journal, stream = _journal(instance)
    # Each open lease is checked against the records naming its body only (the
    # payload index), never the whole resolution journal.
    ProcedureMaterialReservationStore(instance.body_store().reservation_root).recover_run_material(
        lambda reservation: journal.select_records(
            stream,
            event_kind=reservation.intended_event_kind,
            payload_digest=reservation.body_digest,
        ),
        bodies=instance.body_store(),
    )
    partition = resolution_contract_partition_id(activation)
    book, _head = resolution_book_at_head(journal, stream, activation, bodies=instance.body_store())
    latest = book.latest_non_overturned(activation.contract_id)
    if latest is not None:
        if isinstance(latest, ProcedureResolutionV2) and all(
            (
                latest.contract_id == resolution.contract_id,
                latest.subject == resolution.subject,
                latest.measurement_name == resolution.measurement_name,
                latest.verdict == resolution.verdict,
                latest.settlement == resolution.settlement,
                latest.settlement_outcome == resolution.settlement_outcome,
                latest.value == resolution.value,
                latest.evidence_refs == resolution.evidence_refs,
                latest.observed_at == resolution.observed_at,
            )
        ):
            return latest
        raise _refuse(
            "settlement_evidence_mismatch",
            "Prediction is already settled by different evidence.",
        )
    state = journal.writer_state(stream, partition)
    if state is not None and state.active and state.fencing_token != _WRITER_TOKEN:
        journal.fence_writer(
            stream,
            partition,
            expected_fencing_token=state.fencing_token,
        )
        state = journal.writer_state(stream, partition)
    if state is None or not state.active:
        journal.activate_writer(
            stream,
            partition,
            fencing_token=_WRITER_TOKEN,
            expected_head=journal.read_head(stream, partition),
        )
    writer = ProcedureExhaustWriter(
        journal=journal,
        bodies=instance.body_store(),
        fencing_token=_WRITER_TOKEN,
    )
    try:
        activation_records = journal.select_records(
            stream, partition_id=partition, event_kind="resolution_activation"
        )
        if not activation_records:
            writer.append(
                stream=stream,
                partition_id=partition,
                event_kind="resolution_activation",
                accepted_coordinate=activation.investigation.contract.coordinate,
                procedure_artifact_digest=activation.procedure_artifact_digest,
                definition_digest=activation.definition_digest,
                actor_context=resolution.actor_context,
                recorded_at=resolution.recorded_at,
                payload=activation.model_dump(mode="json"),
            )
        else:
            payloads = tuple(
                parse_journal_payload(
                    instance.body_store().read(
                        stored.record.payload_digest,
                        access=BodyAccessContext(
                            principal_id="playbill-prediction-settlement",
                            can_read_body=True,
                        ),
                    )
                )
                for stored in activation_records
            )
            if payloads != (activation.model_dump(mode="json"),):
                raise FormatError("prediction activation journal history diverged")
        append_procedure_resolution(
            writer,
            activation=activation,
            resolution=resolution,
            stream=stream,
        )
    finally:
        journal.fence_writer(
            stream,
            partition,
            expected_fencing_token=_WRITER_TOKEN,
        )
    return resolution


def _live_contract_digest(instance: PlaybillInstance, name: str) -> str | None:
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        row = projection.typed.connection.execute(
            "SELECT artifact_digest FROM resolution_contracts "
            "WHERE identity=? AND lifecycle='live'",
            (f"ResolutionContract:{name}",),
        ).fetchone()
    return None if row is None else str(row[0])


def _settlement_route(
    instance: PlaybillInstance,
    *,
    prediction_id: str,
    request: SettleRequest,
) -> tuple[ResolutionContractReference, TriggerEventReference | None]:
    """The exact contract (and anchor) a settle route names, unless given outright.

    A bound window id (RSC-...) is held by the prediction worker with its exact
    contract reference and anchor event; a contract name resolves to the live
    accepted version at the head.
    """

    if request.contract is not None:
        return request.contract, request.trigger_event
    if prediction_id.startswith("RSC-"):
        from cruxible_core.consumers.next.predictions import bound_window

        held = bound_window(instance, prediction_id)
        if held is None:
            raise _refuse(
                "prediction_window_unknown",
                f"No bound prediction window {prediction_id} is held by the prediction worker; "
                "`cruxible next` lists settleable windows.",
            )
        # The worker's findings may trail accepted state: the held version must
        # still be the live one at the head.
        if _live_contract_digest(instance, held.contract.identity.name) != (
            held.contract.artifact_digest
        ):
            raise _refuse(
                "prediction_window_unknown",
                f"Bound prediction window {prediction_id} belongs to a contract version "
                "that is no longer live at the accepted head.",
            )
        return held.contract, request.trigger_event or held.window.event
    name = prediction_id.removeprefix("ResolutionContract:")
    digest = _live_contract_digest(instance, name)
    if digest is None:
        raise _refuse(
            "prediction_window_unknown",
            f"No live accepted ResolutionContract is named {name!r}; name the prediction by "
            "its contract name or by the RSC-... window id `cruxible next` shows.",
        )
    return (
        ResolutionContractReference(
            identity=ArtifactIdentity(kind="ResolutionContract", name=name),
            artifact_digest=digest,
            coordinate=PublicAcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
        request.trigger_event,
    )


def service_settle_playbill_prediction(
    instance: PlaybillInstance,
    *,
    prediction_id: str,
    request: SettleRequest,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> SettleResult:
    """Settle one accepted predicted Claim from a later accepted outcome.

    The route names the contract (its name or qualified identity) or one bound
    window of it (the RSC-... id `next` names); a bound window id must be the
    one this request's contract and window rebuild. The observation is a Claim
    ID the daemon resolves to its accepted version.
    """

    instance.require_writable()
    reference, trigger_event = _settlement_route(
        instance, prediction_id=prediction_id, request=request
    )
    evidence = request.evidence or ObservationSettlementEvidence(
        claim=cast(str, request.observation)
    )
    evidence = evidence.model_copy(
        update={"claim": resolve_claim_version(instance, evidence.claim)}
    )
    names_bound_window = prediction_id not in {
        reference.identity.name,
        reference.identity.qualified,
    }
    contract = read_resolution_contract(instance, reference)
    reference = canonical_contract_reference(instance, reference)
    if contract.lifecycle.state != "live":
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement must reference the original live contract version, not its retirement.",
        )
    investigation = InvestigationBinding(
        contract=reference,
        hypothesis=contract.hypothesis,
        window=bind_window(instance, contract.window, trigger_event, now=ensure_utc(recorded_at)),
    )
    activation = build_independent_activation(
        contract,
        investigation,
        activated_at=artifact_accepted_time(instance, reference),
    )
    if names_bound_window and activation.contract_id != prediction_id:
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement route differs from its exact contract reference and bound window.",
        )
    prediction_claim = read_claim_reference(instance, contract.hypothesis)
    observation_reference = cast(ClaimVersionReference, evidence.claim)
    observation = read_claim_reference(instance, observation_reference)
    observation_coordinate = observation_reference.coordinate
    if not _observation_matches(contract, observation):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Observation does not match the accepted contract selector.",
        )
    observed_at = artifact_accepted_time(instance, observation_reference)
    reobservation: RecordedReobservation | None = None
    if not _inside_window(activation, observed_at):
        # The version itself was accepted outside the window. A recorded
        # re-observation of this exact version counts as observing it at the
        # re-observing Capture's time; nothing about the Claim changes.
        # The one that counts is the latest observed by the window's close (or
        # by now): a later record past the close never hides an earlier one.
        settled_at = ensure_utc(recorded_at)
        reobservation = _version_reobservation(
            instance,
            observation_reference,
            observed_by=min(activation.expires_at, settled_at),
        )
        if reobservation is not None and _inside_window(
            activation, reobservation.record.observed_at
        ):
            observed_at = reobservation.record.observed_at
        else:
            # Refused, and judged by the latest observation of this version
            # there is: the one before the window opened, or one after it closed.
            latest = reobservation or _version_reobservation(
                instance, observation_reference, observed_by=settled_at
            )
            raise _window_refusal(
                activation,
                observed_at if latest is None else max(observed_at, latest.record.observed_at),
            )
    prediction_object = prediction_claim.statement.object
    observation_object = observation.statement.object
    if not isinstance(prediction_object, LiteralClaimObject) or not isinstance(
        observation_object, LiteralClaimObject
    ):
        raise _refuse(
            "prediction_unsettleable_rule",
            "Served prediction settlement requires canonical literal Claim objects.",
        )
    predicted_value = prediction_object.value
    settlement_value = observation_object.value
    # Presence is read off the settling observation, not assumed. An accepted
    # observation whose object is null is an explicit record of absence, so a
    # prediction of `False` -- "no such value will be observed" -- settles
    # correct when one lands, and incorrect when a real value does. Hardcoding
    # presence made every `False` presence prediction settle incorrect and
    # biased every calibration row derived from one.
    evidence_present = settlement_value is not None
    outcome = evaluate_prediction_correctness_condition(
        activation.correctness_condition,
        prediction_value=predicted_value,
        settlement_value=settlement_value,
        evidence_present=evidence_present,
    )
    if outcome is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            "Prediction rule cannot evaluate the accepted settlement value.",
        )
    evidence_kind: Literal["observation_claim", "terminal"] = "observation_claim"
    proof_kind: Literal["claim_statement", "run_receipt"] = "claim_statement"
    proof_digest = claim_statement_digest(observation.statement).tagged
    proof_subject: SemanticAddress | None = claim_statement_address(
        claim_path(observation.identity.name)
    )
    authorization_source: CanonicalValue = {
        "tag": "playbill-prediction-settlement-authorization-v1",
        "kind": "observation_admission",
    }
    if isinstance(evidence, TerminalSettlementEvidence):
        terminal = _terminal_record(
            instance,
            evidence=evidence,
            investigation=investigation,
        )
        # The terminal's mandate is the AUTHORITY; the caller is the ACTOR. A
        # settlement journaled under the mandate holder's actor context would
        # let anyone who can name a delivered record mint a settlement
        # attributed to someone else, so the caller must hold that mandate and
        # the record it authorizes is recorded under the caller.
        mandate_actor = terminal.record.actor_context
        if mandate_actor.actor_id != actor_context.actor_id:
            raise _refuse(
                "settlement_evidence_mismatch",
                "Terminal settlement requires the principal the mandate settlement ran under.",
            )
        evidence_kind = "terminal"
        proof_kind = "run_receipt"
        proof_digest = terminal.record_digest
        proof_subject = None
        authorization_source = {
            "tag": "playbill-prediction-settlement-authorization-v1",
            "kind": "terminal_mandate",
            "terminal_record_digest": terminal.record_digest,
            "mandate_actor_id": mandate_actor.actor_id,
        }
    elif not isinstance(evidence, ObservationSettlementEvidence):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement evidence kind is unsupported.",
        )
    evidence_refs = [
        ProcedureProofReferenceV1(kind=proof_kind, digest=proof_digest, subject=proof_subject)
    ]
    if reobservation is not None:
        # The re-observing Capture and the journal record that admitted it are
        # the proof the value was observed inside the window.
        evidence_refs += [
            ProcedureProofReferenceV1(kind="capture", digest=reobservation.record.capture_digest),
            ProcedureProofReferenceV1(kind="journal_record", digest=reobservation.record_digest),
        ]
        reobserved: dict[str, CanonicalValue] = {
            "reobserving_capture_digest": reobservation.record.capture_digest,
            "reobservation_record_digest": reobservation.record_digest,
        }
        authorization_source = (
            {
                "tag": "playbill-prediction-settlement-authorization-v1",
                "kind": "reobservation_admission",
                **reobserved,
            }
            if evidence_kind == "observation_claim"
            else {**cast(dict[str, CanonicalValue], authorization_source), **reobserved}
        )
    settlement_endpoint = ResolutionClaimEndpointV1(
        statement_address=claim_statement_address(claim_path(observation.identity.name)),
        content_digest=claim_statement_digest(observation.statement).tagged,
        accepted_coordinate=observation_coordinate,
    )
    resolution = build_procedure_resolution_v2(
        activation,
        sequence=1,
        verdict="satisfied",
        settlement=settlement_endpoint,
        settlement_outcome=outcome,
        value={
            "tag": "playbill-prediction-settlement-value-v1",
            "evidence_kind": evidence_kind,
            "evidence_present": evidence_present,
            "prediction_value": predicted_value,
            "settlement_value": settlement_value,
            "authorization_source": authorization_source,
        },
        evidence_refs=tuple(
            sorted(evidence_refs, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        ),
        observed_at=observed_at,
        recorded_at=ensure_utc(recorded_at),
        actor_context=actor_context,
    )
    resolution = _append_settlement(
        instance,
        activation=activation,
        resolution=resolution,
    )
    relation = build_settled_outcome_relation(activation, resolution)
    return SettleResult(
        prediction_id=prediction_id,
        activation=activation.model_dump(mode="json"),
        resolution=resolution.model_dump(mode="json"),
        relation=relation.model_dump(mode="json"),
    )
