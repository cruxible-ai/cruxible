"""Served Claim prediction authoring and P2-C settlement orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentViewV1,
    ResolutionContractAuthoringPayloadV1,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import CanonicalValue
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    LiteralClaimObject,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV2,
    PlaybillPredictRequestV2,
    PlaybillPredictResultV2,
    PlaybillSettleRequestV2,
    PlaybillSettleResultV2,
    PredictionRefusalCodeV1,
    TerminalSettlementEvidenceV2,
)
from cruxible_client.contracts.repairs import ServedRepairV1, served_repair_for_refusal
from cruxible_client.contracts.resolution_contracts import (
    InvestigationBindingV1,
    ResolutionContractV1,
    resolution_contract_digest,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
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
from cruxible_core.procedures.egress import (
    TerminalEgressReceiptV1,
    TerminalEgressReceiptV2,
    TerminalEgressReceiptV4,
)
from cruxible_core.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    ProcedureResolutionV2,
    ResolutionClaimEndpointV1,
    ResolutionContractActivationV2,
    ResolutionContractActivationV3,
    append_procedure_resolution,
    build_independent_activation,
    build_procedure_resolution_v2,
    build_settled_outcome_relation,
    evaluate_prediction_correctness_condition,
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
)
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

_PROCEDURE_JOURNAL = "procedure-runs"
_PROCEDURE_STREAM = "procedures"
_WRITER_TOKEN = "playbill-procedure-direct-run-v1"


class PredictionRefused(PlaybillFormatError):
    """Closed, repair-carrying refusal for served prediction operations."""

    def __init__(
        self,
        code: PredictionRefusalCodeV1,
        message: str,
        *,
        repair: ServedRepairV1,
    ) -> None:
        self.code = code
        self.error_code = code
        self.repair = repair
        super().__init__(f"{code}: {message}")


def _refuse(
    code: PredictionRefusalCodeV1,
    message: str,
) -> PredictionRefused:
    return PredictionRefused(code, message, repair=served_repair_for_refusal(code))


def service_predict_playbill(
    instance: PlaybillInstance,
    *,
    request: PlaybillPredictRequestV2,
    actor: AuthenticatedActor,
    evaluation_time: datetime,
) -> PlaybillPredictResultV2:
    """Submit a governed test of an already accepted exact hypothesis."""
    instance.require_writable()
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=actor,
        payload=ResolutionContractAuthoringPayloadV1(resolution_contract=request.contract),
        canonical_timestamp=canonical_candidate_timestamp(ensure_utc(evaluation_time)),
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    if submitted.status.proposal_id is None or submitted.status.candidate_digest is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            "Resolution contract did not produce a valid proposal; repair the authoring "
            "diagnostics.",
        )
    return PlaybillPredictResultV2(
        contract_identity=request.contract.identity.qualified,
        contract_digest=resolution_contract_digest(request.contract).tagged,
        proposal_id=submitted.status.proposal_id,
        intent=AuthoringIntentViewV1(intent=submitted.intent).model_dump(mode="json"),
    )


def _observation_matches(
    declaration: ResolutionContractV1,
    claim: ClaimArtifactAny,
) -> bool:
    statement = claim.statement
    selector = declaration.observation
    return (
        statement.subject == selector.subject
        and statement.predicate == selector.predicate
        and statement.qualifier == selector.qualifier
        and statement.role == selector.role
    )


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
    evidence: TerminalSettlementEvidenceV2,
    investigation: InvestigationBindingV1,
) -> StoredProcedureJournalRecordV1:
    from cruxible_core.service.procedures.procedure_runs import _state_from_records

    state = _state_from_records(instance, run_id=evidence.run_id)
    if state.investigation != investigation:
        raise _refuse(
            "settlement_evidence_mismatch",
            "Terminal run investigated a different contract or window.",
        )
    journal, stream = _journal(instance, independent=False)
    for partition in journal.partition_ids(stream):
        for stored in journal.all_records(stream, partition):
            record = stored.record
            if stored.record_digest != evidence.terminal_record_digest:
                continue
            if (
                record.event_kind != "terminal_egress"
                or record.run_id != evidence.run_id
                or record.procedure_artifact_digest != state.procedure_artifact_digest
            ):
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
                if not isinstance(receipt_payload, dict):
                    break
                receipt_types: dict[str, type[TerminalEgressReceiptV1]] = {
                    "playbill-terminal-egress-receipt-v4": TerminalEgressReceiptV4,
                    "playbill-terminal-egress-receipt-v2": TerminalEgressReceiptV2,
                }
                receipt_type = receipt_types.get(
                    str(receipt_payload.get("tag")), TerminalEgressReceiptV1
                )
                try:
                    receipt = receipt_type.model_validate(receipt_payload)
                except ValidationError:
                    break
                # A settle terminal counts only when it actually settled; its
                # fallback proposal settled nothing. The scaffolded kind remains
                # readable for retained evidence.
                settled = (
                    isinstance(receipt, TerminalEgressReceiptV4) and receipt.outcome == "settled"
                ) or receipt.kind == "mandate_settlement"
                if (
                    receipt.run_id == evidence.run_id
                    and payload.get("node_id") == receipt.node_id
                    and payload.get("kind") == receipt.kind
                    and settled
                ):
                    return stored
            break
    raise _refuse(
        "settlement_evidence_mismatch",
        "Terminal evidence is not one delivered, settled settle_change_set record under the "
        "prediction Procedure mandate.",
    )


def _partition_records(
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    activation: ResolutionContractActivationV3,
) -> tuple[StoredProcedureJournalRecordV1, ...]:
    return journal.all_records(stream, resolution_contract_partition_id(activation))


def _append_settlement(
    instance: PlaybillInstance,
    *,
    activation: ResolutionContractActivationV3,
    resolution: ProcedureResolutionV2,
) -> ProcedureResolutionV2:
    journal, stream = _journal(instance)
    all_records = tuple(
        stored
        for partition in journal.partition_ids(stream)
        for stored in journal.all_records(stream, partition)
    )
    ProcedureMaterialReservationStore(instance.body_store().reservation_root).recover_run_material(
        all_records,
        bodies=instance.body_store(),
    )
    partition = resolution_contract_partition_id(activation)
    existing = _partition_records(journal, stream, activation)
    book = ProcedureResolutionBook((activation,))
    book.replay(existing, bodies=instance.body_store())
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
        activation_records = tuple(
            stored for stored in existing if stored.record.event_kind == "resolution_activation"
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
                raise PlaybillFormatError("prediction activation journal history diverged")
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


def service_settle_playbill_prediction(
    instance: PlaybillInstance,
    *,
    prediction_id: str,
    request: PlaybillSettleRequestV2,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> PlaybillSettleResultV2:
    """Settle one accepted predicted Claim from a later accepted outcome."""

    instance.require_writable()
    reference = request.contract
    if prediction_id not in {reference.identity.name, reference.identity.qualified}:
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement route differs from its exact contract reference.",
        )
    contract = read_resolution_contract(instance, reference)
    reference = canonical_contract_reference(instance, reference)
    if contract.lifecycle.state != "live":
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement must reference the original live contract version, not its retirement.",
        )
    investigation = InvestigationBindingV1(
        contract=reference,
        hypothesis=contract.hypothesis,
        window=bind_window(
            instance, contract.window, request.trigger_event, now=ensure_utc(recorded_at)
        ),
    )
    activation = build_independent_activation(
        contract,
        investigation,
        activated_at=artifact_accepted_time(instance, reference),
    )
    prediction_claim = read_claim_reference(instance, contract.hypothesis)
    observation = read_claim_reference(instance, request.evidence.claim)
    observation_coordinate = request.evidence.claim.coordinate
    if not _observation_matches(contract, observation):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Observation does not match the accepted contract selector.",
        )
    observed_at = artifact_accepted_time(instance, request.evidence.claim)
    if (
        observed_at <= activation.activated_at
        or not activation.check_at <= observed_at <= activation.expires_at
    ):
        raise _refuse(
            "prediction_deadline_passed",
            "Observation must follow contract acceptance and fall inside its bound window.",
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
    if isinstance(request.evidence, TerminalSettlementEvidenceV2):
        terminal = _terminal_record(
            instance,
            evidence=request.evidence,
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
    elif not isinstance(request.evidence, ObservationSettlementEvidenceV2):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement evidence kind is unsupported.",
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
        evidence_refs=(
            ProcedureProofReferenceV1(
                kind=proof_kind,
                digest=proof_digest,
                subject=proof_subject,
            ),
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
    return PlaybillSettleResultV2(
        prediction_id=prediction_id,
        activation=activation.model_dump(mode="json"),
        resolution=resolution.model_dump(mode="json"),
        relation=relation.model_dump(mode="json"),
    )


def load_prediction_activations(
    instance: PlaybillInstance,
) -> tuple[ResolutionContractActivationV2 | ResolutionContractActivationV3, ...]:
    """Read retained activations, preserving each generation's verification law."""
    activations: dict[str, ResolutionContractActivationV2 | ResolutionContractActivationV3] = {}
    for independent in (False, True):
        journal, stream = _journal(instance, independent=independent)
        model = ResolutionContractActivationV3 if independent else ResolutionContractActivationV2
        for partition in journal.partition_ids(stream):
            for stored in journal.all_records(stream, partition):
                if stored.record.event_kind != "resolution_activation":
                    continue
                payload = parse_journal_payload(
                    instance.body_store().read(
                        stored.record.payload_digest,
                        access=BodyAccessContext(
                            principal_id="prediction-replay", can_read_body=True
                        ),
                    )
                )
                activation = model.model_validate(payload)
                if partition != resolution_contract_partition_id(activation):
                    raise PlaybillFormatError("prediction activation crossed its journal partition")
                if isinstance(activation, ResolutionContractActivationV3):
                    retained = read_resolution_contract(instance, activation.investigation.contract)
                    if (
                        canonical_contract_reference(instance, activation.investigation.contract)
                        != activation.investigation.contract
                        or artifact_accepted_time(instance, activation.investigation.contract)
                        != activation.activated_at
                        or retained != activation.contract
                        or bind_window(
                            instance,
                            retained.window,
                            activation.investigation.window.event,
                            now=stored.record.recorded_at,
                        )
                        != activation.investigation.window
                    ):
                        raise PlaybillFormatError(
                            "resolution activation differs from its retained authority"
                        )
                previous = activations.setdefault(activation.contract_id, activation)
                if previous != activation:
                    raise PlaybillFormatError("prediction activation history diverged")
    return tuple(activations[key] for key in sorted(activations))
