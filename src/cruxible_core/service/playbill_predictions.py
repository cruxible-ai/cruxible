"""Served Claim prediction authoring and P2-C settlement orchestration."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from cruxible_client.contracts.authoring.models import AuthoringIntentViewV1
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    LiteralClaimObject,
    claim_path,
    claim_statement_address,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV1,
    PlaybillPredictionDeclarationV1,
    PlaybillPredictRequestV1,
    PlaybillPredictResultV1,
    PlaybillSettleRequestV1,
    PlaybillSettleResultV1,
    PredictionPresenceRuleV1,
    PredictionRefusalCodeV1,
    PredictionThresholdRuleV1,
    TerminalSettlementEvidenceV1,
    build_prediction_declaration,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.exhaust.records import (
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.material_reservations import ProcedureMaterialReservationStore
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressReceiptV1,
    TerminalEgressReceiptV2,
)
from cruxible_core.playbill.procedures.resolution import (
    ProcedureProofReferenceV1,
    ProcedureResolutionBook,
    ProcedureResolutionV2,
    ResolutionClaimEndpointV1,
    ResolutionContractActivationV1,
    ResolutionContractActivationV2,
    append_procedure_resolution,
    build_procedure_resolution_v2,
    build_resolution_contract_activation_v2,
    build_settled_outcome_relation,
    derive_resolution_activations,
    evaluate_prediction_correctness_condition,
    resolution_activation_id,
    resolution_contract_id,
    resolution_contract_partition_id,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor

_PREDICTION_STORE = "predictions"
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
        repair: RepairOperationV1,
    ) -> None:
        self.code = code
        self.error_code = code
        self.repair = repair
        super().__init__(f"{code}: {message}")


def _refuse(
    code: PredictionRefusalCodeV1,
    message: str,
    *,
    prediction_id: str | None = None,
) -> PredictionRefused:
    if code == "prediction_unsettleable_rule":
        repair = RepairOperationV1(
            operation="playbill.predict",
            arguments={"rule": "equality"},
        )
    elif code == "prediction_deadline_passed":
        repair = RepairOperationV1(
            operation="playbill.predict",
            arguments={"replace_prediction": prediction_id or "current"},
        )
    else:
        repair = RepairOperationV1(
            operation="playbill.settle",
            arguments={"prediction_id": prediction_id or "required"},
        )
    return PredictionRefused(code, message, repair=repair)


def _prediction_root(instance: PlaybillInstance) -> Path:
    root = instance.root / instance.descriptor.storage.exhaust / _PREDICTION_STORE
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise PlaybillFormatError("prediction declaration store is invalid")
    os.chmod(root, 0o700)
    return root


def _render_declaration(value: PlaybillPredictionDeclarationV1) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def _store_declaration(
    instance: PlaybillInstance,
    value: PlaybillPredictionDeclarationV1,
) -> None:
    root = _prediction_root(instance)
    target = root / f"{value.prediction_id}.json"
    content = _render_declaration(value)
    temporary = root / f".creating-{value.prediction_id}-{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS contract
                raise PlaybillFormatError("prediction declaration write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != content:
            raise PlaybillFormatError("prediction declaration identity is occupied") from None
    finally:
        temporary.unlink(missing_ok=True)


def _load_declaration(
    instance: PlaybillInstance,
    prediction_id: str,
) -> PlaybillPredictionDeclarationV1:
    path = _prediction_root(instance) / f"{prediction_id}.json"
    if path.is_symlink() or not path.is_file():
        raise _refuse(
            "settlement_evidence_mismatch",
            "The named prediction declaration does not exist.",
            prediction_id=prediction_id,
        )
    try:
        raw = path.read_bytes()
        value = PlaybillPredictionDeclarationV1.model_validate(json.loads(raw))
    except (OSError, ValueError, ValidationError) as exc:
        raise PlaybillFormatError("prediction declaration is malformed") from exc
    if raw != _render_declaration(value) or value.prediction_id != prediction_id:
        raise PlaybillFormatError("prediction declaration does not reproduce")
    return value


def _accepted_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    coordinate: AcceptedCoordinate,
) -> AcceptedProcedureV1:
    path = procedure_path(name)
    raw = instance.tree_at(coordinate.git_oid).get(path)
    if raw is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            f"Procedure:{name} is absent at the prediction coordinate.",
        )
    procedure = parse_procedure(raw, path=path)
    return AcceptedProcedureV1(
        path=path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _assert_rule_can_settle(request: PlaybillPredictRequestV1) -> None:
    prediction_object = request.prediction.statement.object
    if not isinstance(prediction_object, LiteralClaimObject):
        raise _refuse(
            "prediction_unsettleable_rule",
            "Served prediction rules require a canonical literal Claim object.",
        )
    predicted = prediction_object.value
    if isinstance(request.rule, PredictionThresholdRuleV1) and not isinstance(predicted, bool):
        raise _refuse(
            "prediction_unsettleable_rule",
            "A threshold prediction must predict a boolean result.",
        )
    if isinstance(request.rule, PredictionPresenceRuleV1) and not isinstance(predicted, bool):
        raise _refuse(
            "prediction_unsettleable_rule",
            "A presence prediction must predict a boolean result.",
        )


def service_predict_playbill(
    instance: PlaybillInstance,
    *,
    request: PlaybillPredictRequestV1,
    actor: AuthenticatedActor,
    evaluation_time: datetime,
) -> PlaybillPredictResultV1:
    """Create and submit the predicted Claim under ordinary Claim authority."""

    declared_at = ensure_utc(evaluation_time)
    if request.deadline <= declared_at:
        raise _refuse(
            "prediction_deadline_passed",
            "Prediction deadline must follow its declaration instant.",
        )
    _assert_rule_can_settle(request)
    base = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    procedure = _accepted_procedure(instance, name=request.procedure, coordinate=base)
    if request.measurement_name not in {
        item.name for item in procedure.procedure.definition.measurements
    }:
        raise _refuse(
            "prediction_unsettleable_rule",
            f"Procedure:{request.procedure} has no measurement {request.measurement_name!r}.",
        )

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    created = coordinator.create(
        actor=actor,
        payload=request.prediction,
        canonical_timestamp=canonical_candidate_timestamp(declared_at),
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    proposal_id = submitted.status.proposal_id
    candidate_digest = submitted.status.candidate_digest
    if proposal_id is None or candidate_digest is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            "The predicted Claim did not produce a proposal; repair its authoring refusal.",
        )
    declaration = build_prediction_declaration(
        intent_id=submitted.intent.intent_id,
        proposal_id=proposal_id,
        candidate_digest=candidate_digest,
        predicted_claim_id=submitted.intent.semantic_identity,
        actor_id=actor.actor_id,
        base_coordinate=base,
        procedure_identity=procedure.procedure.identity,
        procedure_path=procedure.path,
        procedure_artifact_digest=procedure.artifact_digest,
        measurement_name=request.measurement_name,
        observation=request.observation,
        rule=request.rule,
        outcome_class=request.outcome_class,
        declared_at=declared_at,
        deadline=request.deadline,
    )
    _store_declaration(instance, declaration)
    return PlaybillPredictResultV1(
        declaration=declaration,
        intent=AuthoringIntentViewV1(intent=submitted.intent).model_dump(mode="json"),
    )


def _accepted_claim_revision(
    instance: PlaybillInstance,
    *,
    claim_id: str,
) -> tuple[ClaimArtifactAny, AcceptedCoordinate, int]:
    path = claim_path(claim_id.removeprefix("Claim:"))
    current_raw = instance.tree_at(instance.accepted_coordinate().git_oid).get(path)
    if current_raw is None:
        raise ValueError("Claim is not accepted")
    claim = parse_claim(current_raw, path=path)
    for generation in instance.accepted_history():
        if instance.tree_at(generation.oid).get(path) != current_raw:
            continue
        return (
            claim,
            AcceptedCoordinate.from_internal(instance.coordinate_for_oid(generation.oid)),
            generation.sequence,
        )
    raise PlaybillFormatError("accepted Claim revision has no accepting generation")


def _activation_for_declaration(
    instance: PlaybillInstance,
    *,
    declaration: PlaybillPredictionDeclarationV1,
    prediction_claim: ClaimArtifactAny,
    prediction_coordinate: AcceptedCoordinate,
) -> ResolutionContractActivationV2:
    raw = instance.tree_at(declaration.base_coordinate.git_oid).get(declaration.procedure_path)
    if raw is None:
        raise PlaybillFormatError("prediction Procedure disappeared from retained history")
    procedure = parse_procedure(raw, path=declaration.procedure_path)
    if (
        procedure.identity != declaration.procedure_identity
        or procedure_artifact_digest(procedure).tagged != declaration.procedure_artifact_digest
    ):
        raise PlaybillFormatError("prediction Procedure pin does not reproduce")
    accepted = AcceptedProcedureV1(
        path=declaration.procedure_path,
        procedure=procedure,
        artifact_digest=declaration.procedure_artifact_digest,
    )
    activated_at = instance.accepted_evaluation_time(prediction_coordinate.git_oid)
    base = next(
        (
            item
            for item in derive_resolution_activations(
                accepted,
                accepted_coordinate=prediction_coordinate,
                activated_at=activated_at,
            )
            if item.measurement_name == declaration.measurement_name
        ),
        None,
    )
    if base is None:
        raise PlaybillFormatError("prediction measurement disappeared from its pinned Procedure")
    provisional = base.model_copy(
        update={
            "contract_id": "",
            "activation_id": "",
            "check_at": activated_at,
            "expires_at": declaration.deadline,
        }
    )
    contract = resolution_contract_id(provisional)
    with_contract = provisional.model_copy(update={"contract_id": contract})
    bounded = ResolutionContractActivationV1.model_validate(
        with_contract.model_copy(
            update={"activation_id": resolution_activation_id(with_contract)}
        ).model_dump(mode="python")
    )
    return build_resolution_contract_activation_v2(
        bounded,
        prediction=ResolutionClaimEndpointV1(
            statement_address=claim_statement_address(claim_path(prediction_claim.identity.name)),
            content_digest=claim_statement_digest(prediction_claim.statement).tagged,
            accepted_coordinate=prediction_coordinate,
        ),
        outcome_class=declaration.outcome_class,
        correctness_condition=declaration.rule.model_dump(mode="json"),
    )


def _observation_matches(
    declaration: PlaybillPredictionDeclarationV1,
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


def _journal(instance: PlaybillInstance) -> tuple[LocalJournalBackend, JournalStreamIdentityV1]:
    root = instance.root / instance.descriptor.storage.exhaust / _PROCEDURE_JOURNAL
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return LocalJournalBackend(root), JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id=_PROCEDURE_STREAM,
    )


def _terminal_record(
    instance: PlaybillInstance,
    *,
    evidence: TerminalSettlementEvidenceV1,
    procedure_digest: str,
) -> StoredProcedureJournalRecordV1:
    journal, stream = _journal(instance)
    for partition in journal.partition_ids(stream):
        for stored in journal.all_records(stream, partition):
            record = stored.record
            if stored.record_digest != evidence.terminal_record_digest:
                continue
            if (
                record.event_kind != "terminal_egress"
                or record.run_id != evidence.run_id
                or record.procedure_artifact_digest != procedure_digest
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
                receipt_type = (
                    TerminalEgressReceiptV2
                    if receipt_payload.get("tag") == "playbill-terminal-egress-receipt-v2"
                    else TerminalEgressReceiptV1
                )
                try:
                    receipt = receipt_type.model_validate(receipt_payload)
                except ValidationError:
                    break
                if (
                    receipt.run_id == evidence.run_id
                    and payload.get("node_id") == receipt.node_id
                    and payload.get("kind") == receipt.kind
                    and receipt.kind == "mandate_settlement"
                ):
                    return stored
            break
    raise _refuse(
        "settlement_evidence_mismatch",
        "Terminal evidence is not one delivered mandate-settlement record under the "
        "prediction Procedure mandate.",
    )


def _partition_records(
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    activation: ResolutionContractActivationV2,
) -> tuple[StoredProcedureJournalRecordV1, ...]:
    return journal.all_records(stream, resolution_contract_partition_id(activation))


def _append_settlement(
    instance: PlaybillInstance,
    *,
    activation: ResolutionContractActivationV2,
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
                accepted_coordinate=activation.prediction.accepted_coordinate,
                procedure_artifact_digest=activation.procedure_artifact_digest,
                definition_digest=activation.definition_digest,
                actor_context=resolution.actor_context,
                recorded_at=activation.activated_at,
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
    request: PlaybillSettleRequestV1,
    actor_context: GovernedActorContext,
    recorded_at: datetime,
) -> PlaybillSettleResultV1:
    """Settle one accepted predicted Claim from a later accepted outcome."""

    declaration = _load_declaration(instance, prediction_id)
    try:
        prediction_claim, prediction_coordinate, prediction_sequence = _accepted_claim_revision(
            instance,
            claim_id=declaration.predicted_claim_id,
        )
    except (PlaybillError, ValueError) as exc:
        raise _refuse(
            "settlement_evidence_mismatch",
            "The predicted Claim has not been accepted at an exact coordinate.",
            prediction_id=prediction_id,
        ) from exc
    activation = _activation_for_declaration(
        instance,
        declaration=declaration,
        prediction_claim=prediction_claim,
        prediction_coordinate=prediction_coordinate,
    )
    try:
        observation, observation_coordinate, observation_sequence = _accepted_claim_revision(
            instance,
            claim_id=request.evidence.claim_id,
        )
    except (PlaybillError, ValueError) as exc:
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement Claim has not been accepted at an exact coordinate.",
            prediction_id=prediction_id,
        ) from exc
    if observation_sequence <= prediction_sequence or not _observation_matches(
        declaration, observation
    ):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement requires a matching observation Claim accepted after the prediction.",
            prediction_id=prediction_id,
        )
    observed_at = instance.accepted_evaluation_time(observation_coordinate.git_oid)
    if observed_at > declaration.deadline:
        raise _refuse(
            "prediction_deadline_passed",
            "Settlement observation was accepted after the prediction deadline.",
            prediction_id=prediction_id,
        )
    prediction_object = prediction_claim.statement.object
    observation_object = observation.statement.object
    if not isinstance(prediction_object, LiteralClaimObject) or not isinstance(
        observation_object, LiteralClaimObject
    ):
        raise _refuse(
            "prediction_unsettleable_rule",
            "Served prediction settlement requires canonical literal Claim objects.",
            prediction_id=prediction_id,
        )
    predicted_value = prediction_object.value
    settlement_value = observation_object.value
    outcome = evaluate_prediction_correctness_condition(
        activation.correctness_condition,
        prediction_value=predicted_value,
        settlement_value=settlement_value,
        evidence_present=True,
    )
    if outcome is None:
        raise _refuse(
            "prediction_unsettleable_rule",
            "Prediction rule cannot evaluate the accepted settlement value.",
            prediction_id=prediction_id,
        )
    evidence_kind: Literal["observation_claim", "terminal"] = "observation_claim"
    proof_kind: Literal["claim_statement", "run_receipt"] = "claim_statement"
    proof_digest = claim_statement_digest(observation.statement).tagged
    proof_subject: SemanticAddress | None = claim_statement_address(
        claim_path(observation.identity.name)
    )
    settlement_actor = actor_context
    if isinstance(request.evidence, TerminalSettlementEvidenceV1):
        terminal = _terminal_record(
            instance,
            evidence=request.evidence,
            procedure_digest=declaration.procedure_artifact_digest,
        )
        evidence_kind = "terminal"
        proof_kind = "run_receipt"
        proof_digest = terminal.record_digest
        proof_subject = None
        settlement_actor = terminal.record.actor_context
    elif not isinstance(request.evidence, ObservationSettlementEvidenceV1):
        raise _refuse(
            "settlement_evidence_mismatch",
            "Settlement evidence kind is unsupported.",
            prediction_id=prediction_id,
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
            "evidence_present": True,
            "prediction_value": predicted_value,
            "settlement_value": settlement_value,
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
        actor_context=settlement_actor,
    )
    resolution = _append_settlement(
        instance,
        activation=activation,
        resolution=resolution,
    )
    relation = build_settled_outcome_relation(activation, resolution)
    return PlaybillSettleResultV1(
        prediction_id=prediction_id,
        activation=activation.model_dump(mode="json"),
        resolution=resolution.model_dump(mode="json"),
        relation=relation.model_dump(mode="json"),
    )


def load_prediction_activations(
    instance: PlaybillInstance,
) -> tuple[ResolutionContractActivationV2, ...]:
    """Replay exact served activations for settled-outcome/calibration folds."""

    journal, stream = _journal(instance)
    activations: dict[str, ResolutionContractActivationV2] = {}
    for partition in journal.partition_ids(stream):
        for stored in journal.all_records(stream, partition):
            if stored.record.event_kind != "resolution_activation":
                continue
            payload = parse_journal_payload(
                instance.body_store().read(
                    stored.record.payload_digest,
                    access=BodyAccessContext(
                        principal_id="playbill-prediction-replay",
                        can_read_body=True,
                    ),
                )
            )
            activation = ResolutionContractActivationV2.model_validate(payload)
            if partition != resolution_contract_partition_id(activation):
                raise PlaybillFormatError("prediction activation crossed its journal partition")
            previous = activations.setdefault(activation.contract_id, activation)
            if previous != activation:
                raise PlaybillFormatError("prediction activation history diverged")
    return tuple(
        activations[key] for key in sorted(activations, key=lambda item: item.encode("utf-8"))
    )


__all__ = [
    "PredictionRefused",
    "load_prediction_activations",
    "service_predict_playbill",
    "service_settle_playbill_prediction",
]
