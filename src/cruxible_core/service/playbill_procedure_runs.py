"""Served readiness, binding, and query-only Procedure execution."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.errors import PlaybillError, PlaybillExecutionError
from cruxible_client.contracts.procedure_mandates import (
    ProcedureMandateV1,
    parse_procedure_mandate,
    procedure_mandate_digest,
)
from cruxible_client.contracts.procedure_runtime_policy import PROCEDURE_RUNTIME_POLICY_PATH
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
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpecV1,
    CadenceTriggerPolicyV1,
    CaptureLandingTriggerPolicyV1,
    LineSpecV2,
    ManualTriggerPolicyV1,
    evaluate_line_spec_law,
    line_identity_digest,
    line_spec_digest,
    parse_line_spec,
)
from cruxible_client.contracts.procedures.models import (
    ExhaustTapNodeV3,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureDefinitionV4,
    ProcedurePinSlotRefV1,
    ProviderNodeV3,
    ProviderNodeV4,
    RepeatBodyNodeV4,
    RepeatNodeV3,
    RepeatNodeV4,
    SourceNodeV3,
    SourceNodeV4,
    StateTapNodeV3,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAcquisitionPlanV2,
    ProcedureAdmissionMaterialManifestV1,
    ProcedureAdmissionRefusalCodeV1,
    ProcedureAdmissionRefusalV1,
    ProcedureBudgetBoundaryObservationV1,
    ProcedureBudgetExceededDetailV1,
    ProcedureBudgetExhaustedV1,
    ProcedureBudgetRefusalDetailV1,
    ProcedureHaltTerminalV1,
    ProcedureInternalFailureCodeV1,
    ProcedureInternalFailureV1,
    ProcedureJournalCoordinateV1,
    ProcedureNodeRefusalV1,
    ProcedureOperationalFailureCodeV1,
    ProcedureOperationalFailureV1,
    ProcedurePendingSuccessorV1,
    ProcedureProviderBindingV1,
    ProcedureProviderBindingV2,
    ProcedureReplayInputProjectionV1,
    ProcedureRunAttributionV1,
    ProcedureRunBudgetDeclaredV1,
    ProcedureRunBudgetDeclaredV2,
    ProcedureRunBudgetObservedV1,
    ProcedureRunBudgetV1,
    ProcedureRunBudgetV2,
    ProcedureRunNodePinSetV1,
    ProcedureRunReceiptV2,
    ProcedureRunReceiptV3,
    ProcedureRunReceiptV4,
    ProcedureRunReceiptV5,
    ProcedureRunReceiptV6,
    ProcedureSelectionDecisionV1,
    ProcedureSettlementRefusalCodeV1,
    ProcedureSettlementRefusalV1,
    ProcedureSourceCaptureAssociationV1,
    ProcedureTerminalV1,
    ProviderBucketClassificationPlanV1,
    procedure_acquisition_plan_digest,
    procedure_admission_material_digest,
    procedure_selection_decision_digest,
)
from cruxible_client.contracts.provider_execution import (
    ProcedureDerivedSourceRequestV1,
    ProviderEgressObservationV1,
    ProviderExternalOccurrencePlanV1,
    ProviderInvocationCompletedV1,
    ProviderInvocationOutcomeV1,
    ProviderInvocationReceiptV1,
    ProviderInvocationStartedV1,
    ProviderSecretBindingIdentityV1,
    ProviderSecretReceiptReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
    provider_invocation_receipt_digest,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.provider_interfaces import (
    AcceptedProviderInterfaceRegistrationV1,
    parse_provider_interface,
    provider_interface_digest,
    provider_interface_path,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderV2,
    parse_provider,
    provider_digest,
)
from cruxible_client.contracts.repairs import hand_edit_repair
from cruxible_client.contracts.temporal import ensure_utc, format_datetime
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)
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
from cruxible_core.playbill.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.material_reservations import ProcedureMaterialReservationStore
from cruxible_core.playbill.procedures.egress import compute_effective_rung
from cruxible_core.playbill.procedures.execution import (
    PROCEDURE_RUN_RECEIPT_V2_DOMAIN,
    PROCEDURE_RUN_RECEIPT_V3_DOMAIN,
    PROCEDURE_RUN_RECEIPT_V4_DOMAIN,
    PROCEDURE_RUN_RECEIPT_V5_DOMAIN,
    PROCEDURE_RUN_RECEIPT_V6_DOMAIN,
    AcceptedStateRunMaterialV2,
    PreparedProcedureRunV5,
    ProcedureAdmissionBoundPayloadV2,
    ProcedureAdmissionBoundPayloadV3,
    ProcedureAdmissionBoundPayloadV4,
    ProcedureAdmissionBoundPayloadV5,
    ProcedureClockProtocol,
    ProcedureRunAdmissionV2,
    ProcedureRunAdmissionV3,
    ProcedureRunAdmissionV4,
    ProcedureRunAdmissionV5,
    ProcedureRunReceiptV1,
    ProcedureRuntimePolicyAbsent,
    ProviderRuntimeInvokerProtocol,
    bind_line_admission_runtime_policy,
    prepare_direct_procedure_run,
    procedure_admission_digest,
    procedure_line_journal_stream,
    procedure_line_partition,
    procedure_line_run_id,
    procedure_node_pin_sets,
    procedure_pin_set_digest,
    procedure_replay_input_vector,
    procedure_semantic_replay_key_digest,
    resolve_procedure_runtime_policy,
    run_value_digest,
    verify_line_admission_spec,
)
from cruxible_core.playbill.procedures.input_planes import AcceptedStateRunInputV2
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.provider_local_runtime import (
    ProviderLocalRuntimeRefused,
    translate_provider_budget,
)
from cruxible_core.playbill.provider_outcomes import map_provider_refusal
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.workspace_file import WorkspaceFileReader
from cruxible_core.service.playbill_procedures import (
    PlaybillProcedureStateTapReader,
    service_execute_direct_procedure,
)

PROCEDURE_RUN_ID_DOMAIN = "playbill-procedure-run-id-v1"
PROCEDURE_RUN_STREAM_ID = "procedures"
PROCEDURE_RUN_PARTITION_ID = "direct-runs"
PROCEDURE_RUN_FENCING_TOKEN = "playbill-procedure-direct-run-v1"
DIRECT_RECEIPT_REDUCER_DOMAIN = "playbill-direct-procedure-receipt-reducer-v1"
SERVED_NODE_KINDS = frozenset({"state_tap", "transform", "project", "guard", "repeat", "halt"})


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


class ProcedureBindingGraphV4LineClosureRequired(ProcedureSurfaceError):
    code = "playbill.procedure.binding.graph_v4_line_closure_required"


class ProcedureRunNotCurrent(ProcedureSurfaceError):
    code = "playbill.procedure.run.not_current"


class ProcedureRunNotFound(ProcedureSurfaceError):
    code = "playbill.procedure.run.not_found"


class ProcedureRunRecoveryRequired(ProcedureSurfaceError):
    code = "playbill.procedure.run.recovery_required"


class LineRunNotAccepted(ProcedureSurfaceError):
    code = "playbill.line.run.line_not_accepted"


class LineRunIdentityMismatch(ProcedureSurfaceError):
    code = "playbill.line.run.line_identity_mismatch"


class ProcedureNextOperationV1(_StrictProcedureSurfaceModel):
    kind: Literal["run", "bind", "retry", "done", "terminal"]


class ProviderRuntimeOperatorProtocol(Protocol):
    def invoker_for(
        self,
        instance: PlaybillInstance,
        *,
        accepted_oid: str,
    ) -> ProviderRuntimeInvokerProtocol: ...

    def admit_line_provider(
        self,
        accepted_provider: AcceptedProviderV1,
        accepted_interface: AcceptedProviderInterfaceRegistrationV1,
        implementation_digest: str,
        *,
        eligible_environment_pin_keys: tuple[str, ...],
    ) -> VerifiedProviderBindingV1: ...


class ProcedureUnsupportedNodeV1(_StrictProcedureSurfaceModel):
    node_id: str
    kind: str


class ProcedureReadinessRequestV1(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-readiness-request-v1"] = (
        "playbill-procedure-readiness-request-v1"
    )
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime = Field(description="Reads EVALUATION INSTANT.")

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
    definition_digest: str
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


class ProcedureBindResultV2(_StrictProcedureSurfaceModel):
    tag: Literal["playbill-procedure-bind-result-v2"] = "playbill-procedure-bind-result-v2"
    accepted_digest: str
    accepted_readiness: ProcedureReadinessResultV1
    pending: ProcedurePendingSuccessorV1 | None = None
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT


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


class LineRunRequestV1(_StrictProcedureSurfaceModel):
    """An assertion against one daemon-derived accepted Line occurrence."""

    tag: Literal["playbill-line-run-request-v1"] = "playbill-line-run-request-v1"
    line_identity_digest: str
    occurrence_id: str | None = None
    evaluation_time: datetime = Field(description="Reads EVALUATION INSTANT.")

    @field_validator("line_identity_digest")
    @classmethod
    def _identity_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


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
        "halted",
    ]
    pending_inputs: tuple[str, ...]
    outcomes: tuple[ProcedureRunOutcomeV1, ...]
    next_operation: ProcedureNextOperationV1
    result: object | None = None
    attribution: ProcedureRunAttributionV1 | None = None
    semantic_replay_key_digest: str | None = None
    semantic_result_digest: str | None = None
    receipt: (
        ProcedureRunReceiptV2
        | ProcedureRunReceiptV3
        | ProcedureRunReceiptV4
        | ProcedureRunReceiptV5
        | ProcedureRunReceiptV6
        | None
    ) = None
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


def _accepted_line_by_identity_digest(
    tree: Mapping[str, bytes],
    *,
    identity_digest: str,
) -> AcceptedLineSpecV1:
    matches: list[AcceptedLineSpecV1] = []
    for path, content in tree.items():
        if not path.startswith("lines/") or not path.endswith((".json", ".yaml")):
            continue
        line = parse_line_spec(content, path=path)
        if line.lifecycle.state == "retired":
            continue
        if line_identity_digest(line.identity) == identity_digest:
            matches.append(
                AcceptedLineSpecV1(
                    path=path,
                    line=line,
                    artifact_digest=line_spec_digest(line).tagged,
                )
            )
    if len(matches) != 1:
        raise LineRunNotAccepted(f"{LineRunNotAccepted.code}: {identity_digest}")
    return matches[0]


def _line_catalogs(
    tree: Mapping[str, bytes],
) -> tuple[
    dict[str, AcceptedProviderV1],
    dict[str, AcceptedProviderInterfaceRegistrationV1],
]:
    providers: dict[str, AcceptedProviderV1] = {}
    interfaces: dict[str, AcceptedProviderInterfaceRegistrationV1] = {}
    for path, content in tree.items():
        if path.startswith("providers/") and path.endswith((".json", ".yaml")):
            provider = parse_provider(content, path=path)
            if provider.lifecycle.state == "live":
                digest = provider_digest(provider).tagged
                providers[digest] = AcceptedProviderV1(
                    path=path,
                    provider=provider,
                    artifact_digest=digest,
                )
        elif path.startswith("provider-interfaces/") and path.endswith((".json", ".yaml")):
            registration = parse_provider_interface(content, path=path)
            if registration.lifecycle.state == "live":
                digest = provider_interface_digest(registration).tagged
                interfaces[digest] = AcceptedProviderInterfaceRegistrationV1(
                    path=path,
                    registration=registration,
                    artifact_digest=digest,
                )
    return providers, interfaces


def _assert_line_closure_complete(
    tree: Mapping[str, bytes],
    accepted_line: AcceptedLineSpecV1,
) -> None:
    index = build_dependency_index(tree)
    for pin in accepted_line.line.pins:
        target_path = index.paths_by_identity.get(pin.target.qualified)
        if target_path is None:
            # These component families are exact registry pins until they gain
            # ledger envelopes. Their owning law, not name lookup, verifies them.
            if pin.target.kind in {
                "Contract",
                "EffectPolicy",
                "EnvironmentManifest",
                "ExhaustReducer",
                "LandingFilter",
                "Policy",
                "ReceiptSetManifest",
                "Reducer",
            }:
                continue
            raise PlaybillExecutionError(
                f"accepted Line closure lost {pin.target.qualified} ({pin.role})"
            )
        target = index.states[target_path]
        if target.artifact_digest != pin.artifact_digest or target.lifecycle.state != "live":
            raise PlaybillExecutionError(
                f"accepted Line closure does not reproduce {pin.target.qualified} ({pin.role})"
            )


def _line_slot_pins(accepted_line: AcceptedLineSpecV1) -> dict[str, ArtifactPin]:
    return {item.slot_name: item.artifact_pin for item in accepted_line.line.slot_bindings}


def _resolve_line_pin(
    value: ArtifactPin | ProcedurePinSlotRefV1,
    *,
    slot_pins: Mapping[str, ArtifactPin],
) -> ArtifactPin:
    if isinstance(value, ArtifactPin):
        return value
    try:
        return slot_pins[value.slot_name]
    except KeyError as exc:
        raise PlaybillExecutionError(
            f"accepted Line closure lost slot {value.slot_name!r}"
        ) from exc


def _line_admissions(
    instance: PlaybillInstance,
    accepted_line: AcceptedLineSpecV1,
) -> tuple[ProcedureRunAdmissionV5, ...]:
    journal, _root = _journal_for_write(instance)
    stream = procedure_line_journal_stream(instance.descriptor.instance_id)
    partition = procedure_line_partition(accepted_line.line.identity)
    admissions: list[ProcedureRunAdmissionV5] = []
    for stored in journal.all_records(stream, partition):
        if stored.record.event_kind != "admission_bound":
            continue
        payload = parse_journal_payload(
            instance.body_store().read(
                stored.record.payload_digest,
                access=BodyAccessContext(
                    principal_id="line-occurrence-registry",
                    can_read_body=True,
                ),
            )
        )
        if not isinstance(payload, dict) or payload.get("tag") != (
            "playbill-procedure-admission-bound-payload-v5"
        ):
            continue
        admissions.append(ProcedureAdmissionBoundPayloadV5.model_validate(payload).admission)
    return tuple(admissions)


def _trigger_interval_seconds(
    tree: Mapping[str, bytes],
    accepted_line: AcceptedLineSpecV1,
) -> int | None:
    trigger = accepted_line.line.trigger_policy
    role = (
        "trigger-cadence-policy"
        if isinstance(trigger, CadenceTriggerPolicyV1)
        else "trigger-window-policy"
    )
    pin = next((item for item in accepted_line.line.pins if item.role == role), None)
    if pin is None:
        return None
    candidates = (
        f"policies/{pin.target.name}.json",
        f"policies/{pin.target.name}.yaml",
    )
    for path in candidates:
        content = tree.get(path)
        if content is None:
            continue
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        for key in ("interval_seconds", "cadence_seconds", "window_seconds"):
            seconds = value.get(key)
            if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
                return seconds
    return None


def _line_occurrence(
    tree: Mapping[str, bytes],
    accepted_line: AcceptedLineSpecV1,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    prior: tuple[ProcedureRunAdmissionV5, ...],
) -> tuple[str, datetime | None, str | None]:
    trigger = accepted_line.line.trigger_policy
    last = max(prior, key=lambda item: item.occurrence_evaluation_time, default=None)
    next_due: datetime | None = None
    awaited: str | None = None
    if isinstance(trigger, ManualTriggerPolicyV1):
        occurrence_basis: object = format_datetime(evaluation_time)
    elif isinstance(trigger, CaptureLandingTriggerPolicyV1):
        if last is not None and coordinate.git_oid == last.bound_coordinate.git_oid:
            awaited = trigger.anchor_capture_contract_digest
        occurrence_basis = coordinate.git_oid
    else:
        interval = _trigger_interval_seconds(tree, accepted_line)
        if last is not None:
            if interval is None:
                awaited = (
                    trigger.cadence_policy_digest
                    if isinstance(trigger, CadenceTriggerPolicyV1)
                    else trigger.window_policy_digest
                )
            else:
                next_due = last.occurrence_evaluation_time + timedelta(seconds=interval)
        occurrence_basis = format_datetime(next_due or evaluation_time)
    occurrence_id = typed_digest(
        Sha256Value,
        "playbill-line-occurrence-v1",
        {
            "line_identity_digest": line_identity_digest(accepted_line.line.identity),
            "trigger_kind": trigger.kind,
            "trigger_instant_or_landing_digest": occurrence_basis,
        },
    ).tagged
    return occurrence_id, next_due, awaited


def _line_budget(
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
) -> ProcedureBudgetV3:
    budgets = accepted_line.line.budgets
    if not isinstance(accepted_line.line, LineSpecV2) or not isinstance(budgets, dict):
        return accepted_procedure.procedure.definition.budget
    return ProcedureBudgetV3(
        wall_clock=CanonicalDurationV1(microseconds=budgets["max_wall_clock_microseconds"]),
        max_provider_calls=budgets["max_provider_calls"],
        max_capture_bytes=budgets["max_capture_bytes"],
        max_items=budgets.get("max_items"),
    )


def _line_state_materials(
    instance: PlaybillInstance,
    accepted_procedure: AcceptedProcedureV1,
    *,
    coordinate: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    slot_pins: Mapping[str, ArtifactPin],
) -> tuple[AcceptedStateRunMaterialV2, ...]:
    materials: list[AcceptedStateRunMaterialV2] = []
    reader = PlaybillProcedureStateTapReader(instance=instance, evaluation_time=evaluation_time)
    accepted_coordinate = AcceptedCoordinate.from_internal(coordinate)
    for node in accepted_procedure.procedure.definition.nodes:
        if not isinstance(node, StateTapNodeV3):
            continue
        query = _resolve_line_pin(node.query, slot_pins=slot_pins)
        parameters = normalize_canonical(node.parameters)
        read = reader.read_accepted_state(
            query=query,
            parameters=parameters,
            coordinate=accepted_coordinate,
        )
        value = normalize_canonical(read.value)
        retained = instance.body_store().store(canonical_bytes(value))
        run_input = AcceptedStateRunInputV2(
            input_name=node.as_,
            read_coordinate=accepted_coordinate,
            query_definition_digest=query.artifact_digest,
            parameters_digest=run_value_digest("state-parameters", parameters),
            result_digest=run_value_digest("state-result", value),
            effective_query_budgets=read.effective_budgets,
            material_body_digest=retained.digest,
        )
        materials.append(AcceptedStateRunMaterialV2(input=run_input, value=value))
    return tuple(sorted(materials, key=lambda item: item.input.input_name.encode("utf-8")))


def _provider_nodes(
    definition: ProcedureDefinitionV4,
) -> tuple[tuple[ProviderNodeV4 | SourceNodeV4 | RepeatBodyNodeV4, str | None], ...]:
    result: list[tuple[ProviderNodeV4 | SourceNodeV4 | RepeatBodyNodeV4, str | None]] = []
    for node in definition.nodes:
        if isinstance(node, ProviderNodeV4 | SourceNodeV4):
            result.append((node, None))
        elif isinstance(node, RepeatNodeV4):
            result.extend(
                (body, node.node_id)
                for body in node.body
                if isinstance(body, RepeatBodyNodeV4) and body.operation == "provider"
            )
    return tuple(result)


def _line_external_occurrences(
    accepted_line: AcceptedLineSpecV1,
    accepted_procedure: AcceptedProcedureV1,
    *,
    providers: Mapping[str, AcceptedProviderV1],
    interfaces: Mapping[str, AcceptedProviderInterfaceRegistrationV1],
    slot_pins: Mapping[str, ArtifactPin],
    provider_runtime_operator: ProviderRuntimeOperatorProtocol | None,
    runtime_policy: object,
    budget: ProcedureBudgetV3,
) -> tuple[ProviderExternalOccurrencePlanV1, ...]:
    definition = accepted_procedure.procedure.definition
    if not isinstance(definition, ProcedureDefinitionV4):
        return ()
    occurrences: list[ProviderExternalOccurrencePlanV1] = []
    for node, repeat_node_id in _provider_nodes(definition):
        provider_binding = node.provider
        assert provider_binding is not None
        provider_pin = _resolve_line_pin(provider_binding, slot_pins=slot_pins)
        interface_pin = node.interface
        assert interface_pin is not None
        try:
            provider = providers[provider_pin.artifact_digest]
            interface = interfaces[interface_pin.artifact_digest]
        except KeyError as exc:
            raise PlaybillExecutionError(
                f"accepted Line Provider closure is unavailable for node {node.node_id!r}"
            ) from exc
        if not isinstance(provider.provider, ProviderV2):
            raise PlaybillExecutionError(
                f"accepted Line Provider node {node.node_id!r} requires Provider v2"
            )
        closure = next(
            (
                item
                for item in getattr(accepted_line.line, "provider_implementation_closures", ())
                if item.node_id == node.node_id
            ),
            None,
        )
        implementation_digest = (
            node.implementation_digest
            if isinstance(provider_binding, ArtifactPin)
            else None
            if closure is None
            else closure.implementation_digest
        )
        if implementation_digest is None:
            raise PlaybillExecutionError(
                f"accepted Line Provider closure lost implementation for node {node.node_id!r}"
            )
        implementation = next(
            (
                item
                for item in provider.provider.implementations
                if item.implementation_digest == implementation_digest
            ),
            None,
        )
        if implementation is None:
            raise PlaybillExecutionError(
                f"accepted Line Provider implementation is unavailable for node {node.node_id!r}"
            )
        eligible = (
            closure.environment_pin_map.eligible_environment_pin_keys
            if closure is not None
            else tuple(
                sorted(
                    (
                        item.environment_pin_key
                        for item in implementation.materialization_references
                        if item.kind == "local_env"
                    ),
                    key=str.encode,
                )
            )
        )
        if provider_runtime_operator is None:
            raise ProviderLocalRuntimeRefused(
                "provider_unavailable", "No daemon Provider runtime operator is installed."
            )
        local = provider_runtime_operator.admit_line_provider(
            provider,
            interface,
            implementation_digest,
            eligible_environment_pin_keys=eligible,
        )
        registration = interface.registration
        classification = ProviderBucketClassificationPlanV1(
            node_id=node.node_id,
            interface_artifact_digest=interface.artifact_digest,
            interface_digest=registration.interface_digest,
            vocabulary_digest=registration.vocabulary_digest,
            classifier_digest=registration.classifier_digest,
            accepted_bucket_selectors=tuple(
                sorted(
                    (item.selector for item in registration.conformance_proofs),
                    key=str.encode,
                )
            ),
        )
        produces_capture = isinstance(node, SourceNodeV4)
        translation = translate_provider_budget(
            budget=budget,
            hard_caps=definition.hard_caps,
            runtime_policy=runtime_policy,  # type: ignore[arg-type]
            remaining_wall_clock_microseconds=budget.wall_clock.microseconds,
            result_bytes_cap=max(1, definition.hard_caps.max_capture_bytes),
            produces_capture=produces_capture,
        )
        common = {
            "occurrence_path": (
                f"repeat/{repeat_node_id}/{node.node_id}"
                if repeat_node_id is not None
                else f"{'source' if produces_capture else 'provider'}/{node.node_id}"
            ),
            "occurrence_kind": "source" if produces_capture else "provider",
            "node_id": node.node_id,
            "repeat_node_id": repeat_node_id,
            "provider_artifact_digest": provider.artifact_digest,
            "interface_artifact_digest": interface.artifact_digest,
            "interface_id": registration.interface_id,
            "interface_digest": registration.interface_digest,
            "vocabulary_digest": registration.vocabulary_digest,
            "classifier_digest": registration.classifier_digest,
            "accepted_bucket_selectors": classification.accepted_bucket_selectors,
            "implementation_digest": implementation_digest,
            "effect_class": registration.effect_class,
            "local_execution": local,
            "secret_plan": ProviderSecretResolutionPlanV1(),
            "budget_translation": translation,
        }
        if isinstance(node, SourceNodeV4):
            capture_pin = _resolve_line_pin(node.capture_contract, slot_pins=slot_pins)
            occurrence = ProviderExternalOccurrencePlanV1.model_validate(
                {
                    **common,
                    "input_name": node.as_,
                    "capture_contract_digest": capture_pin.artifact_digest,
                    "source_runtime_plan_digest": typed_digest(
                        Sha256Value,
                        "playbill-source-runtime-plan-v1",
                        {"node_id": node.node_id, "request": node.request},
                    ).tagged,
                }
            )
        else:
            contract_in = _resolve_line_pin(node.contract_in, slot_pins=slot_pins)
            contract_out = _resolve_line_pin(node.contract_out, slot_pins=slot_pins)
            occurrence = ProviderExternalOccurrencePlanV1.model_validate(
                {
                    **common,
                    "contract_input_digest": contract_in.artifact_digest,
                    "contract_output_digest": contract_out.artifact_digest,
                }
            )
        occurrences.append(occurrence)
    return tuple(sorted(occurrences, key=lambda item: item.occurrence_path.encode("utf-8")))


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
        if isinstance(node, RepeatNodeV3 | RepeatNodeV4):
            unsupported_rows.extend(
                ProcedureUnsupportedNodeV1(
                    node_id=f"{node.node_id}.{body.node_id}",
                    kind=body.operation,
                )
                for body in node.body
                if body.operation != "transform"
            )
    slots = _required_slots(accepted.procedure)
    if slots and accepted.procedure.definition.graph_format == 4:
        unsupported_rows.append(
            ProcedureUnsupportedNodeV1(
                node_id="procedure",
                kind="graph_v4_line_closure_required",
            )
        )
    unsupported = tuple(unsupported_rows)
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
        definition_digest=accepted.procedure.definition_digest,
        state=state,
        required_slots=slots,
        unsupported_nodes=unsupported,
        next_operation=operation,
    )


def _graph_v3_external_occurrences(accepted: AcceptedProcedureV1) -> tuple[str, ...]:
    definition = accepted.procedure.definition
    if definition.graph_format != 3:
        return ()
    rows: list[str] = []
    for node in definition.nodes:
        if isinstance(node, SourceNodeV3 | ProviderNodeV3):
            rows.append(node.node_id)
        elif isinstance(node, RepeatNodeV3):
            rows.extend(
                f"{node.node_id}.{body.node_id}"
                for body in node.body
                if body.operation == "provider"
            )
    return tuple(rows)


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
    if accepted.procedure.definition.graph_format == 4:
        raise ProcedureBindingGraphV4LineClosureRequired(
            f"{ProcedureBindingGraphV4LineClosureRequired.code}: graph-v4 Provider slots "
            "are resolved only by accepted Line closure"
        )
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
        interface_digests[state.artifact_digest] = state.artifact_digest
        interface_pin = next(
            (pin for pin in state.pins if pin.role == "provider-interface"),
            None,
        )
        if interface_pin is not None:
            interface_path = provider_interface_path(interface_pin.target.name)
            interface_content = tree.get(interface_path)
            if interface_content is not None:
                registration = parse_provider_interface(interface_content, path=interface_path)
                if provider_interface_digest(registration).tagged == interface_pin.artifact_digest:
                    interface_digests[state.artifact_digest] = registration.interface_digest
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
        workspace_advertisement=proposal.workspace_advertisement,
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


def _journal_for_write(instance: PlaybillInstance) -> tuple[LocalJournalBackend, Path]:
    """Open the journal and recover append-window leases before the next write."""

    journal, root = _journal(instance)
    stream = _stream(instance)
    records = tuple(
        stored
        for partition_id in journal.partition_ids(stream)
        for stored in journal.all_records(stream, partition_id)
    )
    bodies = instance.body_store()
    ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
        records,
        bodies=bodies,
    )
    return journal, root


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
    records = tuple(
        item
        for partition_id in journal.partition_ids(_stream(instance))
        for item in journal.all_records(_stream(instance), partition_id)
        if item.record.run_id == run_id and item.record.admission_binding_digest is not None
    )
    partitions = {item.record.partition_id for item in records}
    admission_digests = {
        item.record.admission_binding_digest
        for item in records
        if item.record.admission_binding_digest is not None
    }
    if len(partitions) > 1 or len(admission_digests) > 1:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run id collides across journal authority"
        )
    return records


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
    admission: (
        ProcedureRunAdmissionV2
        | ProcedureRunAdmissionV3
        | ProcedureRunAdmissionV4
        | ProcedureRunAdmissionV5
        | None
    ) = None
    admission_count = 0
    admission_material_manifest = None
    admission_material_manifest_digest: str | None = None
    acquisition_plan = None
    acquisition_plan_digest: str | None = None
    final = None
    outcomes: list[ProcedureRunOutcomeV1] = []
    invocation_receipt_digests: list[str] = []
    source_capture_associations: tuple[ProcedureSourceCaptureAssociationV1, ...] = ()
    provider_invocations: dict[str, Literal["started", "completed"]] = {}
    derived_source_requests: dict[str, ProcedureDerivedSourceRequestV1] = {}
    produced_source_associations: list[ProcedureSourceCaptureAssociationV1] = []
    for stored in records:
        payload = parse_journal_payload(bodies.read(stored.record.payload_digest, access=access))
        if stored.record.event_kind == "admission_bound":
            admission_count += 1
            if isinstance(payload, dict) and payload.get("tag") == (
                "playbill-procedure-admission-bound-payload-v5"
            ):
                bound_v5 = ProcedureAdmissionBoundPayloadV5.model_validate(payload)
                admission = bound_v5.admission
                admission_material_manifest = bound_v5.admission_material_manifest
                admission_material_manifest_digest = bound_v5.admission_material_manifest_digest
                acquisition_plan = bound_v5.acquisition_plan
                acquisition_plan_digest = bound_v5.acquisition_plan_digest
            elif isinstance(payload, dict) and payload.get("tag") == (
                "playbill-procedure-admission-bound-payload-v4"
            ):
                bound_v4 = ProcedureAdmissionBoundPayloadV4.model_validate(payload)
                admission = bound_v4.admission
                admission_material_manifest = bound_v4.admission_material_manifest
                admission_material_manifest_digest = bound_v4.admission_material_manifest_digest
            elif isinstance(payload, dict) and payload.get("tag") == (
                "playbill-procedure-admission-bound-payload-v3"
            ):
                bound = ProcedureAdmissionBoundPayloadV3.model_validate(payload)
                admission = bound.admission
                admission_material_manifest = bound.admission_material_manifest
                admission_material_manifest_digest = bound.admission_material_manifest_digest
            elif isinstance(payload, dict) and payload.get("tag") == (
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
        if stored.record.event_kind == "source_request_derived":
            try:
                derived = ProcedureDerivedSourceRequestV1.model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: derived Source request is invalid"
                ) from exc
            if (
                derived.run_id != run_id
                or derived.admission_binding_digest != stored.record.admission_binding_digest
                or derived.occurrence_path in derived_source_requests
            ):
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: derived Source request does not bind "
                    "this run exactly"
                )
            derived_source_requests[derived.occurrence_path] = derived
        if stored.record.event_kind == "provider_invocation_started":
            try:
                started = ProviderInvocationStartedV1.model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider invocation start is invalid"
                ) from exc
            if started.invocation_id in provider_invocations:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider invocation start is duplicated"
                )
            planned = (
                ()
                if acquisition_plan is None
                else tuple(
                    item
                    for item in acquisition_plan.external_occurrences
                    if item.occurrence_path == started.occurrence_path
                )
            )
            if len(planned) != 1:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider start is not admitted exactly"
                )
            if planned[0].occurrence_kind == "source":
                derived_for_start = derived_source_requests.get(started.occurrence_path)
                if derived_for_start is None or started.input_digest != run_value_digest(
                    "provider-input", derived_for_start.request
                ):
                    raise ProcedureRunRecoveryRequired(
                        f"{ProcedureRunRecoveryRequired.code}: Source spawn lacks its exact "
                        "pre-spawn derived request result"
                    )
            provider_invocations[started.invocation_id] = "started"
        if stored.record.event_kind == "provider_invocation_completed":
            try:
                completed = ProviderInvocationCompletedV1.model_validate(payload)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider invocation receipt is invalid"
                ) from exc
            if provider_invocations.get(completed.invocation_id) != "started":
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider completion has no exact "
                    "unmatched durable start"
                )
            if (
                completed.receipt.run_id != run_id
                or completed.receipt.admission_binding_digest
                != stored.record.admission_binding_digest
            ):
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Provider completion names another run"
                )
            provider_invocations[completed.invocation_id] = "completed"
            invocation_receipt_digests.append(completed.receipt_digest)
        if stored.record.event_kind == "produced_capture" and isinstance(payload, dict):
            occurrence_path = payload.get("occurrence_path")
            invocation_receipt_digest = payload.get("invocation_receipt_digest")
            if occurrence_path is not None or invocation_receipt_digest is not None:
                try:
                    if not isinstance(occurrence_path, str) or not isinstance(
                        invocation_receipt_digest, str
                    ):
                        raise TypeError("Source Capture association is partial")
                    association = ProcedureSourceCaptureAssociationV1(
                        occurrence_path=occurrence_path,
                        invocation_receipt_digest=invocation_receipt_digest,
                        capture_digest=str(payload.get("capture_digest")),
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    raise ProcedureRunRecoveryRequired(
                        f"{ProcedureRunRecoveryRequired.code}: produced Source Capture is invalid"
                    ) from exc
                if association.invocation_receipt_digest not in invocation_receipt_digests:
                    raise ProcedureRunRecoveryRequired(
                        f"{ProcedureRunRecoveryRequired.code}: produced Source Capture precedes "
                        "its Provider completion"
                    )
                produced_source_associations.append(association)
    if admission is None:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run lacks a supported admission_bound"
        )
    if admission_count != 1:
        raise ProcedureRunRecoveryRequired(
            f"{ProcedureRunRecoveryRequired.code}: run must have one admission_bound"
        )
    for stored in records:
        record = stored.record
        if (
            record.stream != admission.journal_stream
            or record.partition_id != admission.journal_partition_id
            or record.accepted_coordinate != admission.accepted_coordinate
            or record.procedure_artifact_digest != admission.procedure_artifact_digest
            or record.definition_digest != admission.definition_digest
            or record.run_id != admission.run_id
            or record.line_spec_digest != admission.line_spec_digest
            or record.occurrence_id != admission.occurrence_id
            or record.attempt != admission.attempt
            or record.admission_binding_digest != admission.admission_binding_digest
            or record.actor_context != admission.actor_context
        ):
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: run record metadata disagrees with admission"
            )
    status: Literal[
        "running",
        "succeeded",
        "node_refused",
        "operational_failed",
        "internal_failed",
        "halted",
    ] = "running"
    result = None
    terminal: ProcedureTerminalV1 | None = None
    semantic_result_digest = None
    if isinstance(final, dict):
        try:
            raw_associations = final.get("source_capture_associations", [])
            if not isinstance(raw_associations, list):
                raise TypeError("Source Capture associations are not a list")
            source_capture_associations = tuple(
                ProcedureSourceCaptureAssociationV1.model_validate(item)
                for item in raw_associations
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: Source Capture associations are invalid"
            ) from exc
        if tuple(item.occurrence_path for item in source_capture_associations) != tuple(
            sorted(
                {item.occurrence_path for item in source_capture_associations},
                key=str.encode,
            )
        ) or any(
            item.invocation_receipt_digest not in invocation_receipt_digests
            for item in source_capture_associations
        ):
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: Source Capture associations do not "
                "match durable Provider receipts"
            )
        expected_source_associations = tuple(
            sorted(
                produced_source_associations,
                key=lambda item: item.occurrence_path.encode("utf-8"),
            )
        )
        if source_capture_associations != expected_source_associations:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: final Source Capture associations do not "
                "reproduce produced-Capture events"
            )
        raw_status = final.get("status")
        if raw_status not in {
            "succeeded",
            "refused",
            "failed",
            "budget_exhausted",
            "halted",
        }:
            raise ProcedureRunRecoveryRequired(
                f"{ProcedureRunRecoveryRequired.code}: final status is invalid"
            )
        status = (
            "succeeded"
            if raw_status == "succeeded"
            else "halted"
            if raw_status == "halted"
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
        try:
            if raw_status in {"refused", "budget_exhausted"}:
                raw_refusal = final.get("refusal")
                refusal = raw_refusal if isinstance(raw_refusal, dict) else {}
                raw_budget = refusal.get("budget")
                budget = (
                    ProcedureBudgetRefusalDetailV1.model_validate(raw_budget)
                    if isinstance(raw_budget, dict)
                    else None
                )
                refusal_code = str(refusal.get("code", "guard_refused"))
                raw_detail_code = refusal.get("detail_code")
                if refusal_code in get_args(ProcedureAdmissionRefusalCodeV1):
                    terminal = ProcedureAdmissionRefusalV1.model_validate(
                        {
                            "code": refusal_code,
                            "message": str(refusal.get("message", "Procedure admission refused.")),
                            "details": refusal.get("details", {}),
                        }
                    )
                elif refusal_code == "budget_max_items_exceeded":
                    terminal = ProcedureBudgetExhaustedV1(
                        node_id=str(refusal.get("node_id") or last_node_id),
                        journal_coordinate=_journal_coordinate(final_record),
                        details=ProcedureBudgetExceededDetailV1.model_validate(
                            refusal.get("details", {})
                        ),
                    )
                elif refusal_code in get_args(ProcedureSettlementRefusalCodeV1):
                    terminal = ProcedureSettlementRefusalV1.model_validate(
                        {
                            "code": refusal_code,
                            "message": str(refusal.get("message", "Procedure settlement refused.")),
                            "node_id": str(refusal.get("node_id") or last_node_id),
                            "journal_coordinate": _journal_coordinate(final_record),
                            "details": refusal.get("details", {}),
                            "retryable": bool(
                                cast(dict[str, object], refusal.get("details", {})).get(
                                    "retryable", False
                                )
                                if isinstance(refusal.get("details"), dict)
                                else False
                            ),
                        }
                    )
                else:
                    terminal = ProcedureNodeRefusalV1.model_validate(
                        {
                            "code": refusal_code,
                            "message": str(
                                refusal.get("message", "Procedure node refused execution.")
                            ),
                            "node_id": str(refusal.get("node_id") or last_node_id),
                            "journal_coordinate": _journal_coordinate(final_record),
                            "detail_code": (
                                raw_detail_code if isinstance(raw_detail_code, str) else None
                            ),
                            "details": refusal.get("details", {}),
                            "budget": budget,
                        }
                    )
            elif raw_status == "halted":
                raw_halt = final.get("halt")
                if not isinstance(raw_halt, dict):
                    raise ValueError("halted run lacks typed halt material")
                raw_reason = raw_halt.get("reason")
                terminal = ProcedureHaltTerminalV1(
                    node_id=str(raw_halt.get("node_id") or last_node_id),
                    reason=raw_reason if isinstance(raw_reason, str) else None,
                    journal_coordinate=_journal_coordinate(final_record),
                )
            elif raw_status == "failed":
                failure_code = final.get("failure_code")
                if failure_code in set(get_args(ProcedureOperationalFailureCodeV1)):
                    status = "operational_failed"
                    messages = {
                        "cas_unavailable_at_replay": (
                            "Admitted Procedure replay material is unavailable."
                        ),
                        "replay_material_mismatch": (
                            "Admitted Procedure replay material does not match its binding."
                        ),
                        "wall_clock_exhausted": (
                            "Procedure execution exceeded its wall-clock budget."
                        ),
                        "admission_material_unavailable_by_policy": (
                            "Admitted Procedure material is unavailable under its governed "
                            "retention policy."
                        ),
                        "replay_material_unavailable": (
                            "Required admitted Procedure replay material is unavailable."
                        ),
                        "admission_material_corrupt": (
                            "Admitted Procedure replay material fails its content address."
                        ),
                    }
                    terminal = ProcedureOperationalFailureV1.model_validate(
                        {
                            "code": failure_code,
                            "message": messages.get(
                                str(failure_code),
                                "Provider execution failed for an operational reason.",
                            ),
                            "journal_coordinate": _journal_coordinate(final_record),
                            "details": final.get("failure_details", {}),
                        }
                    )
                elif failure_code in set(get_args(ProcedureInternalFailureCodeV1)):
                    terminal = ProcedureInternalFailureV1.model_validate(
                        {
                            "code": failure_code,
                            "message": (
                                "Provider execution violated an internal integrity contract."
                            ),
                            "correlation_id": run_id,
                            "journal_coordinate": _journal_coordinate(final_record),
                        }
                    )
                else:
                    terminal = ProcedureInternalFailureV1(
                        code="unexpected_exception",
                        message="Procedure execution failed unexpectedly; inspect daemon logs.",
                        correlation_id=run_id,
                        journal_coordinate=_journal_coordinate(final_record),
                        repair=hand_edit_repair("unexpected_exception"),
                    )
        except (ValidationError, TypeError, ValueError):
            status = "internal_failed"
            result = None
            semantic_result_digest = None
            terminal = ProcedureInternalFailureV1(
                code="run_record_invalid",
                message="Procedure run record is invalid; inspect daemon logs.",
                correlation_id=run_id,
                journal_coordinate=_journal_coordinate(final_record),
                repair=hand_edit_repair("run_record_invalid"),
            )
    attribution = ProcedureRunAttributionV1(
        actor_type=admission.actor_context.actor_type,
        actor_id=admission.actor_context.actor_id,
        org_id=admission.actor_context.org_id,
        operation_id=admission.actor_context.operation_id,
        request_id=admission.actor_context.request_id,
        recorded_time=admission.actor_context.timestamp,
    )
    public_receipt: (
        ProcedureRunReceiptV2
        | ProcedureRunReceiptV3
        | ProcedureRunReceiptV4
        | ProcedureRunReceiptV5
        | ProcedureRunReceiptV6
        | None
    ) = None
    receipt_digest = None
    if final is not None:
        stream = records[0].record.stream
        receipt_fields = {
            "run_id": run_id,
            "admission_binding_digest": admission.admission_binding_digest,
            "semantic_replay_key_digest": admission.semantic_replay_key_digest,
            "semantic_result_digest": semantic_result_digest,
            "bound_coordinate": admission.bound_coordinate,
            "head_at_admission": admission.head_at_admission,
            "lane": admission.lane,
            "evaluation_time": admission.admitted_at,
            "validated_pins": admission.full_pins,
            "admitted_inputs": tuple(
                cast(dict[str, object], item.model_dump(mode="json"))
                for item in admission.accepted_state_inputs
            ),
            "attribution": attribution,
            "stream_instance_id": stream.instance_id,
            "journal_family": stream.journal_family,
            "stream_id": stream.stream_id,
            "partition_id": records[0].record.partition_id,
            "first_sequence": records[0].record.sequence,
            "last_sequence": records[-1].record.sequence,
            "record_digests": tuple(item.record_digest for item in records),
            "chain_head_digest": records[-1].record_digest,
        }
        raw_budget_block = final.get("budget") if isinstance(final, dict) else None
        if (
            isinstance(raw_budget_block, dict)
            and isinstance(admission, ProcedureRunAdmissionV3)
            and admission_material_manifest is not None
        ):
            required_line_fields = (
                admission.line_spec_digest,
                admission.occurrence_id,
                admission.deployment_snapshot_digest,
                admission.acquisition_policy_digest,
                admission.sensitivity_policy_digest,
                admission.mandate_coordinate_digest,
                admission.calibration_coordinate_digest,
                admission_material_manifest_digest,
            )
            if any(value is None for value in required_line_fields):
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: Line admission is incomplete"
                )
            parsed_budget = ProcedureRunBudgetV1.model_validate(raw_budget_block)
            shared_line_fields = dict(
                **{
                    **receipt_fields,
                    "admitted_inputs": tuple(
                        cast(dict[str, object], item.model_dump(mode="json"))
                        for item in admission.run_inputs
                    ),
                },
                status=cast(
                    Literal[
                        "succeeded",
                        "node_refused",
                        "operational_failed",
                        "internal_failed",
                        "halted",
                    ],
                    status,
                ),
                terminal=terminal,
                invocation_origin="line",
                line_identity=admission.line_identity,
                line_spec_digest=cast(str, admission.line_spec_digest),
                occurrence_id=cast(str, admission.occurrence_id),
                occurrence_evaluation_time=admission.occurrence_evaluation_time,
                node_pin_sets=tuple(
                    ProcedureRunNodePinSetV1(node_id=item.node_id, pins=item.pins)
                    for item in admission.node_pin_sets
                ),
                pin_set_digest=admission.pin_set_digest,
                replay_input_vector=tuple(
                    ProcedureReplayInputProjectionV1.model_validate(item.model_dump(mode="json"))
                    for item in procedure_replay_input_vector(admission)
                ),
                deployment_snapshot_digest=cast(str, admission.deployment_snapshot_digest),
                acquisition_policy_digest=cast(str, admission.acquisition_policy_digest),
                selection_receipt_digest=admission.selection_receipt_digest,
                selection_decision=ProcedureSelectionDecisionV1.model_validate(
                    admission.selection_decision.model_dump(mode="json")
                ),
                selection_decision_digest=admission.selection_decision_digest,
                sensitivity_policy_digest=cast(str, admission.sensitivity_policy_digest),
                mandate_coordinate_digest=cast(str, admission.mandate_coordinate_digest),
                calibration_coordinate_digest=cast(str, admission.calibration_coordinate_digest),
                taint_labels=admission.taint_labels,
                epsilon_member=admission.epsilon_member,
                admission_material_manifest=(
                    ProcedureAdmissionMaterialManifestV1.model_validate(
                        admission_material_manifest.model_dump(mode="json")
                    )
                ),
                admission_material_manifest_digest=cast(str, admission_material_manifest_digest),
                budget=ProcedureRunBudgetV2(
                    declared=ProcedureRunBudgetDeclaredV2(
                        budget=admission.budget,
                        hard_caps=admission.hard_caps,
                        result_bytes_cap=parsed_budget.declared.result_bytes_cap,
                        provider_output_bytes_cap=admission.provider_output_bytes_cap,
                    ),
                    observed=parsed_budget.observed,
                ),
            )
            if isinstance(admission, ProcedureRunAdmissionV5):
                if acquisition_plan is None or acquisition_plan_digest is None:
                    raise ProcedureRunRecoveryRequired(
                        f"{ProcedureRunRecoveryRequired.code}: v5 acquisition plan is absent"
                    )
                public_receipt = ProcedureRunReceiptV6(
                    **shared_line_fields,
                    resolved_provider_bindings=tuple(
                        ProcedureProviderBindingV2.model_validate(item.model_dump(mode="json"))
                        for item in admission.resolved_provider_bindings
                    ),
                    acquisition_plan_digest=acquisition_plan_digest,
                    exhaust_access_binding_digest=(admission.exhaust_access_binding_digest),
                    invocation_receipt_digests=tuple(invocation_receipt_digests),
                    source_capture_associations=source_capture_associations,
                )
            elif isinstance(admission, ProcedureRunAdmissionV4):
                public_receipt = ProcedureRunReceiptV5(
                    **shared_line_fields,
                    resolved_provider_bindings=tuple(
                        ProcedureProviderBindingV2.model_validate(item.model_dump(mode="json"))
                        for item in admission.resolved_provider_bindings
                    ),
                )
            else:
                public_receipt = ProcedureRunReceiptV4(
                    **shared_line_fields,
                    resolved_provider_bindings=tuple(
                        ProcedureProviderBindingV1.model_validate(item.model_dump(mode="json"))
                        for item in admission.resolved_provider_bindings
                    ),
                )
            receipt_domain = (
                PROCEDURE_RUN_RECEIPT_V6_DOMAIN
                if isinstance(admission, ProcedureRunAdmissionV5)
                else PROCEDURE_RUN_RECEIPT_V5_DOMAIN
                if isinstance(admission, ProcedureRunAdmissionV4)
                else PROCEDURE_RUN_RECEIPT_V4_DOMAIN
            )
        elif isinstance(raw_budget_block, dict):
            public_receipt = ProcedureRunReceiptV3(
                **receipt_fields,
                status=cast(
                    Literal[
                        "succeeded",
                        "node_refused",
                        "operational_failed",
                        "internal_failed",
                        "halted",
                    ],
                    status,
                ),
                terminal=terminal,
                budget=ProcedureRunBudgetV1.model_validate(raw_budget_block),
            )
            receipt_domain = PROCEDURE_RUN_RECEIPT_V3_DOMAIN
        else:
            public_receipt = ProcedureRunReceiptV2(**receipt_fields)
            receipt_domain = PROCEDURE_RUN_RECEIPT_V2_DOMAIN
        receipt_digest = typed_digest(
            Sha256Value,
            receipt_domain,
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
    provider_runtime_operator: ProviderRuntimeOperatorProtocol | None = None,
    workspace_file_reader: WorkspaceFileReader | None = None,
) -> ProcedureRunStateV2:
    coordinate = _resolve_coordinate(instance, request.at)
    evaluation_time = request.evaluation_time or instance.accepted_evaluation_time(
        coordinate.git_oid
    )
    head_at_admission = instance.accepted_coordinate()
    # ``at`` binds a live invocation to an accepted coordinate. Exact replay is
    # addressed only by run_id through service_get_playbill_procedure_run.
    lane: Literal["current", "replay"] = "current"
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
                repair=hand_edit_repair("binding_required"),
            ),
        )
    if readiness.state == "unsupported":
        legacy_external = _graph_v3_external_occurrences(accepted)
        refusal_code: Literal[
            "unsupported_node",
            "provider_explicit_implementation_required",
        ] = "unsupported_node"
        refusal_message = "Procedure contains node kinds unavailable on the served run lane."
        if legacy_external:
            refusal_code = "provider_explicit_implementation_required"
            refusal_message = (
                "Graph-v3 Source/Provider occurrences require explicit graph-v4 "
                "implementation pins for live invocation."
            )
        elif any(
            item.kind == "graph_v4_line_closure_required" for item in readiness.unsupported_nodes
        ):
            refusal_message = (
                "Graph-v4 Provider slots require accepted Line closure before execution."
            )
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
                code=refusal_code,
                message=refusal_message,
                details={
                    "unsupported_nodes": [
                        item.model_dump(mode="json") for item in readiness.unsupported_nodes
                    ],
                    "legacy_external_occurrences": list(legacy_external),
                },
                repair=hand_edit_repair(refusal_code),
            ),
        )
    current_digest = _CurrentProcedureAuthority(instance).current_procedure_digest(
        accepted.procedure.identity,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
    )
    if current_digest != accepted.artifact_digest:
        raise ProcedureRunNotCurrent(
            f"{ProcedureRunNotCurrent.code}: Procedure is not current before journal creation"
        )
    stream = _stream(instance)
    journal, root = _journal_for_write(instance)
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
    if request.at is None and instance.accepted_coordinate() != coordinate:
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
            provider_runtime_invoker_factory=(
                None
                if provider_runtime_operator is None
                else lambda: provider_runtime_operator.invoker_for(
                    instance,
                    accepted_oid=coordinate.git_oid,
                )
            ),
            workspace_file_reader=workspace_file_reader,
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


def _line_refusal_state(
    accepted: AcceptedProcedureV1,
    accepted_line: AcceptedLineSpecV1,
    *,
    coordinate: AcceptedProjectionCoordinate,
    head_at_admission: AcceptedProjectionCoordinate,
    evaluation_time: datetime,
    code: str,
    message: str,
    details: object,
) -> ProcedureRunStateV2:
    return ProcedureRunStateV2(
        run_id=None,
        procedure_identity=accepted.procedure.identity,
        procedure_artifact_digest=accepted.artifact_digest,
        bound_coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
        head_at_admission=PlaybillAcceptedCoordinate.from_internal(head_at_admission),
        lane="current",
        evaluation_time=evaluation_time,
        status="admission_refused",
        pending_inputs=(),
        outcomes=(),
        next_operation=ProcedureNextOperationV1(kind="terminal"),
        terminal=ProcedureAdmissionRefusalV1.model_validate(
            {
                "code": code,
                "message": message,
                "details": {
                    "line_identity_digest": line_identity_digest(accepted_line.line.identity),
                    **(details if isinstance(details, dict) else {"detail": details}),
                },
            }
        ),
    )


def _accepted_line_mandates(
    tree: Mapping[str, bytes],
    accepted: AcceptedProcedureV1,
    *,
    evaluation_time: datetime,
) -> tuple[tuple[str, ProcedureMandateV1], ...]:
    result: list[tuple[str, ProcedureMandateV1]] = []
    for path, content in tree.items():
        if not path.startswith("procedure-mandates/") or not path.endswith((".json", ".yaml")):
            continue
        mandate = parse_procedure_mandate(content, path=path)
        if (
            mandate.lifecycle.state == "live"
            and mandate.procedure.target == accepted.procedure.identity
            and mandate.procedure.artifact_digest == accepted.artifact_digest
            and mandate.valid_from <= evaluation_time < mandate.expires_at
        ):
            result.append((procedure_mandate_digest(mandate).tagged, mandate))
    return tuple(sorted(result, key=lambda item: item[0].encode("ascii")))


def service_run_playbill_line(
    instance: PlaybillInstance,
    *,
    path_identity_digest: str,
    request: LineRunRequestV1,
    actor_context: GovernedActorContext,
    caller_rung: int,
    provider_runtime_operator: ProviderRuntimeOperatorProtocol | None = None,
) -> ProcedureRunStateV2:
    """Derive, admit, and execute one occurrence of an accepted Line."""

    if request.line_identity_digest != path_identity_digest:
        raise LineRunIdentityMismatch(
            f"{LineRunIdentityMismatch.code}: route and request Line identities differ"
        )
    coordinate = instance.accepted_coordinate()
    head_at_admission = coordinate
    tree = instance.tree_at(coordinate.git_oid)
    accepted_line = _accepted_line_by_identity_digest(
        tree,
        identity_digest=path_identity_digest,
    )
    accepted = _accepted_procedure(
        instance,
        name=accepted_line.line.procedure.target.name,
        coordinate=coordinate,
    )
    if accepted.artifact_digest != accepted_line.line.procedure.artifact_digest:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="line_closure_incomplete",
            message="The accepted Line binds another Procedure artifact.",
            details={
                "repair": "Author and accept a Line successor with the current Procedure pin."
            },
        )
    try:
        _assert_line_closure_complete(tree, accepted_line)
    except PlaybillExecutionError as exc:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="line_closure_incomplete",
            message=str(exc),
            details={"repair": "Restore or succeed the missing accepted closure member."},
        )
    providers, interfaces = _line_catalogs(tree)
    interface_digests = {
        provider_digest_value: implementation.interface_digest
        for provider_digest_value, provider in providers.items()
        if isinstance(provider.provider, ProviderV2)
        for implementation in provider.provider.implementations[:1]
    }
    law = evaluate_line_spec_law(
        accepted_line.line,
        path=accepted_line.path,
        procedure=accepted,
        interface_digests=interface_digests,
        predecessor=None,
        providers=providers,
        provider_interfaces=interfaces,
    )
    if law.verdict != "accepted":
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="artifact_binding_mismatch",
            message="The accepted Line no longer reproduces its complete Procedure closure.",
            details={
                "diagnostics": [item.model_dump(mode="json") for item in law.diagnostics],
                "repair": "Author and accept a complete Line successor.",
            },
        )
    mandates = _accepted_line_mandates(
        tree,
        accepted,
        evaluation_time=request.evaluation_time,
    )
    if not mandates:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="line_mandate_required",
            message="The Line's bound Procedure has no current accepted ProcedureMandate.",
            details={
                "repair": "Author and accept a ProcedureMandate pinning this exact Procedure."
            },
        )
    prior = _line_admissions(instance, accepted_line)
    occurrence_id, next_due, awaited = _line_occurrence(
        tree,
        accepted_line,
        coordinate=coordinate,
        evaluation_time=request.evaluation_time,
        prior=prior,
    )
    if request.occurrence_id is not None and request.occurrence_id != occurrence_id:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="occurrence_id_mismatch",
            message="The asserted occurrence id differs from the daemon-derived occurrence.",
            details={"derived_occurrence_id": occurrence_id, "repair": "Retry with this id."},
        )
    if next_due is not None and request.evaluation_time < next_due:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="occurrence_not_due",
            message="The next scheduled Line occurrence is not due.",
            details={
                "next_due": format_datetime(next_due),
                "repair": "Re-run at or after next_due.",
            },
        )
    if awaited is not None:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="occurrence_not_due",
            message="The Line is waiting for a newer capture landing or trigger policy event.",
            details={"awaited_source": awaited, "repair": "Retry after the awaited source lands."},
        )
    if any(item.occurrence_id == occurrence_id for item in prior):
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="occurrence_already_admitted",
            message="This daemon-derived Line occurrence is already admitted.",
            details={"occurrence_id": occurrence_id, "repair": "Read the existing run state."},
        )
    if any(isinstance(node, ExhaustTapNodeV3) for node in accepted.procedure.definition.nodes):
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="exhaust_binding_carrier_required",
            message="This Line requires an opaque Exhaust access-binding carrier.",
            details={"repair": "Trigger through a carrier-aware Line scheduler."},
        )
    if accepted_line.line.acquisition_policy is None:
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code="artifact_binding_mismatch",
            message="Served Line execution requires an accepted acquisition-policy pin.",
            details={"repair": "Accept a Line successor with an acquisition policy."},
        )
    runtime_policy = resolve_procedure_runtime_policy(tree)
    slot_pins = _line_slot_pins(accepted_line)
    budget = _line_budget(accepted_line, accepted)
    try:
        external_occurrences = _line_external_occurrences(
            accepted_line,
            accepted,
            providers=providers,
            interfaces=interfaces,
            slot_pins=slot_pins,
            provider_runtime_operator=provider_runtime_operator,
            runtime_policy=runtime_policy,
            budget=budget,
        )
    except ProviderLocalRuntimeRefused as exc:
        return ProcedureRunStateV2(
            run_id=None,
            procedure_identity=accepted.procedure.identity,
            procedure_artifact_digest=accepted.artifact_digest,
            bound_coordinate=PlaybillAcceptedCoordinate.from_internal(coordinate),
            head_at_admission=PlaybillAcceptedCoordinate.from_internal(head_at_admission),
            lane="current",
            evaluation_time=request.evaluation_time,
            status="node_refused",
            pending_inputs=(),
            outcomes=(),
            next_operation=ProcedureNextOperationV1(kind="terminal"),
            terminal=ProcedureNodeRefusalV1(
                code="provider_unavailable",
                message="The daemon Provider lane cannot admit this Line occurrence.",
                node_id="line-admission",
                details={"reason": {"code": exc.code, "detail": str(exc)}, **exc.details},
                retryable=True,
                repair=hand_edit_repair("provider_unavailable"),
            ),
        )
    selection = ProcedureSelectionDecisionV1(
        policy_digest=accepted_line.line.acquisition_policy.artifact_digest,
        verdict="selected",
        decisions=(),
    )
    selection_digest = procedure_selection_decision_digest(selection)
    accepted_coordinate = AcceptedCoordinate.from_internal(coordinate)
    plan = ProcedureAcquisitionPlanV2(
        accepted_coordinate=accepted_coordinate,
        line_identity=accepted_line.line.identity,
        line_spec_digest=accepted_line.artifact_digest,
        occurrence_id=occurrence_id,
        occurrence_evaluation_time=request.evaluation_time,
        acquisition_policy_format="playbill-source-acquisition-policy-v1",
        acquisition_policy_digest=accepted_line.line.acquisition_policy.artifact_digest,
        selection_decision=selection,
        selection_decision_digest=selection_digest,
        external_occurrences=external_occurrences,
    )
    plan_digest = procedure_acquisition_plan_digest(plan)
    materials = _line_state_materials(
        instance,
        accepted,
        coordinate=coordinate,
        evaluation_time=request.evaluation_time,
        slot_pins=slot_pins,
    )
    full_pins = close_procedure_pin_slots(
        accepted.procedure,
        bindings=accepted_line.line.slot_bindings,
        interface_digests=interface_digests,
    ).exact_pins
    node_pin_sets = procedure_node_pin_sets(accepted, slot_pins)
    mandate_coordinate_digest = typed_digest(
        Sha256Value,
        "playbill-line-mandate-coordinate-v1",
        {
            "accepted_coordinate": accepted_coordinate.model_dump(mode="json"),
            "mandates": [digest for digest, _mandate in mandates],
        },
    ).tagged
    sensitivity_policy_digest = typed_digest(
        Sha256Value,
        "playbill-line-sensitivity-coordinate-v1",
        {"line_spec_digest": accepted_line.artifact_digest},
    ).tagged
    calibration_coordinate_digest = typed_digest(
        Sha256Value,
        "playbill-line-calibration-coordinate-v1",
        {"accepted_coordinate": accepted_coordinate.model_dump(mode="json")},
    ).tagged
    deployment_snapshot_digest = typed_digest(
        Sha256Value,
        "playbill-provider-deployment-snapshot-v1",
        {
            "bindings": [
                item.local_execution.model_dump(mode="json") for item in external_occurrences
            ]
        },
    ).tagged
    bindings = tuple(
        ProcedureProviderBindingV2(
            node_id=item.node_id,
            provider_artifact_digest=item.provider_artifact_digest,
            classification_plan=ProviderBucketClassificationPlanV1(
                node_id=item.node_id,
                interface_artifact_digest=item.interface_artifact_digest,
                interface_digest=item.interface_digest,
                vocabulary_digest=item.vocabulary_digest,
                classifier_digest=item.classifier_digest,
                accepted_bucket_selectors=item.accepted_bucket_selectors,
            ),
            implementation_digest=item.implementation_digest,
            effect_class=item.effect_class,
            secret_binding_identity_digests=item.secret_plan.binding_identity_digests,
        )
        for item in external_occurrences
    )
    fields = {
        "instance_id": instance.descriptor.instance_id,
        "run_id": "RUN-" + "0" * 64,
        "attempt": 1,
        "accepted_coordinate": accepted_coordinate,
        "procedure_identity": accepted.procedure.identity,
        "procedure_path": accepted.path,
        "procedure_artifact_digest": accepted.artifact_digest,
        "definition_digest": accepted.procedure.definition_digest,
        "activation_policy": accepted.procedure.activation_policy,
        "full_pins": full_pins,
        "node_pin_sets": node_pin_sets,
        "pin_set_digest": procedure_pin_set_digest(full_pins, node_pin_sets),
        "invocation_input": accepted_line.line.parameters,
        "accepted_state_inputs": tuple(item.input for item in materials),
        "landed_capture_inputs": (),
        "exhaust_inputs": (),
        "budget": budget,
        "hard_caps": accepted.procedure.definition.hard_caps,
        "actor_context": actor_context.model_copy(update={"timestamp": request.evaluation_time}),
        "invocation_origin": "line",
        "journal_stream": procedure_line_journal_stream(instance.descriptor.instance_id),
        "journal_partition_id": procedure_line_partition(accepted_line.line.identity),
        "line_spec_digest": accepted_line.artifact_digest,
        "occurrence_id": occurrence_id,
        "deployment_snapshot_digest": deployment_snapshot_digest,
        "acquisition_policy_digest": accepted_line.line.acquisition_policy.artifact_digest,
        "selection_receipt_digest": None,
        "sensitivity_policy_digest": sensitivity_policy_digest,
        "mandate_coordinate_digest": mandate_coordinate_digest,
        "calibration_coordinate_digest": calibration_coordinate_digest,
        "taint_labels": (),
        "epsilon_member": False,
        "admitted_at": request.evaluation_time,
        "admission_binding_digest": "sha256:" + "0" * 64,
        "bound_coordinate": accepted_coordinate,
        "head_at_admission": AcceptedCoordinate.from_internal(head_at_admission),
        "lane": "current",
        "semantic_replay_key_digest": "sha256:" + "0" * 64,
        "line_identity": accepted_line.line.identity,
        "occurrence_evaluation_time": request.evaluation_time,
        "resolved_provider_bindings": bindings,
        "selection_decision": selection,
        "selection_decision_digest": selection_digest,
        "provider_output_bytes_cap": runtime_policy.provider_output_bytes_cap,
        "acquisition_plan_digest": plan_digest,
        "exhaust_access_binding_digest": None,
    }
    provisional = ProcedureRunAdmissionV5.model_construct(**cast(dict[str, Any], fields))
    provisional = provisional.model_copy(
        update={"semantic_replay_key_digest": procedure_semantic_replay_key_digest(provisional)}
    )
    admission_digest = procedure_admission_digest(provisional)
    admission = ProcedureRunAdmissionV5.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "admission_binding_digest": admission_digest,
            "run_id": procedure_line_run_id(
                occurrence_id=occurrence_id,
                attempt=1,
                admission_binding_digest=admission_digest,
                occurrence_evaluation_time=request.evaluation_time,
            ),
        }
    )
    prepared_admission = service_prepare_playbill_line_admission(
        instance,
        admission=admission,
        accepted_line=accepted_line,
    )
    if isinstance(prepared_admission, ProcedureAdmissionRefusalV1):
        return _line_refusal_state(
            accepted,
            accepted_line,
            coordinate=coordinate,
            head_at_admission=head_at_admission,
            evaluation_time=request.evaluation_time,
            code=prepared_admission.code,
            message=prepared_admission.message,
            details=prepared_admission.details,
        )
    if not isinstance(prepared_admission, ProcedureRunAdmissionV5):
        raise PlaybillExecutionError("Line admission unexpectedly changed wire generation")
    manifest = ProcedureAdmissionMaterialManifestV1(members=())
    prepared = PreparedProcedureRunV5(
        admission=prepared_admission,
        accepted_state_materials=materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=procedure_admission_material_digest(manifest),
        acquisition_plan=plan,
        acquisition_plan_digest=plan_digest,
    )
    mandate_rung = max(caller_rung, *(mandate.rung for _digest, mandate in mandates))
    effective_rung = compute_effective_rung(
        procedure_terminal_capability=accepted.procedure.definition.terminal_capability,
        requested_terminal_rung=accepted_line.line.requested_terminal_rung,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=request.evaluation_time,
        procedure_definition_digest=accepted.procedure.definition_digest,
        line_spec_digest=accepted_line.artifact_digest,
        sensitivity_policy_digest=sensitivity_policy_digest,
        mandate_coordinate_digest=mandate_coordinate_digest,
        calibration_coordinate_digest=calibration_coordinate_digest,
        procedure_mandate_rung=mandate_rung,
    )
    journal, root = _journal_for_write(instance)
    _activate_writer(
        journal,
        prepared_admission.journal_stream,
        prepared_admission.journal_partition_id,
    )
    if instance.accepted_coordinate() != coordinate:
        raise ProcedureRunNotCurrent(
            f"{ProcedureRunNotCurrent.code}: accepted coordinate advanced before Line append"
        )
    result = service_execute_direct_procedure(
        prepared,
        accepted,
        journal=journal,
        bodies=instance.body_store(),
        run_index_path=root / "procedure-run-index.sqlite",
        fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
        activation_authority=_CurrentProcedureAuthority(instance),
        provider_runtime_invoker_factory=(
            None
            if provider_runtime_operator is None
            else lambda: provider_runtime_operator.invoker_for(
                instance,
                accepted_oid=coordinate.git_oid,
            )
        ),
        slot_pins=slot_pins,
        effective_rung=effective_rung,
        clock=_DeterministicClock(request.evaluation_time),
    )
    return _state_from_records(
        instance,
        run_id=prepared_admission.run_id,
        receipt=result.receipt,
    )


def service_get_playbill_procedure_run(
    instance: PlaybillInstance,
    *,
    run_id: str,
) -> ProcedureRunStateV2:
    return _state_from_records(instance, run_id=run_id)


def service_recover_provider_invocations(
    instance: PlaybillInstance,
    *,
    invocation_ids: tuple[str, ...],
    recovery_failure_codes: Mapping[str, str] | None = None,
    recorded_at: datetime,
) -> tuple[str, ...]:
    """Close exact durable starts whose child groups were recovered at startup."""

    failures = {} if recovery_failure_codes is None else dict(recovery_failure_codes)
    if not invocation_ids and not failures:
        return ()
    wanted = set(invocation_ids)
    observed_failures: list[tuple[str, str, str]] = []
    journal, _root = _journal_for_write(instance)
    stream = _stream(instance)
    bodies = instance.body_store()
    access = BodyAccessContext(principal_id="provider-recovery", can_read_body=True)
    recovered: list[str] = []
    handled: set[str] = set()
    for partition_id in journal.partition_ids(stream):
        records = journal.all_records(stream, partition_id)
        admission: ProcedureRunAdmissionV5 | None = None
        plan = None
        starts: dict[str, ProviderInvocationStartedV1] = {}
        completed: dict[str, ProviderInvocationCompletedV1] = {}
        for stored in records:
            payload = parse_journal_payload(
                bodies.read(stored.record.payload_digest, access=access)
            )
            if stored.record.event_kind == "admission_bound" and isinstance(payload, dict):
                if payload.get("tag") == "playbill-procedure-admission-bound-payload-v5":
                    bound = ProcedureAdmissionBoundPayloadV5.model_validate(payload)
                    admission = bound.admission
                    plan = bound.acquisition_plan
            elif stored.record.event_kind == "provider_invocation_started":
                started = ProviderInvocationStartedV1.model_validate(payload)
                starts[started.invocation_id] = started
            elif stored.record.event_kind == "provider_invocation_completed":
                parsed_completion = ProviderInvocationCompletedV1.model_validate(payload)
                completed[parsed_completion.invocation_id] = parsed_completion
        if admission is None or plan is None:
            continue
        handled.update(wanted & set(completed))
        unresolved_ids = set(starts) - set(completed)
        failed_pending_ids = tuple(sorted(set(failures) & unresolved_ids, key=str.encode))
        for invocation_id in failed_pending_ids:
            observed_failures.append((admission.run_id, invocation_id, failures[invocation_id]))
        if failed_pending_ids:
            # One possibly-live process keeps the whole attempt recovery-required;
            # do not partially complete or terminalize its sibling invocations.
            continue
        pending_ids = tuple(sorted(wanted & unresolved_ids, key=str.encode))
        if not pending_ids:
            continue
        resolved_occurrences = []
        for invocation_id in pending_ids:
            started = starts[invocation_id]
            occurrences = tuple(
                item
                for item in plan.external_occurrences
                if item.occurrence_path == started.occurrence_path
                and item.implementation_digest == started.implementation_digest
                and item.local_execution.materialization_digest == started.materialization_digest
            )
            if len(occurrences) != 1:
                raise ProcedureRunRecoveryRequired(
                    f"{ProcedureRunRecoveryRequired.code}: run {admission.run_id} recovered "
                    "Provider start has no exact admitted occurrence"
                )
            resolved_occurrences.append((invocation_id, started, occurrences[0]))
        writer = ProcedureExhaustWriter(
            journal=journal,
            bodies=bodies,
            fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
        )
        writer_state = journal.writer_state(stream, partition_id)
        if (
            writer_state is not None
            and writer_state.active
            and writer_state.fencing_token != PROCEDURE_RUN_FENCING_TOKEN
        ):
            journal.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=writer_state.fencing_token,
            )
        _activate_writer(journal, stream, partition_id)
        try:
            for invocation_id, started, occurrence in resolved_occurrences:
                outcome = map_provider_refusal(
                    "provider_process_group_survived_recovery",
                    message="Daemon startup terminated an incomplete Provider process group.",
                    detail={},
                )
                assert isinstance(outcome, ProviderInvocationOutcomeV1)
                declared = tuple(
                    endpoint
                    for endpoint in occurrence.local_execution.declared_endpoints
                    if not endpoint.startswith("dynamic:")
                )
                dynamic = cast(
                    tuple[Literal["dynamic:target-from-run-input"], ...],
                    tuple(
                        endpoint
                        for endpoint in occurrence.local_execution.declared_endpoints
                        if endpoint == "dynamic:target-from-run-input"
                    ),
                )
                receipt = ProviderInvocationReceiptV1(
                    invocation_id=invocation_id,
                    occurrence_path=occurrence.occurrence_path,
                    run_id=admission.run_id,
                    admission_binding_digest=admission.admission_binding_digest,
                    provider_artifact_digest=occurrence.local_execution.provider_artifact_digest,
                    implementation_digest=occurrence.local_execution.implementation_digest,
                    materialization_digest=occurrence.local_execution.materialization_digest,
                    deployment_digest=occurrence.local_execution.deployment_digest,
                    interface_id=occurrence.local_execution.interface_id,
                    interface_digest=occurrence.local_execution.interface_digest,
                    protocol_version=occurrence.local_execution.protocol_version,
                    input_bucket=started.input_bucket,
                    capture_contract_digest=occurrence.capture_contract_digest,
                    input_digest=started.input_digest,
                    outcome=outcome,
                    egress=ProviderEgressObservationV1(
                        declared_endpoints=declared,
                        observed_endpoints=(),
                        dynamic_endpoint_forms=dynamic,
                        observer_backend="child-self-report",
                        observer_grade="attribution",
                    ),
                    fence_scope="process_group+descendant_sweep",
                    secret_references=tuple(
                        sorted(
                            (
                                ProviderSecretReceiptReferenceV1(
                                    binding_identity_digest=(
                                        provider_secret_binding_identity_digest(
                                            ProviderSecretBindingIdentityV1(
                                                realm=reference.realm,
                                                name=reference.name,
                                            )
                                        )
                                    ),
                                    purpose=reference.purpose,
                                )
                                for reference in occurrence.secret_plan.references
                            ),
                            key=lambda item: item.binding_identity_digest.encode("ascii"),
                        )
                    ),
                    budget_translation=occurrence.budget_translation,
                    duration_microseconds=0,
                    trace={},
                    stderr="",
                )
                completion = ProviderInvocationCompletedV1(
                    invocation_id=invocation_id,
                    receipt=receipt,
                    receipt_digest=provider_invocation_receipt_digest(receipt),
                )
                writer.append(
                    stream=stream,
                    partition_id=partition_id,
                    event_kind="provider_invocation_completed",
                    accepted_coordinate=admission.accepted_coordinate,
                    procedure_artifact_digest=admission.procedure_artifact_digest,
                    definition_digest=admission.definition_digest,
                    run_id=admission.run_id,
                    line_spec_digest=admission.line_spec_digest,
                    occurrence_id=admission.occurrence_id,
                    attempt=admission.attempt,
                    admission_binding_digest=admission.admission_binding_digest,
                    actor_context=admission.actor_context,
                    recorded_at=recorded_at,
                    payload=completion.model_dump(mode="json"),
                )
                completed[invocation_id] = completion
                recovered.append(invocation_id)
                handled.add(invocation_id)
            ordered_completions = tuple(completed[item] for item in starts if item in completed)
            provider_calls = len(ordered_completions)
            invocation_receipt_digests = tuple(item.receipt_digest for item in ordered_completions)
            wall_clock_microseconds = sum(
                item.receipt.duration_microseconds for item in ordered_completions
            )
            writer.append(
                stream=stream,
                partition_id=partition_id,
                event_kind="attempt_finalized",
                accepted_coordinate=admission.accepted_coordinate,
                procedure_artifact_digest=admission.procedure_artifact_digest,
                definition_digest=admission.definition_digest,
                run_id=admission.run_id,
                line_spec_digest=admission.line_spec_digest,
                occurrence_id=admission.occurrence_id,
                attempt=admission.attempt,
                admission_binding_digest=admission.admission_binding_digest,
                actor_context=admission.actor_context,
                recorded_at=recorded_at,
                payload={
                    "status": "failed",
                    "output": None,
                    "refusal": None,
                    "failure": "Provider invocation was terminated during daemon recovery.",
                    "failure_code": "provider_completion_not_durable",
                    "failure_details": {
                        "provider_refusal_code": "provider_process_group_survived_recovery"
                    },
                    "halt": None,
                    "semantic_result_digest": None,
                    "provider_calls": provider_calls,
                    "capture_bytes": 0,
                    "invocation_receipt_digests": list(invocation_receipt_digests),
                    "budget": ProcedureRunBudgetV1(
                        declared=ProcedureRunBudgetDeclaredV1(
                            budget=admission.budget,
                            hard_caps=admission.hard_caps,
                        ),
                        observed=ProcedureRunBudgetObservedV1(
                            max_items=ProcedureBudgetBoundaryObservationV1(high_water=0),
                            result_bytes=ProcedureBudgetBoundaryObservationV1(high_water=0),
                            provider_calls=provider_calls,
                            capture_bytes=0,
                            wall_clock_microseconds=wall_clock_microseconds,
                        ),
                    ).model_dump(mode="json"),
                },
            )
        finally:
            journal.fence_writer(
                stream,
                partition_id,
                expected_fencing_token=PROCEDURE_RUN_FENCING_TOKEN,
            )
    if observed_failures:
        detail = "; ".join(
            f"run {run_id} invocation {invocation_id} requires recovery ({code})"
            for run_id, invocation_id, code in sorted(observed_failures, key=lambda item: item[1])
        )
        raise ProcedureRunRecoveryRequired(f"{ProcedureRunRecoveryRequired.code}: {detail}")
    return tuple(sorted(handled, key=str.encode))


def service_prepare_playbill_line_admission(
    instance: PlaybillInstance,
    *,
    admission: ProcedureRunAdmissionV3 | ProcedureRunAdmissionV4 | ProcedureRunAdmissionV5,
    accepted_line: AcceptedLineSpecV1,
) -> (
    ProcedureRunAdmissionV3
    | ProcedureRunAdmissionV4
    | ProcedureRunAdmissionV5
    | ProcedureAdmissionRefusalV1
):
    """Bind the accepted runtime policy into a Line admission before publication."""

    if isinstance(admission, ProcedureRunAdmissionV5) and bool(admission.exhaust_inputs) != (
        admission.exhaust_access_binding_digest is not None
    ):
        return ProcedureAdmissionRefusalV1(
            code="exhaust_binding_carrier_required",
            message="Exhaust inputs require exactly one opaque access-binding carrier.",
            details={
                "exhaust_input_count": len(admission.exhaust_inputs),
                "carrier_present": admission.exhaust_access_binding_digest is not None,
            },
            repair=hand_edit_repair("exhaust_binding_carrier_required"),
        )
    tree = instance.tree_at(admission.bound_coordinate.git_oid)
    try:
        policy = resolve_procedure_runtime_policy(tree)
    except ProcedureRuntimePolicyAbsent as exc:
        return ProcedureAdmissionRefusalV1(
            code="procedure_runtime_policy_absent",
            message=str(exc),
            details={"policy_path": PROCEDURE_RUNTIME_POLICY_PATH},
            repair=hand_edit_repair("procedure_runtime_policy_absent"),
        )
    try:
        bound = bind_line_admission_runtime_policy(admission, policy)
        verify_line_admission_spec(bound, accepted_line)
    except PlaybillExecutionError as exc:
        return ProcedureAdmissionRefusalV1(
            code="artifact_binding_mismatch",
            message=str(exc),
            details={
                "accepted_line_identity": accepted_line.line.identity.qualified,
                "accepted_line_spec_digest": accepted_line.artifact_digest,
            },
            repair=hand_edit_repair("artifact_binding_mismatch"),
        )
    return bound


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
    "LineRunIdentityMismatch",
    "LineRunNotAccepted",
    "LineRunRequestV1",
    "PROCEDURE_RUN_ID_DOMAIN",
    "ProcedureBindRequestV1",
    "ProcedureBindResultV2",
    "ProcedureBindingGraphV4LineClosureRequired",
    "ProcedurePendingSuccessorV1",
    "ProcedureReadinessRequestV1",
    "ProcedureReadinessResultV1",
    "ProcedureRunRequestV2",
    "ProcedureRunStateV2",
    "ProcedureSurfaceError",
    "service_bind_playbill_procedure",
    "service_get_playbill_procedure_run",
    "service_playbill_procedure_readiness",
    "service_prepare_playbill_line_admission",
    "service_run_playbill_line",
    "service_run_playbill_procedure",
]
