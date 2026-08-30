"""Served readiness, binding, and query-only Procedure execution."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillExecutionError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactAny,
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
    render_procedure,
)
from cruxible_client.contracts.procedures.closure import (
    LineSlotBindingV1,
    ProcedurePinClosureError,
    close_procedure_pin_slots,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinitionV3,
    ProcedurePinSlotRefV1,
    RepeatNodeV3,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureBudgetRefusalDetailV1,
    ProcedureInternalFailureV1,
    ProcedureJournalCoordinateV1,
    ProcedureNodeRefusalV1,
    ProcedureOperationalFailureV1,
    ProcedureRunAttributionV1,
    ProcedureRunReceiptV2,
    ProcedureTerminalV1,
)
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.closure import build_dependency_index
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.exhaust.promotions import VerifiedExhaustRecordV1
from cruxible_core.playbill.exhaust.records import parse_journal_payload
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.procedures.execution import (
    PROCEDURE_RUN_RECEIPT_V2_DOMAIN,
    ProcedureAdmissionBoundPayloadV2,
    ProcedureClockProtocol,
    ProcedureRunAdmissionV2,
    ProcedureRunReceiptV1,
    prepare_direct_procedure_run,
)
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_procedures import (
    PlaybillProcedureStateTapReader,
    service_execute_direct_procedure,
)

PROCEDURE_RUN_ID_DOMAIN = "playbill-procedure-run-id-v1"
PROCEDURE_RUN_STREAM_ID = "procedures"
PROCEDURE_RUN_PARTITION_ID = "direct-runs"
PROCEDURE_RUN_FENCING_TOKEN = "playbill-procedure-direct-run-v1"
DIRECT_RECEIPT_REDUCER_DOMAIN = "playbill-direct-procedure-receipt-reducer-v1"
SERVED_NODE_KINDS = frozenset({"state_tap", "transform", "project", "guard", "repeat"})


class _StrictProcedureSurfaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureSurfaceError(PlaybillError):
    code = "playbill.procedure.refused"


class ProcedureNotFound(ProcedureSurfaceError):
    code = "playbill.procedure.not_found"


class ProcedureRetired(ProcedureSurfaceError):
    code = "playbill.procedure.retired"


class ProcedureBindingSetMismatch(ProcedureSurfaceError):
    code = "playbill.procedure.binding_set_mismatch"


class ProcedureBindingTargetNotFound(ProcedureSurfaceError):
    code = "playbill.procedure.binding_target_not_found"


class ProcedureBindingRoleMismatch(ProcedureSurfaceError):
    code = "playbill.procedure.binding_role_mismatch"


class ProcedureBindingKindMismatch(ProcedureSurfaceError):
    code = "playbill.procedure.binding_kind_mismatch"


class ProcedureBindingInterfaceMismatch(ProcedureSurfaceError):
    code = "playbill.procedure.binding_interface_mismatch"


class ProcedureBindingStaleCoordinate(ProcedureSurfaceError):
    code = "playbill.procedure.binding_stale_coordinate"


class ProcedureEffectfulUnsupported(ProcedureSurfaceError):
    code = "playbill.procedure.run.effectful_unsupported"


class ProcedureRunNotCurrent(ProcedureSurfaceError):
    code = "playbill.procedure.run.not_current"


class ProcedureRunNotFound(ProcedureSurfaceError):
    code = "playbill.procedure.run.not_found"


class ProcedureRunRecoveryRequired(ProcedureSurfaceError):
    code = "playbill.procedure.run.recovery_required"


class ProcedureNextOperationV1(_StrictProcedureSurfaceModel):
    kind: Literal["run", "bind", "retry", "done", "terminal"]


class ProcedureUnsupportedNodeV1(_StrictProcedureSurfaceModel):
    node_id: str
    kind: str


class ProcedureReadinessRequestV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-readiness-request-v1"] = (
        "playbill-procedure-readiness-request-v1"
    )
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProcedureReadinessResultV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-readiness-result-v1"] = (
        "playbill-procedure-readiness-result-v1"
    )
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    state: Literal["ready", "binding_required", "unsupported"]
    required_slots: tuple[str, ...]
    unsupported_nodes: tuple[ProcedureUnsupportedNodeV1, ...]
    next_operation: ProcedureNextOperationV1


class ProcedureBindingTargetV1(_StrictProcedureSurfaceModel):
    kind: str
    name: str


class ProcedureSlotBindingRequestV1(_StrictProcedureSurfaceModel):
    slot_name: str
    target: ProcedureBindingTargetV1


class ProcedureBindRequestV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-bind-request-v1"] = "playbill-procedure-bind-request-v1"
    bindings: tuple[ProcedureSlotBindingRequestV1, ...]

    @field_validator("bindings")
    @classmethod
    def _bindings(
        cls,
        value: tuple[ProcedureSlotBindingRequestV1, ...],
    ) -> tuple[ProcedureSlotBindingRequestV1, ...]:
        names = tuple(item.slot_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Procedure bindings must be byte-sorted and unique")
        return value


class ProcedurePendingSuccessorV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-pending-successor-v1"] = (
        "playbill-procedure-pending-successor-v1"
    )
    proposal_id: str
    pending_successor_digest: str


class ProcedureBindResultV2(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-bind-result-v2"] = "playbill-procedure-bind-result-v2"
    accepted_digest: str
    accepted_readiness: ProcedureReadinessResultV1
    pending: ProcedurePendingSuccessorV1 | None = None


class ProcedureRunRequestV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-run-request-v1"] = "playbill-procedure-run-request-v1"
    evaluation_time: datetime
    input: object

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class ProcedureRunRequestV2(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-run-request-v2"] = "playbill-procedure-run-request-v2"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None
    input: object

    @field_validator("evaluation_time")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_validator("input", mode="before")
    @classmethod
    def _input(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class ProcedureRunOutcomeV1(_StrictProcedureSurfaceModel):
    sequence: int = Field(ge=1)
    event_kind: str
    node_id: str | None = None
    payload_digest: str


class ProcedureRunStateV2(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-run-state-v2"] = "playbill-procedure-run-state-v2"
    run_id: str | None
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    bound_coordinate: PlaybillAcceptedCoordinate
    head_at_admission: PlaybillAcceptedCoordinate
    lane: Literal["current", "replay"]
    evaluation_time: datetime
    status: Literal[
        "running",
        "succeeded",
        "admission_refused",
        "node_refused",
        "operational_failed",
        "internal_failed",
    ]
    pending_inputs: tuple[str, ...]
    outcomes: tuple[ProcedureRunOutcomeV1, ...]
    next_operation: ProcedureNextOperationV1
    result: object | None = None
    attribution: ProcedureRunAttributionV1 | None = None
    semantic_replay_key_digest: str | None = None
    semantic_result_digest: str | None = None
    receipt: ProcedureRunReceiptV2 | None = None
    receipt_digest: str | None = None
    terminal: ProcedureTerminalV1 | None = None

    @property
    def coordinate(self) -> PlaybillAcceptedCoordinate:
        return self.bound_coordinate


def _resolve_coordinate(
    instance: PlaybillInstance,
    at: AcceptedCoordinate | None,
) -> AcceptedProjectionCoordinate:
    if at is None:
        return instance.accepted_coordinate()
    return instance.resolve_accepted_coordinate(
        git_oid=at.git_oid,
        semantic_root=at.semantic_root,
        generation_root=at.generation_root,
        compiler_digest=at.compiler_digest,
    )


def _accepted_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    coordinate: AcceptedProjectionCoordinate,
) -> AcceptedProcedureV1:
    path = procedure_path(name)
    content = instance.tree_at(coordinate.git_oid).get(path)
    if content is None:
        raise ProcedureNotFound(f"{ProcedureNotFound.code}: {name}")
    procedure = parse_procedure(content, path=path)
    if procedure.lifecycle.state == "retired":
        raise ProcedureRetired(f"{ProcedureRetired.code}: {name}")
    return AcceptedProcedureV1(
        path=path,
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


def _required_slots(procedure: ProcedureArtifactAny) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                binding.slot_name
                for binding in iter_pin_bindings(procedure.definition)
                if isinstance(binding, ProcedurePinSlotRefV1)
            },
            key=lambda item: item.encode("utf-8"),
        )
    )


def _readiness(
    accepted: AcceptedProcedureV1,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
) -> ProcedureReadinessResultV1:
    unsupported_rows: list[ProcedureUnsupportedNodeV1] = []
    for node in accepted.procedure.definition.nodes:
        if node.kind not in SERVED_NODE_KINDS:
            unsupported_rows.append(
                ProcedureUnsupportedNodeV1(node_id=node.node_id, kind=node.kind)
            )
        if isinstance(node, RepeatNodeV3):
            unsupported_rows.extend(
                ProcedureUnsupportedNodeV1(
                    node_id=f"{node.node_id}.{body.node_id}",
                    kind=body.operation,
                )
                for body in node.body
                if body.operation != "transform"
            )
    unsupported = tuple(unsupported_rows)
    slots = _required_slots(accepted.procedure)
    if unsupported:
        state: Literal["ready", "binding_required", "unsupported"] = "unsupported"
        operation = ProcedureNextOperationV1(kind="terminal")
    elif slots:
        state = "binding_required"
        operation = ProcedureNextOperationV1(kind="bind")
    else:
        state = "ready"
        operation = ProcedureNextOperationV1(kind="run")
    return ProcedureReadinessResultV1(
        coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
        procedure_identity=accepted.procedure.identity,
        procedure_artifact_digest=accepted.artifact_digest,
        state=state,
        required_slots=slots,
        unsupported_nodes=unsupported,
        next_operation=operation,
    )


def service_playbill_procedure_readiness(
    instance: PlaybillInstance,
    *,
    name: str,
    request: ProcedureReadinessRequestV1,
) -> ProcedureReadinessResultV1:
    coordinate = _resolve_coordinate(instance, request.at)
    accepted = _accepted_procedure(instance, name=name, coordinate=coordinate)
    return _readiness(
        accepted,
        coordinate=coordinate,
        evaluation_time=request.evaluation_time,
    )


def _replace_slots(value: object, bindings: Mapping[str, ArtifactPin]) -> object:
    if isinstance(value, list):
        return [_replace_slots(item, bindings) for item in value]
    if isinstance(value, dict):
        if value.get("tag") == "playbill-procedure-pin-slot-ref-v1" and isinstance(
            value.get("slot_name"), str
        ):
            pin = bindings.get(value["slot_name"])
            if pin is not None:
                return pin.model_dump(mode="json")
        return {key: _replace_slots(item, bindings) for key, item in value.items()}
    return value


def _bound_successor(
    accepted: AcceptedProcedureV1,
    *,
    bindings: tuple[LineSlotBindingV1, ...],
    interface_digests: Mapping[str, str],
) -> ProcedureArtifactAny:
    try:
        closure = close_procedure_pin_slots(
            accepted.procedure,
            bindings=bindings,
            interface_digests=interface_digests,
        )
    except ProcedurePinClosureError as exc:
        message = str(exc)
        if "extra pin slots" in message or "unfilled_pin_slot" in message:
            error: type[ProcedureSurfaceError] = ProcedureBindingSetMismatch
        elif "role" in message:
            error = ProcedureBindingRoleMismatch
        elif "kind" in message:
            error = ProcedureBindingKindMismatch
        else:
            error = ProcedureBindingInterfaceMismatch
        raise error(f"{error.code}: {message}") from exc
    by_slot = {item.slot_name: item.artifact_pin for item in bindings}
    raw = accepted.procedure.definition.model_dump(mode="json", by_alias=True)
    replaced = _replace_slots(raw, by_slot)
    if not isinstance(replaced, dict):  # pragma: no cover - exact model dump
        raise ProcedureBindingSetMismatch(f"{ProcedureBindingSetMismatch.code}: invalid graph")
    replaced["pin_slots"] = [
        item.model_dump(mode="json")
        for item in accepted.procedure.definition.pin_slots
        if item.slot_name not in closure.bound_slot_names
    ]
    definition = ProcedureDefinitionV3.model_validate(replaced)
    return accepted.procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest_v3(definition).tagged,
            "pins": closure.exact_pins,
            "lifecycle": ArtifactLifecycle(predecessor_digest=accepted.artifact_digest),
        }
    )


def service_bind_playbill_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    request: ProcedureBindRequestV1,
    actor: AuthenticatedActor,
    timestamp: str,
) -> ProcedureBindResultV2:
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_procedure(instance, name=name, coordinate=coordinate)
    tree = instance.tree_at(coordinate.git_oid)
    index = build_dependency_index(tree)
    declarations = {item.slot_name: item for item in accepted.procedure.definition.pin_slots}
    requested = {item.slot_name for item in request.bindings}
    required = set(_required_slots(accepted.procedure))
    if requested != required:
        raise ProcedureBindingSetMismatch(
            f"{ProcedureBindingSetMismatch.code}: required={sorted(required)!r}; "
            f"supplied={sorted(requested)!r}"
        )
    lowered: list[LineSlotBindingV1] = []
    interface_digests: dict[str, str] = {}
    for item in request.bindings:
        declaration = declarations[item.slot_name]
        identity = ArtifactIdentity(kind=item.target.kind, name=item.target.name)
        path = index.paths_by_identity.get(identity.qualified)
        state = None if path is None else index.states[path]
        if state is None or state.lifecycle.state != "live":
            raise ProcedureBindingTargetNotFound(
                f"{ProcedureBindingTargetNotFound.code}: {identity.qualified}"
            )
        if identity.kind != declaration.artifact_kind:
            raise ProcedureBindingKindMismatch(
                f"{ProcedureBindingKindMismatch.code}: slot {item.slot_name} requires "
                f"{declaration.artifact_kind}"
            )
        interface_pin = next((pin for pin in state.pins if pin.role == "interface"), None)
        interface_digests[state.artifact_digest] = (
            state.artifact_digest if interface_pin is None else interface_pin.artifact_digest
        )
        lowered.append(
            LineSlotBindingV1(
                slot_name=item.slot_name,
                artifact_pin=ArtifactPin(
                    role=declaration.pin_role,
                    target=identity,
                    artifact_digest=state.artifact_digest,
                ),
            )
        )
    successor = _bound_successor(
        accepted,
        bindings=tuple(lowered),
        interface_digests=interface_digests,
    )
    if instance.accepted_coordinate() != coordinate:
        raise ProcedureBindingStaleCoordinate(
            f"{ProcedureBindingStaleCoordinate.code}: accepted coordinate advanced"
        )
    candidate_tree = dict(tree)
    candidate_tree[accepted.path] = render_procedure(successor)
    operation = typed_digest(
        Sha256Value,
        "playbill-procedure-bind-v1",
        {
            "actor_id": actor.actor_id,
            "coordinate": AcceptedCoordinate.from_internal(coordinate).model_dump(mode="json"),
            "predecessor_digest": accepted.artifact_digest,
            "successor_digest": procedure_artifact_digest(successor).tagged,
        },
    ).tagged
    proposal = instance.proposal_service().submit(
        actor=actor,
        request=ProposalAdmissionRequest(
            target_ref=(
                f"refs/proposals/{actor.actor_id}/procedure-bind-"
                f"{operation.removeprefix('sha256:')}"
            ),
            proposed_base_oid=coordinate.git_oid,
        ),
        candidate_tree=candidate_tree,
        timestamp=timestamp,
    )
    pending_digest = procedure_artifact_digest(successor).tagged
    return ProcedureBindResultV2(
        accepted_digest=accepted.artifact_digest,
        accepted_readiness=_readiness(
            accepted,
            coordinate=coordinate,
            evaluation_time=ensure_utc(datetime.fromisoformat(timestamp.replace("Z", "+00:00"))),
        ),
        pending=ProcedurePendingSuccessorV1(
            proposal_id=proposal.admission.proposal_id,
            pending_successor_digest=pending_digest,
        ),
    )


@dataclass(frozen=True)
class _CurrentProcedureAuthority:
    instance: PlaybillInstance

    def current_procedure_digest(
        self,
        identity: ArtifactIdentity,
        *,
        coordinate: AcceptedCoordinate,
    ) -> str | None:
        del coordinate
        current = self.instance.accepted_coordinate()
        path = procedure_path(identity.name)
        content = self.instance.tree_at(current.git_oid).get(path)
        if content is None:
            return None
        procedure = parse_procedure(content, path=path)
        if procedure.lifecycle.state != "live":
            return None
        return procedure_artifact_digest(procedure).tagged


@dataclass
class _DeterministicClock(ProcedureClockProtocol):
    evaluation_time: datetime

    def now(self) -> datetime:
        return self.evaluation_time

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def _journal(instance: PlaybillInstance) -> tuple[LocalJournalBackend, Path]:
    root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    root.mkdir(mode=0o700, exist_ok=True)
    return LocalJournalBackend(root), root


def _stream(instance: PlaybillInstance) -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id=instance.descriptor.instance_id,
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id=PROCEDURE_RUN_STREAM_ID,
    )


def _activate_writer(
    journal: LocalJournalBackend,
    stream: JournalStreamIdentityV1,
    partition_id: str,
) -> None:
    state = journal.writer_state(stream, partition_id)
    if state is not None and state.active:
        if state.fencing_token != PROCEDURE_RUN_FENCING_TOKEN:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: another writer owns the partition"
            )
        return
    journal.activate_writer(
        stream,
        partition_id,
        fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
        expected_head=journal.read_head(stream, partition_id),
    )


def _run_id(
    instance: PlaybillInstance,
    *,
    actor_id: str,
    accepted: AcceptedProcedureV1,
    coordinate: AcceptedProjectionCoordinate,
    request: ProcedureRunRequestV1,
) -> str:
    digest = typed_digest(
        Sha256Value,
        PROCEDURE_RUN_ID_DOMAIN,
        {
            "instance_id": instance.descriptor.instance_id,
            "actor_id": actor_id,
            "procedure_artifact_digest": accepted.artifact_digest,
            "accepted_coordinate": AcceptedCoordinate.from_internal(coordinate).model_dump(
                mode="json"
            ),
            "evaluation_time": format_datetime(request.evaluation_time),
            "canonical_input": request.input,
        },
    ).tagged
    return "RUN-" + digest.removeprefix("sha256:")


def _records_for_run(instance: PlaybillInstance, run_id: str):  # type: ignore[no-untyped-def]
    journal, _root = _journal(instance)
    return tuple(
        item
        for partition_id in journal.partition_ids(_stream(instance))
        for item in journal.all_records(_stream(instance), partition_id)
        if item.record.run_id == run_id
    )


def _journal_coordinate(stored) -> ProcedureJournalCoordinateV1:  # type: ignore[no-untyped-def]
    record = stored.record
    return ProcedureJournalCoordinateV1(
        stream_instance_id=record.stream.instance_id,
        journal_family=record.stream.journal_family,
        stream_id=record.stream.stream_id,
        partition_id=record.partition_id,
        sequence=record.sequence,
        record_digest=stored.record_digest,
    )


def _state_from_records(
    instance: PlaybillInstance,
    *,
    run_id: str,
    receipt: ProcedureRunReceiptV1 | None = None,
) -> ProcedureRunStateV2:
    records = _records_for_run(instance, run_id)
    if not records:
        raise ProcedureRunNotFound(f"{ProcedureRunNotFound.code}: {run_id}")
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="procedure-runtime", can_read_body=True)
    admission: ProcedureRunAdmissionV2 | None = None
    final = None
    outcomes: list[ProcedureRunOutcomeV1] = []
    for stored in records:
        payload = parse_journal_payload(bodies.read(stored.record.payload_digest, access=access))
        if stored.record.event_kind == "admission_bound":
            if isinstance(payload, dict) and payload.get("tag") == (
                "playbill-procedure-admission-bound-payload-v2"
            ):
                admission = ProcedureAdmissionBoundPayloadV2.model_validate(payload).admission
        if stored.record.event_kind in {"node_fired", "attempt_finalized"}:
            node_id = payload.get("node_id") if isinstance(payload, dict) else None
            outcomes.append(
                ProcedureRunOutcomeV1(
                    sequence=stored.record.sequence,
                    event_kind=stored.record.event_kind,
                    node_id=node_id if isinstance(node_id, str) else None,
                    payload_digest=stored.record.payload_digest,
                )
            )
        if stored.record.event_kind == "attempt_finalized":
            final = payload
    if admission is None:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run lacks a v2 admission_bound"
        )
    status: Literal[
        "running",
        "succeeded",
        "node_refused",
        "operational_failed",
        "internal_failed",
    ] = "running"
    result = None
    terminal: ProcedureTerminalV1 | None = None
    semantic_result_digest = None
    if isinstance(final, dict):
        raw_status = final.get("status")
        if raw_status not in {"succeeded", "refused", "failed", "budget_exhausted"}:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: final status is invalid"
            )
        status = (
            "succeeded"
            if raw_status == "succeeded"
            else "node_refused"
            if raw_status in {"refused", "budget_exhausted"}
            else "internal_failed"
        )
        result = final.get("output") if raw_status == "succeeded" else None
        raw_semantic = final.get("semantic_result_digest")
        semantic_result_digest = raw_semantic if isinstance(raw_semantic, str) else None
        final_record = records[-1]
        last_node_id = next(
            (item.node_id for item in reversed(outcomes) if item.node_id is not None),
            "procedure",
        )
        if raw_status == "refused":
            raw_refusal = final.get("refusal")
            refusal = raw_refusal if isinstance(raw_refusal, dict) else {}
            raw_budget = refusal.get("budget")
            budget = (
                ProcedureBudgetRefusalDetailV1.model_validate(raw_budget)
                if isinstance(raw_budget, dict)
                else None
            )
            detail_code = str(refusal.get("code", "refused"))
            terminal = ProcedureNodeRefusalV1(
                code=("budget_exhausted" if detail_code == "budget_exhausted" else "guard_refused"),
                message=str(refusal.get("message", "Procedure node refused execution.")),
                node_id=str(refusal.get("node_id") or last_node_id),
                journal_coordinate=_journal_coordinate(final_record),
                detail_code=None if detail_code == "budget_exhausted" else detail_code,
                details=refusal.get("details", {}),
                budget=budget,
            )
        elif raw_status == "budget_exhausted":
            terminal = ProcedureNodeRefusalV1(
                code="budget_exhausted",
                message="Procedure execution exhausted its declared budget.",
                node_id=last_node_id,
                journal_coordinate=_journal_coordinate(final_record),
            )
        elif raw_status == "failed":
            failure_code = final.get("failure_code")
            if failure_code == "cas_unavailable_at_replay":
                status = "operational_failed"
                terminal = ProcedureOperationalFailureV1(
                    code="cas_unavailable_at_replay",
                    message="Admitted Procedure replay material is unavailable.",
                    journal_coordinate=_journal_coordinate(final_record),
                )
            else:
                terminal = ProcedureInternalFailureV1(
                    code="unexpected_exception",
                    message="Procedure execution failed unexpectedly; inspect daemon logs.",
                    correlation_id=run_id,
                    journal_coordinate=_journal_coordinate(final_record),
                )
    attribution = ProcedureRunAttributionV1(
        actor_type=admission.actor_context.actor_type,
        actor_id=admission.actor_context.actor_id,
        org_id=admission.actor_context.org_id,
        operation_id=admission.actor_context.operation_id,
        request_id=admission.actor_context.request_id,
        recorded_time=admission.actor_context.timestamp,
    )
    public_receipt = None
    receipt_digest = None
    if final is not None:
        stream = records[0].record.stream
        public_receipt = ProcedureRunReceiptV2(
            run_id=run_id,
            admission_binding_digest=admission.admission_binding_digest,
            semantic_replay_key_digest=admission.semantic_replay_key_digest,
            semantic_result_digest=semantic_result_digest,
            bound_coordinate=admission.bound_coordinate,
            head_at_admission=admission.head_at_admission,
            lane=admission.lane,
            evaluation_time=admission.admitted_at,
            validated_pins=admission.full_pins,
            admitted_inputs=tuple(
                cast(dict[str, object], item.model_dump(mode="json"))
                for item in admission.accepted_state_inputs
            ),
            attribution=attribution,
            stream_instance_id=stream.instance_id,
            journal_family=stream.journal_family,
            stream_id=stream.stream_id,
            partition_id=records[0].record.partition_id,
            first_sequence=records[0].record.sequence,
            last_sequence=records[-1].record.sequence,
            record_digests=tuple(item.record_digest for item in records),
            chain_head_digest=records[-1].record_digest,
        )
        receipt_digest = typed_digest(
            Sha256Value,
            PROCEDURE_RUN_RECEIPT_V2_DOMAIN,
            {"receipt": public_receipt.model_dump(mode="json")},
        ).tagged
    next_kind: Literal["retry", "done", "terminal"] = (
        "done" if status == "succeeded" else "retry" if status == "running" else "terminal"
    )
    return ProcedureRunStateV2(
        run_id=run_id,
        procedure_identity=admission.procedure_identity,
        procedure_artifact_digest=admission.procedure_artifact_digest,
        bound_coordinate=PlaybillAcceptedCoordinate.model_validate(
            admission.bound_coordinate.model_dump(mode="json")
        ),
        head_at_admission=PlaybillAcceptedCoordinate.model_validate(
            admission.head_at_admission.model_dump(mode="json")
        ),
        lane=admission.lane,
        evaluation_time=admission.admitted_at,
        status=status,
        pending_inputs=(),
        outcomes=tuple(outcomes),
        next_operation=ProcedureNextOperationV1(kind=next_kind),
        result=result,
        attribution=attribution,
        semantic_replay_key_digest=admission.semantic_replay_key_digest,
        semantic_result_digest=semantic_result_digest,
        receipt=public_receipt,
        receipt_digest=receipt_digest,
        terminal=terminal,
    )


def service_run_playbill_procedure(
    instance: PlaybillInstance,
    *,
    name: str,
    request: ProcedureRunRequestV2,
    actor_context: GovernedActorContext,
) -> ProcedureRunStateV2:
    coordinate = _resolve_coordinate(instance, request.at)
    evaluation_time = request.evaluation_time or instance.accepted_evaluation_time(
        coordinate.git_oid
    )
    head_at_admission = instance.accepted_coordinate()
    lane: Literal["current", "replay"] = "replay" if request.at is not None else "current"
    accepted = _accepted_procedure(instance, name=name, coordinate=coordinate)
    readiness = _readiness(
        accepted,
        coordinate=coordinate,
        evaluation_time=evaluation_time,
    )
    if readiness.state == "binding_required":
        return ProcedureRunStateV2(
            run_id=None,
            procedure_identity=accepted.procedure.identity,
            procedure_artifact_digest=accepted.artifact_digest,
            bound_coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
            head_at_admission=PlaybillAcceptedCoordinate.from_internal(head_at_admission),
            lane=lane,
            evaluation_time=evaluation_time,
            status="admission_refused",
            pending_inputs=readiness.required_slots,
            outcomes=(),
            next_operation=ProcedureNextOperationV1(kind="bind"),
            terminal=ProcedureAdmissionRefusalV1(
                code="binding_required",
                message="Procedure accepted bindings are incomplete.",
                details={"required_slots": list(readiness.required_slots)},
            ),
        )
    if readiness.state == "unsupported":
        return ProcedureRunStateV2(
            run_id=None,
            procedure_identity=accepted.procedure.identity,
            procedure_artifact_digest=accepted.artifact_digest,
            bound_coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
            head_at_admission=PlaybillAcceptedCoordinate.from_internal(head_at_admission),
            lane=lane,
            evaluation_time=evaluation_time,
            status="admission_refused",
            pending_inputs=(),
            outcomes=(),
            next_operation=ProcedureNextOperationV1(kind="terminal"),
            terminal=ProcedureAdmissionRefusalV1(
                code="unsupported_node",
                message="Procedure contains node kinds unavailable on the served run lane.",
                details={
                    "unsupported_nodes": [
                        item.model_dump(mode="json") for item in readiness.unsupported_nodes
                    ]
                },
            ),
        )
    stream = _stream(instance)
    journal, root = _journal(instance)
    prepared = prepare_direct_procedure_run(
        accepted,
        instance_id=instance.descriptor.instance_id,
        run_id=None,
        accepted_coordinate=AcceptedCoordinate.from_internal(coordinate),
        invocation_input=request.input,
        actor_context=actor_context.model_copy(update={"timestamp": evaluation_time}),
        state_reader=PlaybillProcedureStateTapReader(
            instance=instance,
            evaluation_time=evaluation_time,
        ),
        bodies=instance.body_store(),
        journal_stream=stream,
        journal_partition_id=None,
        head_at_admission=AcceptedCoordinate.from_internal(head_at_admission),
        lane=lane,
        admitted_at=evaluation_time,
    )
    _activate_writer(journal, stream, prepared.admission.journal_partition_id)
    if lane == "current" and instance.accepted_coordinate() != coordinate:
        raise ProcedureRunNotCurrent(
            f"{ProcedureRunNotCurrent.code}: accepted coordinate advanced before append"
        )
    try:
        result = service_execute_direct_procedure(
            prepared,
            accepted,
            journal=journal,
            bodies=instance.body_store(),
            run_index_path=root / "procedure-run-index.sqlite",
            fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
            activation_authority=_CurrentProcedureAuthority(instance),
            provider_executor=None,
            clock=_DeterministicClock(evaluation_time),
        )
    except PlaybillExecutionError as exc:
        if "run_recovery_required" in str(exc):
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: {exc}"
            ) from exc
        if "current" in str(exc):
            raise ProcedureRunNotCurrent(f"{ProcedureRunNotCurrent.code}: {exc}") from exc
        raise
    return _state_from_records(instance, run_id=prepared.admission.run_id, receipt=result.receipt)


def service_get_playbill_procedure_run(
    instance: PlaybillInstance,
    *,
    run_id: str,
) -> ProcedureRunStateV2:
    return _state_from_records(instance, run_id=run_id)


class DirectProcedureReceiptReducer:
    """Pure reducer for later ExhaustPromotion of one finalized direct run."""

    @property
    def reducer_digest(self) -> str:
        return typed_digest(
            Sha256Value,
            DIRECT_RECEIPT_REDUCER_DOMAIN,
            {"accepted_event_kinds": ["attempt_finalized"]},
        ).tagged

    def reduce(self, records: tuple[VerifiedExhaustRecordV1, ...]) -> object:
        finalized = tuple(item for item in records if item.event_kind == "attempt_finalized")
        if len(finalized) != 1 or finalized[0] != records[-1]:
            raise ValueError("direct Procedure receipt range must end in one finalization")
        run_ids = {item.run_id for item in records}
        procedures = {item.procedure_artifact_digest for item in records}
        if None in run_ids or len(run_ids) != 1 or len(procedures) != 1:
            raise ValueError("direct Procedure receipt range mixes run identities")
        return normalize_canonical(
            {
                "run_id": finalized[0].run_id,
                "procedure_artifact_digest": finalized[0].procedure_artifact_digest,
                "result": finalized[0].payload,
                "record_count": len(records),
            }
        )


__all__ = [
    "DIRECT_RECEIPT_REDUCER_DOMAIN",
    "DirectProcedureReceiptReducer",
    "PROCEDURE_RUN_ID_DOMAIN",
    "ProcedureBindRequestV1",
    "ProcedureBindResultV2",
    "ProcedurePendingSuccessorV1",
    "ProcedureReadinessRequestV1",
    "ProcedureReadinessResultV1",
    "ProcedureRunRequestV2",
    "ProcedureRunStateV2",
    "ProcedureSurfaceError",
    "service_bind_playbill_procedure",
    "service_get_playbill_procedure_run",
    "service_playbill_procedure_readiness",
    "service_run_playbill_procedure",
]
