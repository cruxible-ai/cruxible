"""Authenticated Procedure admission and log-sufficient Procedure execution."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Final, Literal, NoReturn, Protocol, cast

from cruxible_client.contracts.captures import capture_retention_policy, parse_capture_envelope
from cruxible_client.contracts.shapes import Contract, ContractRef

if TYPE_CHECKING:
    from cruxible_core.procedures.nested import NestedProcedureRunner, ParentInvocationContext

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    CaptureContractAny as CaptureContract,
)
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureFormatError,
    CaptureRetentionErasurePolicy,
    ProviderResultToExternalCapture,
    build_provider_external_capture_v2,
    capture_contract_digest,
    validate_provider_external_capture_result,
)
from cruxible_client.contracts.errors import (
    CasError,
    ExecutionError,
    JournalConflictError,
    JournalError,
    JournalIntegrityError,
)
from cruxible_client.contracts.line_inbox import (
    INBOX_ITEM_MAX_BYTES,
    INBOX_RUN_ITEM_CAP_DEFAULT,
    INBOX_RUN_MAX_BYTES,
)
from cruxible_client.contracts.procedure_runtime_policy import (
    ProcedureRuntimePolicy,
)
from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionInputDecision,
    AcquisitionRule,
)
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.contracts import (
    ProcedureContractItemBudgetExceeded,
    ValidatedProcedureContract,
)
from cruxible_client.contracts.procedures.graph import analyze_procedure, reference_templates
from cruxible_client.contracts.procedures.line_specs import (
    AcceptedLineSpec,
    line_identity_digest,
)
from cruxible_client.contracts.procedures.models import (
    CLOSED_TRANSFORM_KINDS,
    TERMINAL_REQUIRED_RUNGS,
    CallNode,
    CaptureEgressNode,
    ExtractNode,
    GuardNode,
    GuardPredicate,
    HaltNode,
    InboxEgressNode,
    InvokeNode,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    ProposalItemsFanOut,
    ProposeChangeSetNode,
    ReadNode,
    RepeatBodyNode,
    RepeatNode,
    SelectNode,
    SettleChangeSetNode,
    TransformNode,
    authority_for_rung,
    is_terminal_node,
    iter_references,
    iter_role_references,
)
from cruxible_client.contracts.procedures.results import (
    InboxPostTruncation,
    ProcedureAcquisitionPlan,
    ProcedureAdmissionMaterialManifest,
    ProcedureAdmissionMaterialMember,
    ProcedureBudgetBoundaryObservation,
    ProcedureBudgetRefusalDetail,
    ProcedureFailureCause,
    ProcedureNodeRefusalCode,
    ProcedureProviderBinding,
    ProcedureReplayInputProjection,
    ProcedureRunBudget,
    ProcedureRunBudgetDeclared,
    ProcedureRunBudgetObserved,
    ProcedureSelectionDecision,
    ProcedureSourceCaptureAssociation,
    procedure_acquisition_plan_digest,
    procedure_admission_material_digest,
    procedure_provider_bindings,
    procedure_selection_decision_digest,
)
from cruxible_client.contracts.procedures.windows import LineTriggerBinding
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservation,
    ProviderExternalOccurrencePlan,
    ProviderInvocationCompleted,
    ProviderInvocationOutcome,
    ProviderInvocationOutputDigest,
    ProviderInvocationReceipt,
    ProviderInvocationStarted,
    ProviderSecretBindingIdentity,
    ProviderSecretReceiptReference,
    build_procedure_derived_source_request,
    provider_invocation_output_digest,
    provider_invocation_receipt_digest,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.provider_operations import (
    OperationItemBudgetExceeded,
    OperationValueRefused,
    capture_fields,
    closure_lookup,
    validate_operation_value,
)
from cruxible_client.contracts.query.grammar import QueryBudgets
from cruxible_client.contracts.resolution_contracts import InvestigationBinding
from cruxible_client.contracts.shapes import EnumResolver
from cruxible_client.contracts.source_references import ExternalSourceReference
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, utc_now
from cruxible_client.contracts.workspace_file import (
    ExecutedWorkspaceFileSourceRequestV2,
    SourceReadReceipt,
    WorkspaceBindingDigestAuthored,
    parse_authored_workspace_request,
    source_read_receipt_digest,
)
from cruxible_core.documents.workspace_file import (
    WorkspaceFileReader,
    WorkspaceFileReadRefused,
)
from cruxible_core.evidence.source_readers import (
    CaptureAcquisitionReceiptV1,
    ExternalCaptureAcquisitionV1,
)
from cruxible_core.exhaust import (
    JournalEventKindV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureJournalRecordDraftV1,
    StoredProcedureJournalRecordV1,
    journal_payload_bytes,
)
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.exhaust.writer import note_invocation_start
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.procedures.acquisition import (
    ACQUISITION_ORIGIN_INELIGIBLE,
    ACQUISITION_OVERSIZED,
    ProcedureCaptureMaterialV1,
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
    disallowed_origin,
)
from cruxible_core.procedures.completeness import Completeness, CompletenessProbe, Read
from cruxible_core.procedures.egress import (
    SERVED_AUTHORITY_TERMS,
    EffectiveRungTermV1,
    EffectiveRungV1,
    InboxEgressSinkProtocol,
    PreparedTerminalEgressV1,
    SettleGrantHolderProtocol,
    TerminalAuthorityRefusal,
    TerminalEgressError,
    TerminalEgressItemV1,
    TerminalEgressKindV1,
    TerminalEgressPreparerProtocol,
    TerminalEgressReceiptV1,
    TerminalEgressReceiptV2,
    TerminalEgressRequestV1,
    TerminalEgressRequestV2,
    TerminalEgressSinkProtocol,
    build_terminal_egress_request_v2,
    effect_dispatch_refusal,
    effective_rung_digest,
    verify_terminal_egress_receipt,
)
from cruxible_core.procedures.extract import ExtractRefused, run_extract
from cruxible_core.procedures.input_planes import (
    AcceptedClaimRunInputV1,
    AcceptedStateRunInputV2,
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
    ProcedureRunInputV1,
    accepted_read_definition_digest,
    merge_run_input_vector,
    run_input_digest,
    validate_node_input_plane,
    validate_run_input_vector,
)
from cruxible_core.procedures.line_admission import LINE_ARM_ADMISSION_GATE
from cruxible_core.procedures.ports import (
    AcquisitionRules,
    bound_operation,
)
from cruxible_core.procedures.reference_binding import (
    ProcedureExecutionPlan,
    execution_plan,
    resolved_pin,
)
from cruxible_core.procedures.run_index import ProcedureRunIndex
from cruxible_core.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    TAINT_OMITTED_OPTIONAL,
    TAINT_UNPROMOTED_EXHAUST,
    AcquisitionInputOutcomeV1,
    AliasProvenanceV1,
    DependencyEvidenceFactsV1,
    DependencyToken,
    TerminalChildReceiptV1,
    TerminalItemDependencyManifestV1,
    accepted_state_token,
    admitted_capture_token,
    build_terminal_item_manifest,
    citable_captures,
    derive_terminal_item_facts,
    exhaust_token,
    manifest_dependency_tokens,
    policy_token,
    produced_capture_token,
    receipt_token,
    terminal_item_key,
)
from cruxible_core.procedures.transforms import (
    TransformInputInvalid,
    TransformItemBudgetExceeded,
    apply_closed_transform,
)
from cruxible_core.providers.provider_local_runtime import (
    BoundLocalProviderV1,
    ProviderDriverOutcomeV1,
    ProviderLocalRuntimeRefused,
    ProviderSpawnDeadline,
)
from cruxible_core.providers.provider_outcomes import (
    RefusalDetailCheck,
    map_provider_envelope,
    map_provider_refusal,
    operation_refusal_is_declared,
    provider_canonical_value,
    provider_refusal_is_absorbable,
)
from cruxible_core.providers.provider_runtime_contract import (
    ProviderRuntimeBudgets,
    ProviderRuntimeRunContext,
    ProviderRuntimeWireError,
)
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.storage.material_reservations import (
    PendingAdmissionMaterialReservationV1,
    ProcedureMaterialReservationStore,
    ReservedCaptureStore,
    make_run_reservation,
)

ProcedureRunStatusV1 = Literal[
    "succeeded",
    "refused",
    "failed",
    "budget_exhausted",
    "halted",
]

PROCEDURE_SEMANTIC_RESULT_DOMAIN = "playbill-procedure-semantic-result-v1"
#: The one admitted-run family: its admission tag is also its binding-digest
#: domain, and its replay key hashes one explicit semantic projection.
PROCEDURE_RUN_ADMISSION_TAG: Final = "playbill-procedure-run-admission-v10"
PROCEDURE_SEMANTIC_REPLAY_KEY_DOMAIN: Final = "playbill-procedure-replay-key-v10"
PROCEDURE_INPUT_PROVENANCE_DOMAIN = "playbill-procedure-replay-input-provenance-v1"
PROCEDURE_LINE_RUN_ID_DOMAIN = "playbill-procedure-line-run-id-v1"
PROCEDURE_RUN_ID_V2_DOMAIN = "playbill-procedure-run-id-v2"
#: The one public run receipt's tag, which is also its digest domain.
PROCEDURE_RUN_RECEIPT_DOMAIN: Final = "playbill-procedure-run-receipt-v8"

_ZERO_DIGEST: Final = "sha256:" + "0" * 64
_ZERO_RUN_ID: Final = "RUN-" + "0" * 64


class _StrictExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


def _sorted_unique(value: tuple[str, ...]) -> bool:
    return value == tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))


def run_value_digest(domain: str, value: object) -> str:
    return typed_digest(
        ArtifactDigest,
        f"playbill-procedure-run-{domain}-v1",
        {"value": normalize_canonical(value)},
    ).tagged


class ProcedureNodePinSetV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-node-pin-set-v1"] = "playbill-procedure-node-pin-set-v1"
    node_id: str
    pins: tuple[ArtifactPin, ...]

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        ordered = tuple(
            sorted(
                value,
                key=lambda pin: (
                    pin.role.encode("utf-8"),
                    pin.target.qualified.encode("utf-8"),
                    pin.artifact_digest.encode("ascii"),
                ),
            )
        )
        identities = tuple((pin.role, pin.target.qualified) for pin in value)
        if value != ordered or len(identities) != len(set(identities)):
            raise ValueError("Procedure node pins must be sorted and unique")
        return value


#: The accepted-state plane's two arms: a Query result and a Claim selection.
AcceptedStateRunInput = AcceptedStateRunInputV2 | AcceptedClaimRunInputV1


class AcceptedStateRunMaterial(_StrictExecutionModel):
    """One admitted accepted-state read and the exact value its digest reproduces."""

    tag: Literal["playbill-accepted-state-run-material-v2"] = (
        "playbill-accepted-state-run-material-v2"
    )
    input: AcceptedStateRunInput
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _digest(self) -> "AcceptedStateRunMaterial":
        if self.input.result_digest != run_value_digest("state-result", self.value):
            raise ValueError("accepted-state material does not reproduce its result digest")
        return self


class ProcedureResourceBudgetV1(_StrictExecutionModel):
    """Effective instance/Procedure intersection retained before execution."""

    result_bytes_cap: int = Field(ge=1)
    repeat_attempts_cap: int = Field(ge=1)


def procedure_run_declared_budget(admission: "ProcedureRunAdmission") -> ProcedureRunBudgetDeclared:
    """The declared half of a run's one budget grammar, read off its admission."""

    return ProcedureRunBudgetDeclared(
        budget=admission.budget,
        hard_caps=admission.hard_caps,
        result_bytes_cap=(
            admission.resource_budget.result_bytes_cap
            if admission.resource_budget is not None
            else 1_048_576
        ),
        provider_output_bytes_cap=admission.provider_output_bytes_cap,
    )


def resolve_procedure_resource_budget(
    budget: ProcedureBudget,
    hard_caps: ProcedureHardCaps,
    policy: ProcedureRuntimePolicy,
) -> ProcedureResourceBudgetV1 | None:
    if policy.result_bytes_cap is None and policy.repeat_attempts_cap is None:
        if budget.max_result_bytes is not None or hard_caps.max_result_bytes is not None:
            raise ExecutionError("explicit result budgets require a runtime result policy")
        if hard_caps.max_repeat_attempts > 25:
            raise ExecutionError("repeat attempts above 25 require a runtime repeat policy")
        return None
    return ProcedureResourceBudgetV1(
        result_bytes_cap=min(
            cap
            for cap in (
                policy.result_bytes_cap or 1_048_576,
                budget.max_result_bytes,
                hard_caps.max_result_bytes,
            )
            if cap is not None
        ),
        repeat_attempts_cap=min(policy.repeat_attempts_cap or 25, hard_caps.max_repeat_attempts),
    )


class ProcedureParentBindingV1(_StrictExecutionModel):
    """Backend-owned link to the admitted invocation that delegated this work."""

    parent_run_id: str
    parent_admission_digest: str
    node_id: str
    ancestors: tuple[ArtifactPin, ...]
    activation_policies: tuple[Literal["drain", "abort", "snapshot", "epoch-check"], ...]

    @field_validator("parent_admission_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return _sha256(value, label="parent_admission_digest")

    @model_validator(mode="after")
    def _lineage(self) -> ProcedureParentBindingV1:
        identities = tuple(pin.target for pin in self.ancestors)
        if not identities or len(set(identities)) != len(identities):
            raise ValueError("child invocation requires a nonrecursive ancestor chain")
        if any(identity.kind != "Procedure" for identity in identities):
            raise ValueError("child ancestors must be exact Procedure pins")
        if len(self.activation_policies) != len(identities):
            raise ValueError("every ancestor binds its activation policy")
        return self


class ProcedureRunAdmission(_StrictExecutionModel):
    """The one complete run binding, fixed before any result is visible.

    Every supported run admits under this one schema; what kind of run it is
    reads off explicit fields, never off a model version:

    - ``invocation_origin``: an actor's direct run or a Line occurrence. A Line
      run binds its LineSpec, occurrence, deployment snapshot, acquisition
      policy, mandate, calibration and sensitivity coordinates together; a
      direct run binds none of the Line's own coordinates.
    - ``parent_binding``: present exactly for a nested child, which keeps its
      root's actor, lane and authority coordinates.
    - ``lane``: the current lane or exact replay.
    - ``acquisition_plan_digest``: present exactly when the run plans external
      Provider occurrences (every Line run, and a direct run with Source or
      Call nodes). A pure direct run plans none, so it carries no Provider
      bindings, selection decision or Provider output cap.
    - ``investigation`` and ``trigger_binding``: the run's exact investigation
      and Line trigger, when it has them.

    ``accepted_coordinate.generation``, ``bound_coordinate.generation`` and
    ``head_at_admission.generation`` read SETTLEMENT ORDER; ``admitted_at`` and
    ``occurrence_evaluation_time`` read EVALUATION INSTANT.
    """

    resource_budget: ProcedureResourceBudgetV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    tag: Literal["playbill-procedure-run-admission-v10"] = PROCEDURE_RUN_ADMISSION_TAG
    instance_id: str
    run_id: str
    attempt: int = Field(ge=1)
    accepted_coordinate: AcceptedCoordinate
    bound_coordinate: AcceptedCoordinate
    head_at_admission: AcceptedCoordinate
    lane: Literal["current", "replay"]
    procedure_identity: ArtifactIdentity
    procedure_path: str
    procedure_artifact_digest: str
    definition_digest: str
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    full_pins: tuple[ArtifactPin, ...]
    node_pin_sets: tuple[ProcedureNodePinSetV1, ...]
    pin_set_digest: str
    invocation_input: object
    accepted_state_inputs: tuple[AcceptedStateRunInput, ...]
    landed_capture_inputs: tuple[LandedCaptureRunInputV1, ...] = ()
    exhaust_inputs: tuple[ExhaustRunInputV1, ...] = ()
    budget: ProcedureBudget
    hard_caps: ProcedureHardCaps
    actor_context: GovernedActorContext
    invocation_origin: Literal["actor", "line"] = "actor"
    journal_stream: JournalStreamIdentityV1
    journal_partition_id: str
    line_identity: ArtifactIdentity | None = None
    line_spec_digest: str | None = None
    occurrence_id: str | None = None
    occurrence_evaluation_time: datetime
    deployment_snapshot_digest: str | None = None
    acquisition_policy_digest: str | None = None
    selection_receipt_digest: str | None = None
    selection_decision: ProcedureSelectionDecision | None = None
    selection_decision_digest: str | None = None
    resolved_provider_bindings: tuple[ProcedureProviderBinding, ...] = ()
    provider_output_bytes_cap: int | None = Field(default=None, ge=1)
    acquisition_plan_digest: str | None = None
    exhaust_access_binding_digest: str | None = None
    sensitivity_policy_digest: str | None = None
    mandate_coordinate_digest: str | None = None
    calibration_coordinate_digest: str | None = None
    taint_labels: tuple[str, ...] = ()
    epsilon_member: bool = False
    investigation: InvestigationBinding | None = None
    trigger_binding: LineTriggerBinding | None = None
    parent_binding: ProcedureParentBindingV1 | None = None
    admitted_at: datetime
    semantic_replay_key_digest: str
    admission_binding_digest: str

    @field_validator("invocation_input", mode="before")
    @classmethod
    def _invocation_input(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator(
        "procedure_artifact_digest",
        "definition_digest",
        "pin_set_digest",
        "line_spec_digest",
        "deployment_snapshot_digest",
        "acquisition_policy_digest",
        "selection_receipt_digest",
        "selection_decision_digest",
        "acquisition_plan_digest",
        "exhaust_access_binding_digest",
        "sensitivity_policy_digest",
        "mandate_coordinate_digest",
        "calibration_coordinate_digest",
        "semantic_replay_key_digest",
        "admission_binding_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _sha256(value, label=str(getattr(info, "field_name", "run digest")))

    @field_validator("admitted_at", "occurrence_evaluation_time")
    @classmethod
    def _instants(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("accepted_state_inputs")
    @classmethod
    def _state_inputs(
        cls, value: tuple[AcceptedStateRunInput, ...]
    ) -> tuple[AcceptedStateRunInput, ...]:
        if not _sorted_unique(tuple(item.input_name for item in value)):
            raise ValueError("accepted-state run inputs must be sorted and unique")
        return value

    @field_validator("node_pin_sets")
    @classmethod
    def _node_pin_sets(
        cls, value: tuple[ProcedureNodePinSetV1, ...]
    ) -> tuple[ProcedureNodePinSetV1, ...]:
        if not _sorted_unique(tuple(item.node_id for item in value)):
            raise ValueError("Procedure node pin sets must be sorted and unique")
        return value

    @field_validator("resolved_provider_bindings")
    @classmethod
    def _provider_bindings(
        cls, value: tuple[ProcedureProviderBinding, ...]
    ) -> tuple[ProcedureProviderBinding, ...]:
        if not _sorted_unique(tuple(item.node_id for item in value)):
            raise ValueError("resolved Provider bindings must be sorted and node-id unique")
        return value

    @field_validator("taint_labels")
    @classmethod
    def _taint(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not _sorted_unique(value):
            raise ValueError("admitted taint labels must be sorted and unique")
        return value

    @property
    def run_inputs(self) -> tuple[ProcedureRunInputV1, ...]:
        """Return the complete discriminated union bound before any result is visible."""

        return merge_run_input_vector(
            self.accepted_state_inputs,
            self.landed_capture_inputs,
            self.exhaust_inputs,
        )

    @property
    def plans_providers(self) -> bool:
        """Whether this run planned external Provider occurrences at admission."""

        return self.acquisition_plan_digest is not None

    @model_validator(mode="after")
    def _admission_shape(self) -> "ProcedureRunAdmission":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("Procedure run admission requires a Procedure identity")
        if self.journal_stream.instance_id != self.instance_id:
            raise ValueError("Procedure run and journal stream instance identities differ")
        if self.accepted_coordinate != self.bound_coordinate:
            raise ValueError("admission bound and accepted coordinates differ")
        validate_run_input_vector(self.run_inputs, expected_accepted=self.accepted_coordinate)
        self._plan_shape()
        if bool(self.exhaust_inputs) != (self.exhaust_access_binding_digest is not None):
            raise ValueError(
                "exhaust_binding_carrier_required: Exhaust inputs require their opaque binding"
            )
        if self.parent_binding is not None and self.procedure_identity in {
            pin.target for pin in self.parent_binding.ancestors
        }:
            raise ValueError("recursive Procedure invocation is not supported")
        if self.pin_set_digest != procedure_pin_set_digest(self.full_pins, self.node_pin_sets):
            raise ValueError("Procedure run pin_set_digest does not reproduce")
        if self.semantic_replay_key_digest != procedure_semantic_replay_key_digest(self):
            raise ValueError("semantic replay key does not reproduce")
        if self.admission_binding_digest != procedure_admission_digest(self):
            raise ValueError("Procedure run admission_binding_digest does not reproduce")
        if self.invocation_origin == "actor":
            self._direct_shape()
        else:
            self._line_shape()
        return self

    def _plan_shape(self) -> None:
        if not self.plans_providers:
            if (
                self.resolved_provider_bindings
                or self.selection_decision is not None
                or self.selection_decision_digest is not None
                or self.provider_output_bytes_cap is not None
                or self.trigger_binding is not None
            ):
                raise ValueError(
                    "a run that plans no Provider occurrence binds no Provider, selection, "
                    "output cap or trigger"
                )
            return
        if (
            self.selection_decision is None
            or self.selection_decision_digest is None
            or self.provider_output_bytes_cap is None
        ):
            raise ValueError(
                "a run that plans Provider occurrences binds its selection decision and "
                "Provider output cap"
            )
        if self.acquisition_policy_digest != self.selection_decision.policy_digest:
            raise ValueError("selection decision must name the admitted acquisition policy")
        if self.selection_decision_digest != procedure_selection_decision_digest(
            self.selection_decision
        ):
            raise ValueError("selection decision digest does not reproduce")

    def _direct_shape(self) -> None:
        # A direct run is not an occurrence of anything: it names no Line,
        # occurrence, mandate or calibration, and evaluates at its admission
        # instant. A run that plans Providers also binds the acquisition
        # policy and deployment snapshot it acquires under.
        if (
            self.line_identity is not None
            or self.line_spec_digest is not None
            or self.occurrence_id is not None
            or self.mandate_coordinate_digest is not None
            or self.calibration_coordinate_digest is not None
            or self.epsilon_member
        ):
            raise ValueError("direct admission cannot claim Line state")
        if not self.plans_providers and (
            self.deployment_snapshot_digest is not None
            or self.acquisition_policy_digest is not None
        ):
            raise ValueError("a direct run without Provider occurrences binds no deployment")
        if (
            self.landed_capture_inputs
            or self.exhaust_inputs
            or self.selection_receipt_digest is not None
            or self.sensitivity_policy_digest is not None
            or self.taint_labels
        ):
            raise ValueError("direct admission binds only the accepted-state input plane")
        if self.occurrence_evaluation_time != self.admitted_at:
            raise ValueError("direct admission evaluates at the instant it was admitted")
        if self.run_id != procedure_semantic_run_id(self.semantic_replay_key_digest):
            raise ValueError("direct admission run_id does not reproduce")

    def _line_shape(self) -> None:
        if not self.plans_providers:
            raise ValueError("Line run admission binds its acquisition plan")
        if any(
            value is None
            for value in (
                self.line_spec_digest,
                self.occurrence_id,
                self.deployment_snapshot_digest,
                self.acquisition_policy_digest,
                self.mandate_coordinate_digest,
                self.calibration_coordinate_digest,
            )
        ):
            raise ValueError(
                "Line run admission must bind LineSpec, occurrence, deployment snapshot, "
                "acquisition policy, mandate, and calibration coordinates together"
            )
        if self.sensitivity_policy_digest is None:
            raise ValueError("Line run admission must bind its sensitivity policy")
        if self.line_identity is None or self.line_identity.kind != "Line":
            raise ValueError("Line admission line_identity must have kind Line")
        if self.journal_stream != procedure_line_journal_stream(self.instance_id):
            raise ValueError("Line admission must use the Procedure exhaust stream")
        expected_partition = (
            procedure_direct_partition(self.semantic_replay_key_digest)
            if self.parent_binding is not None
            else procedure_line_partition(self.line_identity)
        )
        if self.journal_partition_id != expected_partition:
            raise ValueError("Line admission partition does not reproduce its Line identity")
        assert self.occurrence_id is not None
        if self.run_id != procedure_line_run_id(
            occurrence_id=self.occurrence_id,
            attempt=self.attempt,
            admission_binding_digest=self.admission_binding_digest,
            occurrence_evaluation_time=self.occurrence_evaluation_time,
        ):
            raise ValueError("Line admission run_id does not reproduce")


class LandedCaptureRunMaterialV1(_StrictExecutionModel):
    """One admitted landed Capture and the exact envelope its digest reproduces."""

    tag: Literal["playbill-landed-capture-run-material-v1"] = (
        "playbill-landed-capture-run-material-v1"
    )
    input: LandedCaptureRunInputV1
    material: ProcedureCaptureMaterialV1

    @model_validator(mode="after")
    def _binding(self) -> "LandedCaptureRunMaterialV1":
        if self.material.capture_digest != self.input.capture_digest:
            raise ValueError("landed Capture material differs from its admitted input")
        if self.material.capture_contract_digest != self.input.capture_contract_digest:
            raise ValueError("landed Capture material names another CaptureContract")
        return self


class ExhaustRunMaterialV1(_StrictExecutionModel):
    """One admitted exhaust range result; the range and reducer are already bound."""

    tag: Literal["playbill-exhaust-run-material-v1"] = "playbill-exhaust-run-material-v1"
    input: ExhaustRunInputV1
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _digest(self) -> "ExhaustRunMaterialV1":
        if self.input.result_digest != run_value_digest("exhaust-result", self.value):
            raise ValueError("exhaust material does not reproduce its result digest")
        return self


class _AdmissionCarrier(_StrictExecutionModel):
    """What both the prepared run and its durable admission record carry.

    The material manifest covers the landed-Capture and exhaust inputs (empty
    when there are none). The acquisition plan is present exactly when the
    admission planned Provider occurrences, and the admission's bindings are
    then exactly the plan's own.

    Nested coordinate generations read SETTLEMENT ORDER; nested ``admitted_at``
    and ``occurrence_evaluation_time`` read EVALUATION INSTANT; nested retention
    deadlines and Provider budget windows read VALIDITY WINDOW.
    """

    admission: ProcedureRunAdmission
    admission_material_manifest: ProcedureAdmissionMaterialManifest
    admission_material_manifest_digest: str
    acquisition_plan: ProcedureAcquisitionPlan | None = None
    acquisition_plan_digest: str | None = None
    required_reservation_ids: tuple[str, ...] = ()

    @field_validator("admission_material_manifest_digest", "acquisition_plan_digest")
    @classmethod
    def _carrier_digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _sha256(value, label=str(getattr(info, "field_name", "carrier digest")))

    @field_validator("required_reservation_ids")
    @classmethod
    def _reservations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("required reservation ids must be sorted and unique")
        for item in value:
            _sha256(item, label="required_reservation_id")
        return value

    @model_validator(mode="after")
    def _carrier(self) -> "_AdmissionCarrier":
        if self.admission_material_manifest_digest != procedure_admission_material_digest(
            self.admission_material_manifest
        ):
            raise ValueError("admission material manifest digest does not reproduce")
        _validate_admission_material_manifest(self.admission, self.admission_material_manifest)
        plan = self.acquisition_plan
        if (plan is None) != (self.acquisition_plan_digest is None):
            raise ValueError("an acquisition plan and its digest travel together")
        if self.acquisition_plan_digest != self.admission.acquisition_plan_digest:
            raise ValueError("prepared acquisition plan differs from admission")
        if plan is None:
            return self
        admission = self.admission
        if self.acquisition_plan_digest != procedure_acquisition_plan_digest(plan):
            raise ValueError("prepared acquisition plan digest does not reproduce")
        if plan.exhaust_access_binding_digest != admission.exhaust_access_binding_digest:
            raise ValueError("prepared Exhaust binding differs from admission")
        if (
            plan.accepted_coordinate != admission.accepted_coordinate
            or plan.line_identity != admission.line_identity
            or plan.line_spec_digest != admission.line_spec_digest
            or plan.occurrence_id != admission.occurrence_id
            or plan.occurrence_evaluation_time != admission.occurrence_evaluation_time
        ):
            raise ValueError("prepared acquisition plan names another Line coordinate")
        if any(
            item.budget_translation.policy_output_bytes_cap != admission.provider_output_bytes_cap
            for item in plan.external_occurrences
        ):
            raise ValueError("acquisition plan budget does not use policy-in-force")
        if admission.resolved_provider_bindings != procedure_provider_bindings(
            plan.external_occurrences
        ):
            raise ValueError("Provider bindings are not the acquisition plan's own")
        return self


class PreparedProcedureRun(_AdmissionCarrier):
    """An admitted run and every material it observes, ready to execute."""

    tag: Literal["playbill-prepared-procedure-run-v10"] = "playbill-prepared-procedure-run-v10"
    accepted_state_materials: tuple[AcceptedStateRunMaterial, ...]
    landed_capture_materials: tuple[LandedCaptureRunMaterialV1, ...] = ()
    exhaust_materials: tuple[ExhaustRunMaterialV1, ...] = ()
    acquisition_outcomes: tuple[AcquisitionInputOutcomeV1, ...] = ()

    @model_validator(mode="after")
    def _materials(self) -> "PreparedProcedureRun":
        admission = self.admission
        if tuple(item.input for item in self.accepted_state_materials) != (
            admission.accepted_state_inputs
        ):
            raise ValueError("prepared run materials must exactly match admitted state inputs")
        if tuple(item.input for item in self.landed_capture_materials) != (
            admission.landed_capture_inputs
        ):
            raise ValueError("prepared run materials must exactly match admitted landed Captures")
        if tuple(item.input for item in self.exhaust_materials) != admission.exhaust_inputs:
            raise ValueError("prepared run materials must exactly match admitted exhaust inputs")
        if not _sorted_unique(tuple(item.input_name for item in self.acquisition_outcomes)):
            raise ValueError("prepared acquisition outcomes must be sorted and unique")
        return self

    def bound_payload(self) -> "ProcedureAdmissionBoundPayload":
        """The durable admission record of this prepared run."""

        return ProcedureAdmissionBoundPayload.model_validate(
            {
                name: getattr(self, name)
                for name in ProcedureAdmissionBoundPayload.model_fields
                if name != "tag"
            }
        )


class ProcedureAdmissionBoundPayload(_AdmissionCarrier):
    """The ``admission_bound`` journal record: the admission and its carried commitments.

    Accepted-state values are not repeated here: each admitted read already
    names its retained body (``material_body_digest``).
    """

    tag: Literal["playbill-procedure-admission-bound-payload-v10"] = (
        "playbill-procedure-admission-bound-payload-v10"
    )


class ProcedureRunRefusalV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-refusal-v1"] = "playbill-procedure-run-refusal-v1"
    code: str
    message: str
    node_id: str | None = None
    detail_code: str | None = None
    details: object = Field(default_factory=dict)
    budget: ProcedureBudgetRefusalDetail | None = None

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureRunReceiptV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-receipt-v1"] = "playbill-procedure-run-receipt-v1"
    run_id: str
    admission_binding_digest: str
    stream: JournalStreamIdentityV1
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_digests: tuple[str, ...]
    chain_head_digest: str

    @field_validator("admission_binding_digest", "chain_head_digest")
    @classmethod
    def _receipt_digest(cls, value: str, info: object) -> str:
        return _sha256(value, label=str(getattr(info, "field_name", "receipt digest")))

    @field_validator("record_digests")
    @classmethod
    def _record_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("Procedure receipt record digests must be nonempty and unique")
        for item in value:
            _sha256(item, label="Procedure receipt record digest")
        return value

    @model_validator(mode="after")
    def _range(self) -> "ProcedureRunReceiptV1":
        if self.last_sequence - self.first_sequence + 1 != len(self.record_digests):
            raise ValueError("Procedure receipt sequence range and records disagree")
        if self.chain_head_digest != self.record_digests[-1]:
            raise ValueError("Procedure receipt head must equal its final record digest")
        return self


class ProcedureRunResultV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-result-v1"] = "playbill-procedure-run-result-v1"
    run_id: str
    status: ProcedureRunStatusV1
    output: object | None = None
    refusal: ProcedureRunRefusalV1 | None = None
    receipt: ProcedureRunReceiptV1

    @field_validator("output", mode="before")
    @classmethod
    def _output(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureRunResultV1":
        if (self.status == "succeeded") != (self.output is not None):
            raise ValueError("only a succeeded Procedure run carries output")
        if (self.status == "refused") != (self.refusal is not None):
            raise ValueError("only a refused Procedure run carries a typed refusal")
        return self


def procedure_run_receipt_digest(receipt: ProcedureRunReceiptV1) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-receipt-v1",
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


def _semantic_refusal_payload(refusal: ProcedureRunRefusalV1) -> CanonicalValue:
    return normalize_canonical(
        {
            "code": refusal.code,
            "node_id": refusal.node_id,
            "detail_code": refusal.detail_code,
            "details": refusal.details,
            "budget": None if refusal.budget is None else refusal.budget.model_dump(mode="json"),
        }
    )


def procedure_semantic_result_digest(
    *,
    semantic_replay_key_digest: str,
    status: Literal["succeeded", "refused", "halted"],
    output: CanonicalValue | None,
    refusal: ProcedureRunRefusalV1 | None,
    halt: CanonicalValue | None = None,
) -> str:
    """Commit only replay-stable output or refusal material."""

    if (status == "refused") != (refusal is not None):
        raise ValueError("semantic Procedure refusal shape is inconsistent")
    if status == "halted":
        if halt is None or output is not None or refusal is not None:
            raise ValueError("semantic Procedure halt shape is inconsistent")
        return typed_digest(
            Sha256Value,
            PROCEDURE_SEMANTIC_RESULT_DOMAIN,
            {
                "semantic_replay_key_digest": semantic_replay_key_digest,
                "status": "halted",
                "halt": halt,
            },
        ).tagged
    if halt is not None:
        raise ValueError("only a halted Procedure semantic result carries halt material")
    return typed_digest(
        Sha256Value,
        PROCEDURE_SEMANTIC_RESULT_DOMAIN,
        {
            "semantic_replay_key_digest": semantic_replay_key_digest,
            "status": status,
            "output": output if status == "succeeded" else None,
            "refusal": None if refusal is None else _semantic_refusal_payload(refusal),
        },
    ).tagged


class ProviderInvocationResultV1(_StrictExecutionModel):
    tag: Literal["playbill-provider-invocation-result-v1"] = (
        "playbill-provider-invocation-result-v1"
    )
    output: object
    trace: object = Field(default_factory=dict)

    @field_validator("output", "trace", mode="before")
    @classmethod
    def _canonical(cls, value: object) -> object:
        return normalize_canonical(value)


class StateTapReadResultV1(_StrictExecutionModel):
    tag: Literal["playbill-state-tap-read-result-v1"] = "playbill-state-tap-read-result-v1"
    value: object
    effective_budgets: QueryBudgets
    #: Rows the read returned, charged against the run's read ceiling.
    rows: int = Field(default=0, ge=0, exclude=True)


class ReadCeilingExceeded(ExecutionError):
    """A Procedure read that the run's read ceiling would clip: refused, never truncated."""

    def __init__(self, *, clipped: tuple[str, ...], candidates: int | None, ceiling: int) -> None:
        self.clipped = clipped
        self.candidates = candidates
        self.ceiling = ceiling
        super().__init__(
            f"read_ceiling_exceeded: the read clips {list(clipped)} under its {ceiling}-row ceiling"
        )

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)


class StateTapReaderProtocol(Protocol):
    def read_accepted_state(
        self,
        *,
        query: ArtifactPin,
        parameters: CanonicalValue,
        coordinate: AcceptedCoordinate,
        ceiling: int = ...,
    ) -> StateTapReadResultV1: ...

    def read_accepted_claim(
        self,
        *,
        claim_type: ArtifactPin,
        subject_kind: str,
        subject_id: str,
        cardinality: Literal["one", "all"],
        limit: int | None = None,
        coordinate: AcceptedCoordinate,
    ) -> StateTapReadResultV1: ...


class ProviderRuntimeInvokerProtocol(Protocol):
    def bind_provider(
        self,
        *,
        occurrence: ProviderExternalOccurrencePlan,
    ) -> BoundLocalProviderV1: ...

    def invoke_provider(
        self,
        *,
        occurrence: ProviderExternalOccurrencePlan,
        context: ProviderRuntimeRunContext,
        invocation_id: str,
        bound: BoundLocalProviderV1,
        deadline: ProviderSpawnDeadline,
    ) -> ProviderDriverOutcomeV1: ...


class ContractValidatorProtocol(Protocol):
    def validate_contract(
        self,
        *,
        contract: ArtifactPin,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
    ) -> object: ...

    def validate_contract_with_budget(
        self,
        *,
        contract: ArtifactPin,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
        max_items: int,
    ) -> ValidatedProcedureContract: ...

    def unique_list_field_path(self, contract: ArtifactPin) -> str | None: ...


class ProcedureActivationAuthorityProtocol(Protocol):
    def current_procedure_digest(
        self,
        identity: ArtifactIdentity,
        *,
        coordinate: AcceptedCoordinate,
    ) -> str | None: ...


def _journal_refusal_code(
    exc: JournalError,
) -> Literal["journal_conflict", "journal_integrity_error", "journal_append_failed"]:
    """Map one journal failure onto its served code by its exception class."""

    if isinstance(exc, JournalConflictError):
        return "journal_conflict"
    if isinstance(exc, JournalIntegrityError):
        return "journal_integrity_error"
    return "journal_append_failed"


class ProcedureClockProtocol(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


@dataclass(frozen=True)
class SystemProcedureClock:
    def now(self) -> datetime:
        return utc_now()

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


def procedure_pin_set_digest(
    pins: tuple[ArtifactPin, ...],
    node_pin_sets: tuple[ProcedureNodePinSetV1, ...],
) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-pin-set-v1",
        {
            "full_pins": [pin.model_dump(mode="json") for pin in pins],
            "node_pin_sets": [item.model_dump(mode="json") for item in node_pin_sets],
        },
    ).tagged


def procedure_replay_input_projection(
    run_input: ProcedureRunInputV1,
) -> ProcedureReplayInputProjection:
    if isinstance(run_input, AcceptedStateRunInputV2 | AcceptedClaimRunInputV1):
        provenance = {
            "plane": "accepted_state",
            (
                "claim_type_digest"
                if isinstance(run_input, AcceptedClaimRunInputV1)
                else "query_definition_digest"
            ): accepted_read_definition_digest(run_input),
            "parameters_digest": run_input.parameters_digest,
            "read_coordinate": run_input.read_coordinate.model_dump(mode="json"),
            "effective_query_budgets": run_input.effective_query_budgets.model_dump(mode="json"),
        }
        return ProcedureReplayInputProjection(
            input_name=run_input.input_name,
            plane="accepted_state",
            kind="claim_selection"
            if isinstance(run_input, AcceptedClaimRunInputV1)
            else "query_result",
            value_or_body_digest=run_input.result_digest,
            provenance_digest=typed_digest(
                Sha256Value,
                PROCEDURE_INPUT_PROVENANCE_DOMAIN,
                provenance,
            ).tagged,
        )
    if isinstance(run_input, LandedCaptureRunInputV1):
        return ProcedureReplayInputProjection(
            input_name=run_input.input_name,
            plane="landed_capture",
            kind="capture",
            value_or_body_digest=run_input.capture_digest,
            provenance_digest=typed_digest(
                Sha256Value,
                PROCEDURE_INPUT_PROVENANCE_DOMAIN,
                {
                    "plane": "landed_capture",
                    "capture_contract_digest": run_input.capture_contract_digest,
                    "landing_cursor": run_input.landing_cursor,
                },
            ).tagged,
        )
    return ProcedureReplayInputProjection(
        input_name=run_input.input_name,
        plane="exhaust",
        kind="reduced_exhaust",
        value_or_body_digest=run_input.result_digest,
        provenance_digest=typed_digest(
            Sha256Value,
            PROCEDURE_INPUT_PROVENANCE_DOMAIN,
            {
                "plane": "exhaust",
                "journal_identity": run_input.journal_identity,
                "first_cursor": run_input.first_cursor,
                "last_cursor": run_input.last_cursor,
                "reducer_or_query_digest": run_input.reducer_or_query_digest,
            },
        ).tagged,
    )


def procedure_replay_input_vector(
    admission: ProcedureRunAdmission,
) -> tuple[ProcedureReplayInputProjection, ...]:
    return tuple(procedure_replay_input_projection(item) for item in admission.run_inputs)


def _validate_admission_material_manifest(
    admission: ProcedureRunAdmission,
    manifest: ProcedureAdmissionMaterialManifest,
) -> None:
    expected: dict[str, tuple[str, str, str | None]] = {
        item.input_name: (
            "landed_capture",
            item.capture_digest,
            item.capture_contract_digest,
        )
        for item in admission.landed_capture_inputs
    }
    expected.update(
        {
            item.input_name: ("exhaust", item.result_digest, None)
            for item in admission.exhaust_inputs
        }
    )
    names = tuple(member.input_name for member in manifest.members)
    expected_names = tuple(sorted(expected, key=lambda item: item.encode("utf-8")))
    if names != expected_names:
        raise ValueError("admission material manifest must cover Capture/exhaust inputs")
    for member in manifest.members:
        plane, semantic_digest, retention_authority = expected[member.input_name]
        if member.plane != plane or member.semantic_digest != semantic_digest:
            raise ValueError("admission material member disagrees with its admitted input")
        if (
            retention_authority is not None
            and member.retention_authority_digest != retention_authority
        ):
            raise ValueError("Capture material member names another retention authority")


def capture_admission_material_member(
    run_input: LandedCaptureRunInputV1,
    *,
    policy: CaptureRetentionErasurePolicy,
    admitted_at: datetime,
    body_digest: str | None,
) -> ProcedureAdmissionMaterialMember:
    """Freeze Capture retention into one admission member at its admission time."""

    admitted_at = ensure_utc(admitted_at)
    retain_until = (
        admitted_at + timedelta(microseconds=policy.minimum_retention.microseconds)
        if policy.body_retention == "required_for_duration" and policy.minimum_retention is not None
        else None
    )
    return ProcedureAdmissionMaterialMember(
        input_name=run_input.input_name,
        plane="landed_capture",
        semantic_digest=run_input.capture_digest,
        body_digest=body_digest,
        retention_authority_digest=run_input.capture_contract_digest,
        body_retention=policy.body_retention,
        retain_until=retain_until,
        erasure_rule_digest=policy.erasure_rule_digest,
    )


def read_admission_material_body(
    bodies: ContentAddressedBodyStore,
    member: ProcedureAdmissionMaterialMember,
) -> bytes:
    """Read one admitted body or classify local absence without inventing erasure proof."""

    details = {
        "input_name": member.input_name,
        "plane": member.plane,
        "semantic_digest": member.semantic_digest,
        "retention_authority_digest": member.retention_authority_digest,
    }
    if member.body_digest is None:
        raise _OperationalFailure(
            "admission_material_unavailable_by_policy",
            details=details,
        )
    access = BodyAccessContext(principal_id="procedure-material-replay", can_read_body=True)
    try:
        metadata = bodies.metadata(member.body_digest, access=access)
    except Exception as exc:
        raise _OperationalFailure(
            "replay_material_unavailable",
            details={**details, "body_digest": member.body_digest},
        ) from exc
    if not metadata.present:
        raise _OperationalFailure(
            (
                "admission_material_unavailable_by_policy"
                if member.body_retention in {"never_materialize", "optional"}
                else "replay_material_unavailable"
            ),
            details={**details, "body_digest": member.body_digest},
        )
    try:
        return bodies.read(member.body_digest, access=access)
    except Exception as exc:  # pragma: no cover - metadata and read share verification
        raise _OperationalFailure(
            "replay_material_unavailable",
            details={**details, "body_digest": member.body_digest},
        ) from exc


def procedure_line_journal_stream(instance_id: str) -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id=instance_id,
        journal_family="procedure-exhaust-v1",
        stream_id="procedures",
    )


def _source_capture_association_fields(
    occurrence_path: str | None,
    invocation_receipt_digest: str | None,
) -> dict[str, str]:
    """Keep the retained graph-v3 produced-Capture payload byte-identical."""

    if occurrence_path is None or invocation_receipt_digest is None:
        return {}
    return {
        "occurrence_path": occurrence_path,
        "invocation_receipt_digest": invocation_receipt_digest,
    }


def verify_line_admission_spec(
    admission: ProcedureRunAdmission,
    accepted_line: AcceptedLineSpec,
    *,
    procedure_binding: ArtifactPin | None = None,
    acquisition_rules_digest: str | None = None,
) -> None:
    """Bind admission to exact Line bytes, its exact Procedure and its acquisition rules."""

    if (
        admission.line_identity != accepted_line.line.identity
        or admission.line_spec_digest != accepted_line.artifact_digest
    ):
        raise ExecutionError("Line admission names another accepted LineSpec")
    procedure = accepted_line.line.procedure
    if procedure_binding is not None and procedure_binding != procedure:
        raise ExecutionError("Line Procedure pin differs from the coordinate-resolved binding")
    if (
        admission.procedure_identity != procedure.target
        or admission.procedure_artifact_digest != procedure.artifact_digest
    ):
        raise ExecutionError("Line admission names another Procedure artifact")
    # A Source-free Line acquires nothing; its admission binds the accepted
    # runtime policy digest instead, which the admission digest seals.
    if (
        acquisition_rules_digest is not None
        and admission.acquisition_policy_digest != acquisition_rules_digest
    ):
        raise ExecutionError("Line admission names other acquisition rules")


def procedure_line_partition(line_identity: ArtifactIdentity) -> str:
    if line_identity.kind != "Line":
        raise ValueError("Procedure Line partition requires a Line identity")
    digest = line_identity_digest(line_identity)
    return "line:" + digest.removeprefix("sha256:")


def procedure_line_run_id(
    *,
    occurrence_id: str,
    attempt: int,
    admission_binding_digest: str,
    occurrence_evaluation_time: datetime,
) -> str:
    if attempt < 1:
        raise ValueError("Procedure Line attempt must be positive")
    _sha256(admission_binding_digest, label="admission_binding_digest")
    digest = typed_digest(
        Sha256Value,
        PROCEDURE_LINE_RUN_ID_DOMAIN,
        {
            "occurrence_id": occurrence_id,
            "attempt": attempt,
            "admission_binding_digest": admission_binding_digest,
            "occurrence_evaluation_time": format_datetime(ensure_utc(occurrence_evaluation_time)),
        },
    ).tagged
    return "RUN-" + digest.removeprefix("sha256:")


def parse_admission_payload(payload: object) -> ProcedureAdmissionBoundPayload:
    """Read the one current ``admission_bound`` record; any other envelope is refused."""

    if not isinstance(payload, dict):
        raise ExecutionError("admission payload is not an object")
    if payload.get("tag") != ProcedureAdmissionBoundPayload.model_fields["tag"].default:
        raise ExecutionError("admission payload version is unsupported")
    return ProcedureAdmissionBoundPayload.model_validate(payload)


def procedure_semantic_run_id(semantic_replay_key_digest: str) -> str:
    """Return the direct lane's run id: exact replay semantics, one identity."""

    _sha256(semantic_replay_key_digest, label="semantic_replay_key_digest")
    return "RUN-" + typed_digest(
        Sha256Value,
        PROCEDURE_RUN_ID_V2_DOMAIN,
        {"semantic_replay_key_digest": semantic_replay_key_digest},
    ).tagged.removeprefix("sha256:")


def procedure_direct_partition(semantic_replay_key_digest: str) -> str:
    """Return the direct lane's journal partition for one replay identity."""

    _sha256(semantic_replay_key_digest, label="semantic_replay_key_digest")
    return "direct:" + semantic_replay_key_digest.removeprefix("sha256:")


class ProcedureRuntimePolicyAbsent(ExecutionError):
    code = "procedure_runtime_policy_absent"


def procedure_semantic_replay_key_digest(admission: ProcedureRunAdmission) -> str:
    """Hash the one explicit semantic projection of an admission.

    The projection names exactly what decides a run's replay identity, and
    nothing volatile: the exact Procedure, invocation input, bound coordinate,
    evaluation instant and pins; each admitted input as its value (or body)
    digest beside a separate provenance digest; the budget, caps and runtime
    resource budget; the acquisition plan and exhaust binding (which carry the
    Line, occurrence, selection and Provider bindings); the authority
    coordinates, lane, taint and epsilon membership; the Provider output cap;
    and the exact investigation, trigger and parent binding. Who admitted it,
    when the head moved, and where its material bodies are stored are not
    replay semantics and never enter.
    """

    pins = [pin.model_dump(mode="json") for pin in admission.full_pins]
    pins.sort(key=canonical_bytes)
    return typed_digest(
        Sha256Value,
        PROCEDURE_SEMANTIC_REPLAY_KEY_DOMAIN,
        {
            "procedure_identity": admission.procedure_identity.model_dump(mode="json"),
            "procedure_artifact_digest": admission.procedure_artifact_digest,
            "invocation_input": admission.invocation_input,
            "bound_coordinate": admission.bound_coordinate.model_dump(mode="json"),
            "evaluation_time": format_datetime(admission.occurrence_evaluation_time),
            "validated_pins": pins,
            "node_pin_sets": [item.model_dump(mode="json") for item in admission.node_pin_sets],
            "pin_set_digest": admission.pin_set_digest,
            "replay_input_vector": [
                item.model_dump(mode="json") for item in procedure_replay_input_vector(admission)
            ],
            "budget": admission.budget.model_dump(mode="json"),
            "hard_caps": admission.hard_caps.model_dump(mode="json"),
            "resource_budget": None
            if admission.resource_budget is None
            else admission.resource_budget.model_dump(mode="json"),
            "acquisition_plan_digest": admission.acquisition_plan_digest,
            "exhaust_access_binding_digest": admission.exhaust_access_binding_digest,
            "mandate_coordinate_digest": admission.mandate_coordinate_digest,
            "calibration_coordinate_digest": admission.calibration_coordinate_digest,
            "sensitivity_policy_digest": admission.sensitivity_policy_digest,
            "lane": admission.lane,
            "taint_labels": list(admission.taint_labels),
            "epsilon_member": admission.epsilon_member,
            "provider_output_bytes_cap": admission.provider_output_bytes_cap,
            "investigation": None
            if admission.investigation is None
            else admission.investigation.model_dump(mode="json"),
            "trigger": None
            if admission.trigger_binding is None
            else admission.trigger_binding.model_dump(mode="json"),
            "parent_binding": None
            if admission.parent_binding is None
            else admission.parent_binding.model_dump(mode="json"),
        },
    ).tagged


def procedure_admission_digest(admission: ProcedureRunAdmission) -> str:
    return typed_digest(
        ArtifactDigest,
        PROCEDURE_RUN_ADMISSION_TAG,
        {"semantic_replay_key_digest": admission.semantic_replay_key_digest},
    ).tagged


def seal_procedure_run_admission(
    fields: Mapping[str, object],
    *,
    journal_partition_id: str | None = None,
) -> ProcedureRunAdmission:
    """Derive a run's complete identity from its bound fields: the one place it is done.

    Every preparation path (a pure direct run, a direct Provider run, a Line
    occurrence, a nested child, and any later investigation, trigger or runtime
    policy binding) supplies the admission's semantic fields here; the replay
    key, binding digest, run id and partition are always derived, never
    supplied. A direct run's id is its replay identity; a Line run's id names
    its occurrence, attempt and binding. A root Line run journals in its Line's
    partition, every other run in its replay identity's direct partition (a
    root direct run may name another partition explicitly).
    """

    provisional = ProcedureRunAdmission.model_construct(
        **{
            **cast(dict[str, Any], fields),
            # Exactly the normalized values validation hashes again.
            "invocation_input": normalize_canonical(fields.get("invocation_input")),
            "admitted_at": ensure_utc(cast(datetime, fields["admitted_at"])),
            "occurrence_evaluation_time": ensure_utc(
                cast(datetime, fields["occurrence_evaluation_time"])
            ),
            "run_id": _ZERO_RUN_ID,
            "semantic_replay_key_digest": _ZERO_DIGEST,
            "admission_binding_digest": _ZERO_DIGEST,
        }
    )
    replay_key = procedure_semantic_replay_key_digest(provisional)
    provisional = provisional.model_copy(update={"semantic_replay_key_digest": replay_key})
    digest = procedure_admission_digest(provisional)
    if provisional.invocation_origin == "line":
        if provisional.occurrence_id is None or provisional.line_identity is None:
            raise ExecutionError("Line admission lacks its occurrence or Line identity")
        run_id = procedure_line_run_id(
            occurrence_id=provisional.occurrence_id,
            attempt=provisional.attempt,
            admission_binding_digest=digest,
            occurrence_evaluation_time=provisional.occurrence_evaluation_time,
        )
        partition = (
            procedure_direct_partition(replay_key)
            if provisional.parent_binding is not None
            else procedure_line_partition(provisional.line_identity)
        )
    else:
        run_id = procedure_semantic_run_id(replay_key)
        partition = (
            journal_partition_id
            if journal_partition_id is not None and provisional.parent_binding is None
            else procedure_direct_partition(replay_key)
        )
    return ProcedureRunAdmission.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "run_id": run_id,
            "journal_partition_id": partition,
            "admission_binding_digest": digest,
        }
    )


def admission_fields(admission: ProcedureRunAdmission) -> dict[str, object]:
    """An admission's semantic fields, to rebind and reseal."""

    return {
        name: getattr(admission, name)
        for name in ProcedureRunAdmission.model_fields
        if name not in {"tag", "run_id", "semantic_replay_key_digest", "admission_binding_digest"}
    }


def bind_prepared_investigation(
    prepared: PreparedProcedureRun,
    *,
    investigation: InvestigationBinding | None,
    trigger: LineTriggerBinding | None = None,
) -> PreparedProcedureRun:
    """Bind an investigation (and a Line trigger) into a prepared run's admission."""

    if trigger is not None and not prepared.admission.plans_providers:
        raise ExecutionError("Line trigger requires a Line acquisition admission")
    admission = prepared.admission
    # A direct run's partition is re-derived from its new replay identity.
    sealed = seal_procedure_run_admission(
        {**admission_fields(admission), "investigation": investigation, "trigger_binding": trigger}
    )
    return PreparedProcedureRun.model_validate(
        {**prepared.model_dump(mode="python", exclude={"tag", "admission"}), "admission": sealed}
    )


def bind_line_admission_runtime_policy(
    admission: ProcedureRunAdmission,
    policy: ProcedureRuntimePolicy,
) -> ProcedureRunAdmission:
    """Derive the complete Line admission identity from its governed runtime cap."""

    if admission.invocation_origin != "line":
        raise ExecutionError("runtime policy rebinding is a Line admission step")
    return seal_procedure_run_admission(
        {
            **admission_fields(admission),
            "provider_output_bytes_cap": policy.provider_output_bytes_cap,
            "resource_budget": resolve_procedure_resource_budget(
                admission.budget, admission.hard_caps, policy
            ),
        }
    )


def _node_pin_sets(accepted: AcceptedProcedure) -> tuple[ProcedureNodePinSetV1, ...]:
    plan = execution_plan(accepted)
    result: list[ProcedureNodePinSetV1] = []
    for node in plan.definition.nodes:
        # One artifact pinned by two fields (two capture fields of one
        # CaptureContract) is one pin of the node.
        pins = tuple(
            dict.fromkeys(
                plan.pin(reference, role=role) for role, reference in iter_role_references(node)
            )
        )
        result.append(
            ProcedureNodePinSetV1(
                node_id=node.node_id,
                pins=tuple(
                    sorted(
                        pins,
                        key=lambda pin: (
                            pin.role.encode("utf-8"),
                            pin.target.qualified.encode("utf-8"),
                            pin.artifact_digest.encode("ascii"),
                        ),
                    )
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.node_id.encode("utf-8")))


def procedure_node_pin_sets(accepted: AcceptedProcedure) -> tuple[ProcedureNodePinSetV1, ...]:
    """Public seam for the exact per-node pin commitment one run must reproduce."""

    return _node_pin_sets(accepted)


def accepted_procedure_pin_set_digest(accepted: AcceptedProcedure) -> str:
    """Reproduce the direct-runtime pin commitment for one accepted Procedure."""

    return procedure_pin_set_digest(accepted.procedure.pins, _node_pin_sets(accepted))


def state_tap_parameters(
    node: ReadNode,
    *,
    invocation_input: object,
    values: dict[str, CanonicalValue],
) -> CanonicalValue:
    if node.reads_claims:
        return _resolve_template(
            {
                "subject_kind": node.subject_kind,
                "subject_id": node.subject_id,
                "cardinality": node.cardinality,
            },
            input_payload=normalize_canonical(invocation_input),
            outputs=values,
        )
    return _resolve_template(
        node.parameters, input_payload=normalize_canonical(invocation_input), outputs=values
    )


def state_tap_view(node: ReadNode, value: object) -> CanonicalValue:
    if not node.reads_claims:
        from cruxible_client.contracts.query.results import ClaimQueryResult
        from cruxible_core.query.engine import query_execution_receipt

        result = ClaimQueryResult.model_validate(value)
        return normalize_canonical(
            {
                "result": value,
                "coordinate": result.coordinate.model_dump(mode="json"),
                "definition_path": result.definition_path,
                "definition_digest": result.definition_digest,
                "receipt": query_execution_receipt(result).model_dump(mode="json"),
                "completed": result.verdict == "completed",
                "truncated": result.truncation.truncated,
                "has_conflicts": bool(
                    result.conflicts or any(row.conflicts for row in result.rows)
                ),
            }
        )
    return normalize_canonical(value)


def bind_accepted_state_materials(
    accepted: AcceptedProcedure,
    *,
    accepted_coordinate: AcceptedCoordinate,
    state_reader: StateTapReaderProtocol,
    bodies: ContentAddressedBodyStore,
    invocation_input: object = None,
) -> tuple[AcceptedStateRunMaterial, ...]:
    """Read and retain every accepted-state tap this run will observe.

    Every run lane reads the accepted-state plane the SAME way, so this is the
    one place a state tap becomes admitted material.
    """

    materials: list[AcceptedStateRunMaterial] = []
    values: dict[str, CanonicalValue] = {}
    # One ceiling across the run's reads: each read gets what is left of it.
    remaining = accepted.procedure.definition.hard_caps.max_read_rows
    for node in accepted.procedure.definition.nodes:
        if isinstance(node, ProjectNode) and node.literal is not None:
            values[node.as_] = normalize_canonical(node.literal)
        if not isinstance(node, ReadNode):
            continue
        binding = node.claim_type if node.claim_type is not None else node.query
        assert binding is not None  # a read names exactly one selection
        query = resolved_pin(accepted, binding)
        parameters = state_tap_parameters(node, invocation_input=invocation_input, values=values)
        try:
            if node.reads_claims:
                assert isinstance(parameters, dict)
                assert node.subject_kind is not None
                identity = parameters["subject_id"]
                if not isinstance(identity, str):
                    raise ExecutionError("Subject id must resolve to a string")
                read = state_reader.read_accepted_claim(
                    claim_type=query,
                    subject_kind=node.subject_kind,
                    subject_id=identity,
                    cardinality=node.cardinality,
                    limit=node.limit,
                    coordinate=accepted_coordinate,
                )
            else:
                if remaining < 1:
                    raise ReadCeilingExceeded(clipped=("max_results",), candidates=None, ceiling=0)
                read = state_reader.read_accepted_state(
                    query=query,
                    parameters=parameters,
                    coordinate=accepted_coordinate,
                    ceiling=remaining,
                )
                remaining -= read.rows
        except ReadCeilingExceeded as exc:
            raise ProcedureBoundaryRefused(
                "read_ceiling_exceeded",
                "A Procedure read would be clipped by the run's read ceiling; it refuses "
                "rather than return part of its result.",
                details={
                    "node": node.node_id,
                    "clipped": list(exc.clipped),
                    "candidates": exc.candidates,
                    "ceiling": exc.ceiling,
                },
            ) from exc
        except Exception as exc:
            raise ProcedureBoundaryRefused(
                "state_tap_refused",
                "The accepted-state reader refused this selection.",
                details={"node_id": node.node_id, "reason": str(exc)},
            ) from exc
        value = state_tap_view(node, read.value)
        values[node.as_] = value
        retained = bodies.store(canonical_bytes(value))
        common = dict(
            input_name=node.as_,
            read_coordinate=accepted_coordinate,
            parameters_digest=run_value_digest("state-parameters", parameters),
            result_digest=run_value_digest("state-result", value),
            effective_query_budgets=read.effective_budgets,
            material_body_digest=retained.digest,
        )
        run_input = (
            AcceptedClaimRunInputV1.model_validate(
                dict(common, claim_type_digest=query.artifact_digest)
            )
            if node.reads_claims
            else AcceptedStateRunInputV2.model_validate(
                dict(common, query_definition_digest=query.artifact_digest)
            )
        )
        materials.append(AcceptedStateRunMaterial(input=run_input, value=value))
    materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))
    return tuple(materials)


def prepare_direct_procedure_run(
    accepted: AcceptedProcedure,
    *,
    instance_id: str,
    accepted_coordinate: AcceptedCoordinate,
    invocation_input: object,
    actor_context: GovernedActorContext,
    state_reader: StateTapReaderProtocol,
    bodies: ContentAddressedBodyStore,
    journal_stream: JournalStreamIdentityV1,
    journal_partition_id: str | None,
    head_at_admission: AcceptedCoordinate | None = None,
    lane: Literal["current", "replay"] = "current",
    admitted_at: datetime,
    attempt: int = 1,
    runtime_policy: ProcedureRuntimePolicy | None = None,
) -> PreparedProcedureRun:
    """Bind exact accepted state and all pins for an actor's pure direct run.

    A pure run plans no Provider occurrence, so it binds no acquisition plan,
    Provider binding or deployment; its admission material manifest is empty.
    """

    procedure = accepted.procedure
    from cruxible_core.procedures.reference_binding import authored_procedure

    if procedure_artifact_digest(authored_procedure(accepted)).tagged != accepted.artifact_digest:
        raise ExecutionError("accepted Procedure artifact digest does not reproduce")

    materials = bind_accepted_state_materials(
        accepted,
        accepted_coordinate=accepted_coordinate,
        state_reader=state_reader,
        bodies=bodies,
        invocation_input=invocation_input,
    )
    node_pin_sets = _node_pin_sets(accepted)
    admitted_at = ensure_utc(admitted_at)
    admission = seal_procedure_run_admission(
        {
            "instance_id": instance_id,
            "attempt": attempt,
            "accepted_coordinate": accepted_coordinate,
            "bound_coordinate": accepted_coordinate,
            "head_at_admission": head_at_admission or accepted_coordinate,
            "lane": lane,
            "procedure_identity": procedure.identity,
            "procedure_path": accepted.path,
            "procedure_artifact_digest": accepted.artifact_digest,
            "definition_digest": procedure.definition_digest,
            "activation_policy": procedure.activation_policy,
            "full_pins": procedure.pins,
            "node_pin_sets": node_pin_sets,
            "pin_set_digest": procedure_pin_set_digest(procedure.pins, node_pin_sets),
            "invocation_input": invocation_input,
            "accepted_state_inputs": tuple(item.input for item in materials),
            "resource_budget": (
                resolve_procedure_resource_budget(
                    procedure.definition.budget, procedure.definition.hard_caps, runtime_policy
                )
                if runtime_policy is not None
                else None
            ),
            "budget": procedure.definition.budget,
            "hard_caps": procedure.definition.hard_caps,
            "actor_context": actor_context,
            "invocation_origin": "actor",
            "journal_stream": journal_stream,
            "journal_partition_id": journal_partition_id,
            "occurrence_evaluation_time": admitted_at,
            "admitted_at": admitted_at,
        },
        journal_partition_id=journal_partition_id,
    )
    manifest, manifest_digest = empty_admission_material_manifest()
    return PreparedProcedureRun(
        admission=admission,
        accepted_state_materials=materials,
        admission_material_manifest=manifest,
        admission_material_manifest_digest=manifest_digest,
    )


def empty_admission_material_manifest() -> tuple[ProcedureAdmissionMaterialManifest, str]:
    """The manifest of a run with no landed-Capture or exhaust input, and its digest."""

    manifest = ProcedureAdmissionMaterialManifest(members=())
    return manifest, procedure_admission_material_digest(manifest)


class _RunRefusal(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        detail_code: str | None = None,
        details: object | None = None,
        budget: ProcedureBudgetRefusalDetail | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal = ProcedureRunRefusalV1(
            code=code,
            message=message,
            node_id=node_id,
            detail_code=detail_code,
            details={} if details is None else details,
            budget=budget,
        )


def _workspace_refusal(
    exc: ValidationError | WorkspaceFileReadRefused, *, node_id: str
) -> _RunRefusal:
    """A workspace Source refusal, typed to its path class and naming its repair."""

    if isinstance(exc, WorkspaceFileReadRefused):
        details: dict[str, object] = {
            "path_class": exc.path_class,
            "repair_commands": list(exc.repair_commands),
        }
        if exc.workspace_names:
            details["workspace_names"] = list(exc.workspace_names)
        return _RunRefusal(
            "workspace_file_read_refused",
            str(exc),
            node_id=node_id,
            detail_code=exc.path_class,
            details=details,
        )
    return _RunRefusal(
        "workspace_file_read_refused",
        str(exc),
        node_id=node_id,
        detail_code="path_grammar",
        details={
            "path_class": "path_grammar",
            "repair_commands": ["use a normalized workspace-relative POSIX path"],
        },
    )


class ProcedureBoundaryRefused(ExecutionError):
    """A typed pre-execution or integrity refusal at a real public boundary."""

    def __init__(self, code: str, message: str, *, details: object | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = normalize_canonical({} if details is None else details)


class _ProviderNodeRefusal(_RunRefusal):
    """A node refusal from a Provider call.

    A refusal the bound operation declares is served as ``operation_refused``
    with the operation's own code as its ``detail_code``; a runtime refusal
    (a budget, say) keeps its runtime code.
    """

    def __init__(self, outcome: ProviderInvocationOutcome, *, node_id: str, declared: bool) -> None:
        code = outcome.code or "operation_refused"
        super().__init__(
            "operation_refused" if declared else code,
            outcome.message or "Provider invocation refused.",
            node_id=node_id,
            detail_code=code if declared else None,
            details=outcome.detail,
        )
        self.outcome = outcome


class _BudgetExceeded(Exception):
    def __init__(
        self,
        budget_kind: Literal["wall_clock", "max_provider_calls", "max_capture_bytes"],
        *,
        limit: int,
        observed: int,
        node_id: str,
    ) -> None:
        super().__init__(f"Procedure {budget_kind} budget exhausted")
        self.budget_kind = budget_kind
        self.limit = limit
        self.observed = observed
        self.node_id = node_id


class _OperationalFailure(Exception):
    def __init__(
        self,
        code: str,
        *,
        details: object | None = None,
        cause: ProcedureFailureCause | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.details = normalize_canonical({} if details is None else details)
        self.cause = cause


def _provider_failure_cause(
    outcome: ProviderInvocationOutcome, *, node_id: str
) -> ProcedureFailureCause:
    """The Provider's own account of an operational failure, for the run's final record."""

    detail = outcome.detail
    kind = detail.get("kind") if isinstance(detail, dict) else None
    return ProcedureFailureCause(
        node_id=node_id,
        provider_code=outcome.code or "provider_execution_error",
        kind=kind if isinstance(kind, str) and kind else None,
        message=outcome.message or "",
    )


class _InternalFailure(Exception):
    def __init__(self, code: str, *, details: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = normalize_canonical({} if details is None else details)


class _Halted(Exception):
    def __init__(self, *, node_id: str, reason: str | None, message: str | None = None) -> None:
        super().__init__("Procedure halted")
        self.node_id = node_id
        self.reason = reason
        self.message = message


class _TransformInputInvalid(ExecutionError):
    def __init__(self, message: str, *, slot: str) -> None:
        super().__init__(message)
        self.slot = slot


@dataclass
class _RunState:
    outputs: dict[str, CanonicalValue]
    input_payload: CanonicalValue
    parameters: CanonicalValue
    provenance: dict[str, AliasProvenanceV1] = dataclass_field(default_factory=dict)
    facts: dict[str, DependencyEvidenceFactsV1] = dataclass_field(default_factory=dict)
    outcomes: dict[str, AcquisitionInputOutcomeV1] = dataclass_field(default_factory=dict)
    control: frozenset[DependencyToken] = frozenset()
    return_tokens: frozenset[DependencyToken] = frozenset()
    terminal_capture: str | None = None
    provider_calls: int = 0
    capture_bytes: int = 0
    max_items_high_water: int = 0
    max_items_boundary: str | None = None
    max_items_field_path: str | None = None
    result_bytes_high_water: int = 0
    result_bytes_boundary: str | None = None
    result_bytes_field_path: str | None = None
    wall_clock_microseconds: int = 0
    acquisition_plan: ProcedureAcquisitionPlan | None = None
    invocation_receipt_digests: list[str] = dataclass_field(default_factory=list)
    source_capture_associations: list[ProcedureSourceCaptureAssociation] = dataclass_field(
        default_factory=list
    )
    provider_invocations_started: int = 0
    provider_invocations_completed: int = 0
    run_started_monotonic_ns: int = 0
    #: Items this run's post_inbox terminals posted, against the run's inbox cap,
    #: and their canonical bytes, against the run's inbox byte bound.
    inbox_items: int = 0
    inbox_bytes: int = 0
    #: Exact bytes of each Capture this run committed or was admitted, by
    #: Capture digest, for the extract nodes that decode them.
    capture_bodies: dict[str, bytes] = dataclass_field(default_factory=dict)
    #: The CaptureContract digest each such Capture was committed under.
    capture_contracts: dict[str, str] = dataclass_field(default_factory=dict)
    #: Which aliases hold incomplete values, and the control lineage's state.
    completeness: Completeness = dataclass_field(default_factory=Completeness)
    #: Whether the value this run returns is complete, data and control lineage
    #: both; a parent's invoke reads it from the child's completion record.
    return_complete: bool = True

    def observe_items(self, observed: int, boundary: str, field_path: str) -> None:
        candidate = (boundary.encode("utf-8"), field_path.encode("utf-8"))
        current = (
            b"" if self.max_items_boundary is None else self.max_items_boundary.encode("utf-8"),
            b"" if self.max_items_field_path is None else self.max_items_field_path.encode("utf-8"),
        )
        if observed > self.max_items_high_water or (
            observed == self.max_items_high_water
            and observed > 0
            and (self.max_items_boundary is None or candidate < current)
        ):
            self.max_items_high_water = observed
            self.max_items_boundary = boundary
            self.max_items_field_path = field_path

    def observe_result_bytes(self, observed: int, boundary: str, field_path: str) -> None:
        if observed > self.result_bytes_high_water:
            self.result_bytes_high_water = observed
            self.result_bytes_boundary = boundary
            self.result_bytes_field_path = field_path

    def alias_tokens(self, aliases: frozenset[str]) -> frozenset[DependencyToken]:
        tokens: frozenset[DependencyToken] = frozenset()
        for alias in sorted(aliases):
            found = self.provenance.get(alias)
            if found is not None:
                tokens |= found.whole
        return tokens

    def item_tokens(self, alias: str, index: int) -> frozenset[DependencyToken]:
        found = self.provenance.get(alias)
        return frozenset() if found is None else found.item(index)


def _effective_max_items(admission: ProcedureRunAdmission) -> int:
    return (
        admission.hard_caps.max_items
        if admission.budget.max_items is None
        else admission.budget.max_items
    )


def _kernel_list_boundary(
    validator: ContractValidatorProtocol,
    contract: ArtifactPin,
) -> tuple[str, str] | None:
    field_path = validator.unique_list_field_path(contract)
    if not isinstance(field_path, str):
        return None
    return (f"contract-out:{contract.target.name}", field_path)


class ProcedureExecutor:
    """Execute one admitted graph while making the journal the operational authority."""

    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        run_index: ProcedureRunIndex,
        fencing_token: str,
        activation_authority: ProcedureActivationAuthorityProtocol,
        contract_validator: ContractValidatorProtocol,
        provider_runtime_invoker: ProviderRuntimeInvokerProtocol | None = None,
        provider_runtime_invoker_factory: Callable[[], ProviderRuntimeInvokerProtocol]
        | None = None,
        capture_contracts: Mapping[str, CaptureContract] | None = None,
        acquisition_rules: AcquisitionRules | None = None,
        default_authorizations: tuple[str, ...] = (),
        effective_rung: EffectiveRungV1 | None = None,
        egress_sink: TerminalEgressSinkProtocol | None = None,
        declared_effect_grants: tuple[str, ...] = (),
        workspace_file_reader: WorkspaceFileReader | None = None,
        clock: ProcedureClockProtocol | None = None,
        nested_runner: NestedProcedureRunner | None = None,
        parent_context: ParentInvocationContext | None = None,
        ephemeral_spec_digest: str | None = None,
        enum_resolver: EnumResolver | None = None,
        contract_resolver: Callable[[ContractRef], Contract | None] | None = None,
        completeness_probe: CompletenessProbe | None = None,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.material_reservations = ProcedureMaterialReservationStore(bodies.reservation_root)
        self.run_index = run_index
        self.fencing_token = fencing_token
        self.activation_authority = activation_authority
        self.contract_validator = contract_validator
        self.provider_runtime_invoker = provider_runtime_invoker
        self.provider_runtime_invoker_factory = provider_runtime_invoker_factory
        self.capture_contracts = dict(capture_contracts or {})
        # Execution reads acquisition only through the port.
        self.acquisition_rules = acquisition_rules
        # Each slot's exact build and operation, from the execution plan.
        self.slot_bindings: dict[str, Any] = {}
        # The execution plan this executor runs, and each named dependency's
        # exact digest from it.
        self.plan: ProcedureExecutionPlan | None = None
        self.references: dict[str, str] = {}
        self.default_authorizations = default_authorizations
        self.effective_rung = effective_rung
        self.egress_sink = egress_sink
        self.declared_effect_grants = declared_effect_grants
        self.workspace_file_reader = workspace_file_reader
        self.clock = clock or SystemProcedureClock()
        self.nested_runner = nested_runner
        self.parent_context = parent_context
        # F12: a direct run's ephemeral Line spec digest (ruling C4), recorded
        # on its attempt_started event -- the Admission itself never carries
        # it (PC-E1 reserves line/mandate/calibration coordinates for Line
        # runs), so this is its only retained, verifiable record.
        self.ephemeral_spec_digest = ephemeral_spec_digest
        # F07: resolves enum_of<ClaimType> members at the admitted (immutable)
        # coordinate; replay re-reads the same pinned coordinate and so sees
        # the same members deterministically -- nothing extra needs retaining.
        self.enum_resolver = enum_resolver
        # Resolves an extract's row Contract (and the Contracts it nests) at the
        # admitted coordinate, by name, version and pinned digest.
        self.contract_resolver = contract_resolver
        # A sink harness told whether each sink's value is complete (the law's
        # real sinks belong to the Claims terminal).
        self.completeness_probe = completeness_probe

    def _use_plan(self, accepted: AcceptedProcedure) -> ProcedureExecutionPlan:
        """Run against ``accepted``'s execution plan: its references resolve through it."""

        plan = execution_plan(accepted)
        self.plan = plan
        self.references = dict(plan.resolved)
        return plan

    def _pin(self, binding: ArtifactNameRef, *, label: str) -> ArtifactPin:
        """The exact pin one node reference resolved to at admission."""

        if self.plan is None or binding.target.qualified not in self.plan.resolved:
            raise ExecutionError(f"{label} was not resolved at admission")
        return self.plan.pin(binding)

    def execute(
        self,
        prepared: PreparedProcedureRun,
        accepted: AcceptedProcedure,
    ) -> ProcedureRunResultV1:
        admission = prepared.admission
        plan = self._use_plan(accepted)
        self._verify_correspondence(admission, accepted, prepared.accepted_state_materials)
        self.slot_bindings = {item.slot: item for item in plan.slot_bindings}
        try:
            existing_records = self.journal.select_records(
                admission.journal_stream,
                partition_id=admission.journal_partition_id,
                run_id=admission.run_id,
            )
            self.run_index.rebuild_run(
                admission.run_id,
                existing_records,
                bodies=self.bodies,
            )
        except (JournalError, ValueError) as exc:
            raise ProcedureBoundaryRefused(
                "journal_integrity_error",
                "Procedure journal history failed integrity verification.",
            ) from exc
        indexed = self.run_index.get(admission.run_id)
        if indexed is not None:
            if indexed.admission_binding_digest != admission.admission_binding_digest:
                raise ExecutionError("run_id collides across distinct admission bindings")
            if indexed.status == "failed" and indexed.final_payload_digest is None:
                return ProcedureRunResultV1(
                    run_id=admission.run_id,
                    status="failed",
                    receipt=self._receipt(admission, list(existing_records)),
                )
            if indexed.status != "running":
                return self._replay_completed(admission, existing_records)
            effect_state = (
                " with an unresolved durable effect intent"
                if indexed.effect_intent_count != indexed.effect_result_count
                else ""
            )
            provider_state = (
                " with an incomplete Provider invocation"
                if indexed.provider_invocation_started_count
                != indexed.provider_invocation_completed_count
                else ""
            )
            terminal_state = (
                " with an unresolved prepared terminal egress"
                if indexed.terminal_egress_prepared_count != indexed.terminal_egress_resolved_count
                else ""
            )
            raise ExecutionError(
                "run_recovery_required: admitted run has incomplete exhaust"
                + effect_state
                + provider_state
                + terminal_state
            )
        self._require_current(admission)
        if any(isinstance(node, InvokeNode) for node in accepted.procedure.definition.nodes):
            if self.nested_runner is None:
                raise ExecutionError("nested Procedure runner is unavailable")
            self.nested_runner.preflight(accepted, admission)
        if prepared.acquisition_plan is not None:
            self._preflight_source_runtime(prepared, accepted)
            active_reservations = {
                item.reservation_id for item in self.material_reservations.active()
            }
            missing_reservations = sorted(
                set(prepared.required_reservation_ids) - active_reservations
            )
            if missing_reservations:
                raise ExecutionError(
                    "admission_material_reservation_missing: complete acquisition plan "
                    "reservations must exist before attempt_started"
                )
        records: list[StoredProcedureJournalRecordV1] = []
        started_ns = self.clock.monotonic_ns()
        if self.parent_context is not None and self.parent_context.deadline_ns is not None:
            # Child planning also consumes the ancestor's remaining time. Its
            # execution clock cannot restart that deadline after planning ends.
            started_ns = min(
                started_ns,
                self.parent_context.deadline_ns - admission.budget.wall_clock.microseconds * 1000,
            )

        # An automatic Line dispatch orders this admission against a disarm; a
        # child run inherits its parent's admission and is never gated again.
        gate = LINE_ARM_ADMISSION_GATE.get() if self.parent_context is None else None
        with gate() if gate is not None else nullcontext():
            attempt_started_payload: dict[str, object] = {
                "attempt": admission.attempt,
                "admitted_at": admission.admitted_at.isoformat(),
            }
            if self.ephemeral_spec_digest is not None:
                attempt_started_payload["ephemeral_spec_digest"] = self.ephemeral_spec_digest
            self._append_event(
                admission,
                records,
                "attempt_started",
                attempt_started_payload,
            )
            self._append_event(
                admission,
                records,
                "admission_bound",
                prepared.bound_payload().model_dump(mode="json"),
            )

        state = _RunState(
            outputs={},
            input_payload=normalize_canonical(admission.invocation_input),
            parameters={},
        )
        status: ProcedureRunStatusV1
        output: CanonicalValue | None = None
        refusal: ProcedureRunRefusalV1 | None = None
        failure_message: str | None = None
        failure_code: str | None = None
        failure_details: object = {}
        failure_cause: ProcedureFailureCause | None = None
        halt: CanonicalValue | None = None
        try:
            state = self._seed_state(prepared)
            state.run_started_monotonic_ns = started_ns
            input_contract = self._pin(
                accepted.procedure.definition.contract_in,
                label="Procedure contract_in",
            )
            state.input_payload = _validate_node_contract(
                self.contract_validator,
                contract=input_contract,
                payload=state.input_payload,
                direction="input",
                node_id="procedure",
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            output = self._walk(
                accepted,
                admission=admission,
                state=state,
                records=records,
                started_ns=started_ns,
            )
            status = "succeeded"
        except ProcedureBoundaryRefused as exc:
            if exc.code in {
                "not_current",
                "pin_binding_mismatch",
                "input_material_mismatch",
                "state_tap_refused",
                "provider_replay_receipt_required",
            }:
                status = "refused"
                refusal = ProcedureRunRefusalV1(
                    code=exc.code,
                    message=str(exc),
                    details=exc.details,
                )
            elif exc.code in {"journal_append_failed", "journal_conflict"}:
                status = "failed"
                failure_code = exc.code
                failure_details = exc.details
                failure_message = "Procedure journal operation failed."
            else:
                status = "failed"
                failure_code = exc.code
                failure_details = exc.details
                failure_message = "Procedure integrity verification failed."
        except _RunRefusal as exc:
            status = "refused"
            refusal = exc.refusal
        except _Halted as exc:
            status = "halted"
            halt = normalize_canonical(
                {
                    "node_id": exc.node_id,
                    "reason": exc.reason,
                    **({"message": exc.message} if exc.message is not None else {}),
                }
            )
        except _BudgetExceeded as exc:
            if exc.budget_kind == "wall_clock":
                status = "failed"
                failure_code = "wall_clock_exhausted"
                failure_message = "Procedure execution exceeded its operational budget."
            else:
                status = "refused"
                failure_message = "Procedure execution exhausted its declared budget."
                refusal = ProcedureRunRefusalV1(
                    code="budget_exhausted",
                    message=failure_message,
                    node_id=exc.node_id,
                    budget=ProcedureBudgetRefusalDetail(
                        budget_kind=exc.budget_kind,
                        limit=exc.limit,
                        observed=exc.observed,
                    ),
                )
        except _OperationalFailure as exc:
            status = "failed"
            failure_code = exc.code
            failure_details = exc.details
            failure_cause = exc.cause
            failure_message = "Procedure execution failed."
        except _InternalFailure as exc:
            status = "failed"
            failure_code = exc.code
            failure_details = exc.details
            failure_message = "Procedure execution failed."
        except ExecutionError:
            status = "failed"
            failure_code = "unexpected_exception"
            failure_message = "Procedure execution failed."
        except Exception as exc:
            status = "failed"
            failure_message = f"{type(exc).__name__}: {exc}"
            failure_code = "unexpected_exception"

        if state.provider_invocations_started != state.provider_invocations_completed:
            raise ExecutionError(
                "provider_completion_not_durable: Provider result was not committed"
            )
        semantic_result_digest = None
        if status in {
            "succeeded",
            "refused",
            "halted",
        }:
            semantic_result_digest = procedure_semantic_result_digest(
                semantic_replay_key_digest=admission.semantic_replay_key_digest,
                status=cast(Literal["succeeded", "refused", "halted"], status),
                output=output,
                refusal=refusal,
                halt=halt,
            )
        final_payload = {
            "status": status,
            **(
                {
                    "return_manifest": build_terminal_item_manifest(
                        state.return_tokens,
                        run_id=admission.run_id,
                        terminal_node_id="return",
                        item_key="result",
                    ).model_dump(mode="json"),
                    "dependency_facts": {
                        token.digest: state.facts[token.digest].model_dump(mode="json")
                        for token in state.return_tokens
                        if token.digest in state.facts
                    },
                    "terminal_capture": state.terminal_capture,
                    # Recorded only when incomplete, so complete runs keep their bytes.
                    **(
                        {"return_complete": False}
                        if status == "succeeded" and not state.return_complete
                        else {}
                    ),
                }
                if isinstance(accepted.procedure.definition, ProcedureDefinition)
                or admission.parent_binding is not None
                else {}
            ),
            "output": output,
            "refusal": None if refusal is None else refusal.model_dump(mode="json"),
            "failure": failure_message,
            "failure_code": failure_code,
            "failure_details": failure_details,
            # Only a Provider's operational failure carries a cause; every other
            # final record keeps its bytes.
            **(
                {}
                if failure_cause is None
                else {"failure_cause": failure_cause.model_dump(mode="json")}
            ),
            "halt": halt,
            "semantic_result_digest": semantic_result_digest,
            "provider_calls": state.provider_calls,
            "capture_bytes": state.capture_bytes,
            "invocation_receipt_digests": state.invocation_receipt_digests,
            "source_capture_associations": [
                item.model_dump(mode="json")
                for item in sorted(
                    state.source_capture_associations,
                    key=lambda item: (
                        item.occurrence_path.encode("utf-8"),
                        item.field.encode("utf-8"),
                    ),
                )
            ],
            "budget": ProcedureRunBudget(
                declared=procedure_run_declared_budget(admission),
                observed=ProcedureRunBudgetObserved(
                    max_items=ProcedureBudgetBoundaryObservation(
                        high_water=state.max_items_high_water,
                        boundary=state.max_items_boundary,
                        field_path=state.max_items_field_path,
                    ),
                    result_bytes=ProcedureBudgetBoundaryObservation(
                        high_water=state.result_bytes_high_water,
                        boundary=state.result_bytes_boundary,
                        field_path=state.result_bytes_field_path,
                    ),
                    provider_calls=state.provider_calls,
                    capture_bytes=state.capture_bytes,
                    wall_clock_microseconds=state.wall_clock_microseconds,
                ),
            ).model_dump(mode="json"),
        }
        self._append_event(
            admission,
            records,
            "attempt_finalized",
            final_payload,
        )
        receipt = self._receipt(admission, records)
        return ProcedureRunResultV1(
            run_id=admission.run_id,
            status=status,
            output=output if status == "succeeded" else None,
            refusal=refusal,
            receipt=receipt,
        )

    def _preflight_source_runtime(
        self,
        prepared: PreparedProcedureRun,
        accepted: AcceptedProcedure,
    ) -> None:
        """Refuse an incomplete Source closure before the first attempt record."""

        sources = tuple(
            node
            for node in accepted.procedure.definition.nodes
            if isinstance(node, CallNode) and node.acquires
        )
        if not sources:
            return
        admission = prepared.admission
        plan = prepared.acquisition_plan
        selection = admission.selection_decision
        if plan is None or selection is None:
            raise ExecutionError("source_acquisition_plan_mismatch: no admitted acquisition plan")
        if (
            self.acquisition_rules is None
            or self.acquisition_rules.digest != admission.acquisition_policy_digest
        ):
            raise ExecutionError(
                "source_acquisition_plan_mismatch: accepted Source policy is unavailable"
            )
        for node in sources:
            matches = tuple(
                occurrence
                for occurrence in plan.external_occurrences
                if occurrence.occurrence_kind == "source"
                and occurrence.node_id == node.node_id
                and occurrence.repeat_node_id is None
            )
            # Each capture field is pinned, ruled and decided on its own.
            pins = {
                field: self._pin(reference, label=f"call {node.node_id!r} captures")
                for field, reference in node.captures.items()
            }
            contracts = {
                field: self.capture_contracts.get(pin.artifact_digest)
                for field, pin in pins.items()
            }
            if any(
                contract is None
                or capture_contract_digest(contract).tagged != pins[field].artifact_digest
                or self._acquisition_rule(node, field) is None
                for field, contract in contracts.items()
            ):
                raise ExecutionError(
                    "source_acquisition_plan_mismatch: Source closure does not reproduce"
                )
            supplied = tuple(
                item
                for item in admission.landed_capture_inputs
                if item.input_name.partition(".")[0] == node.as_
            )
            if supplied:
                # Only a one-field call is substituted: its one capture output.
                field, contract_pin = next(iter(pins.items()))
                key = f"{node.as_}.{field}"
                selected = next(
                    (item for item in selection.decisions if item.input_name == key),
                    None,
                )
                captured = (
                    self.capture_contracts.get(supplied[0].capture_contract_digest)
                    if len(supplied) == 1
                    else None
                )
                if (
                    matches
                    or len(pins) != 1
                    or len(supplied) != 1
                    or supplied[0].input_name != key
                    or captured is None
                    or captured.identity != contract_pin.target
                    or capture_contract_digest(captured).tagged
                    != supplied[0].capture_contract_digest
                    or selected is None
                    or selected.disposition != "selected"
                    or selected.selected_capture_digests != (supplied[0].capture_digest,)
                ):
                    raise ExecutionError(
                        "source_acquisition_plan_mismatch: supplied Source closure differs"
                    )
                continue
            if len(matches) != 1:
                raise ExecutionError(
                    "source_acquisition_plan_mismatch: Source occurrence is not exact"
                )
            occurrence = matches[0]
            if (
                occurrence.input_name != node.as_
                or occurrence.capture_contracts
                != {field: pin.artifact_digest for field, pin in pins.items()}
                or not self._occurrence_matches_slot(occurrence, node.slot)
                or occurrence.effect_class == "external_mutation"
            ):
                raise ExecutionError(
                    "source_acquisition_plan_mismatch: Source closure does not reproduce"
                )

    def _seed_state(self, prepared: PreparedProcedureRun) -> _RunState:
        """Bind every admitted plane's material and its provenance before node one."""

        admission = prepared.admission
        state = _RunState(
            outputs={},
            input_payload=normalize_canonical(admission.invocation_input),
            parameters={},
            outcomes={item.input_name: item for item in prepared.acquisition_outcomes},
            acquisition_plan=prepared.acquisition_plan,
        )
        for accepted_state in prepared.accepted_state_materials:
            access = BodyAccessContext(
                principal_id="procedure-runtime",
                can_read_body=True,
            )
            try:
                retained = self.bodies.read(
                    accepted_state.input.material_body_digest,
                    access=access,
                )
            except Exception as exc:
                raise _OperationalFailure("cas_unavailable_at_replay") from exc
            try:
                retained_value = normalize_canonical(json.loads(retained))
            except Exception as exc:
                raise _OperationalFailure("replay_material_mismatch") from exc
            if (
                canonical_bytes(retained_value) != retained
                or run_value_digest("state-result", retained_value)
                != accepted_state.input.result_digest
            ):
                raise _OperationalFailure("replay_material_mismatch")
            value = retained_value
            digest = run_input_digest(accepted_state.input)
            name = accepted_state.input.input_name
            state.outputs[name] = normalize_canonical(value)
            state.provenance[name] = AliasProvenanceV1(
                whole=frozenset(
                    {
                        accepted_state_token(digest),
                        policy_token(accepted_read_definition_digest(accepted_state.input)),
                    }
                )
            )
            state.facts[digest] = DependencyEvidenceFactsV1(
                epistemic_grade="observed",
                provenance_grade="self-asserted",
                taint_labels=(TAINT_ACCEPTED_STATE,),
            )
        for landed in prepared.landed_capture_materials:
            # A landed Capture is one capture output, <alias>.<field>; the
            # substituted call's alias holds its value.
            name = landed.input.input_name.partition(".")[0]
            state.outputs[name] = normalize_canonical(landed.material.value)
            state.capture_bytes += len(canonical_bytes(landed.material.value))
            tokens = {
                admitted_capture_token(landed.input.capture_digest),
                policy_token(landed.input.capture_contract_digest),
            }
            if admission.acquisition_policy_digest is not None:
                tokens.add(policy_token(admission.acquisition_policy_digest))
            if admission.selection_receipt_digest is not None:
                tokens.add(receipt_token(admission.selection_receipt_digest))
            envelope = landed.material.envelope
            tokens.add(
                receipt_token(
                    envelope.run_receipt_digest
                    if isinstance(envelope, CaptureEnvelopeV1)
                    else envelope.producer_receipt_digest
                )
            )
            state.provenance[name] = AliasProvenanceV1(whole=frozenset(tokens))
            contract = self.capture_contracts.get(landed.input.capture_contract_digest)
            state.facts[landed.input.capture_digest] = DependencyEvidenceFactsV1(
                selector_privacy=(
                    capture_retention_policy(contract).selector_privacy
                    if contract is not None
                    else "direct_allowed"
                ),
                epistemic_grade=landed.material.epistemic_grade,
                provenance_grade=landed.material.provenance_grade,
                acquisition_input_name=landed.input.input_name,
            )
        for exhaust in prepared.exhaust_materials:
            digest = run_input_digest(exhaust.input)
            name = exhaust.input.input_name
            state.outputs[name] = normalize_canonical(exhaust.value)
            state.provenance[name] = AliasProvenanceV1(
                whole=frozenset(
                    {
                        exhaust_token(digest),
                        policy_token(exhaust.input.reducer_or_query_digest),
                    }
                )
            )
            state.facts[digest] = DependencyEvidenceFactsV1(
                epistemic_grade="derived",
                provenance_grade="self-asserted",
                taint_labels=(TAINT_UNPROMOTED_EXHAUST,),
            )
        return state

    def _verify_correspondence(
        self,
        admission: ProcedureRunAdmission,
        accepted: AcceptedProcedure,
        state_materials: tuple[AcceptedStateRunMaterial, ...],
    ) -> None:
        procedure = accepted.procedure
        if (
            accepted.path != admission.procedure_path
            or accepted.artifact_digest != admission.procedure_artifact_digest
            or procedure.definition_digest != admission.definition_digest
            or procedure.identity != admission.procedure_identity
            or procedure.activation_policy != admission.activation_policy
            or (
                admission.parent_binding is None
                and procedure.definition.hard_caps != admission.hard_caps
            )
        ):
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "Procedure admission and accepted artifact differ.",
            )
        if admission.parent_binding is not None:
            if self.parent_context is None:
                raise ExecutionError("nested admission requires its executing parent")
            self.parent_context.verify(admission, accepted)
            if self.effective_rung != self.parent_context.child_rung(accepted):
                raise ExecutionError("nested invocation changed the parent effective rung")
        elif self.parent_context is not None:
            raise ExecutionError("a child cannot execute under a standalone admission")
        if admission.invocation_origin == "actor":
            if procedure.pins != admission.full_pins:
                raise ProcedureBoundaryRefused(
                    "pin_binding_mismatch",
                    "Procedure admission pins differ from the accepted artifact.",
                )
        elif not set(procedure.pins).issubset(set(admission.full_pins)):
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "Line run pins must close the accepted Procedure pins exactly.",
            )
        if admission.invocation_origin == "actor" and admission.parent_binding is None:
            if procedure.definition.budget != admission.budget:
                raise ExecutionError("Procedure admission and accepted artifact differ")
        else:
            caps = procedure.definition.hard_caps
            if (
                admission.budget.wall_clock.microseconds > caps.max_wall_clock.microseconds
                or admission.budget.max_provider_calls > caps.max_provider_calls
                or admission.budget.max_capture_bytes > caps.max_capture_bytes
                or (
                    admission.budget.max_items is not None
                    and admission.budget.max_items > caps.max_items
                )
            ):
                raise ExecutionError("Line run budget exceeds the Procedure hard caps")
        if _node_pin_sets(accepted) != admission.node_pin_sets:
            raise ProcedureBoundaryRefused(
                "pin_binding_mismatch",
                "Procedure node pins changed after admission.",
            )
        if admission.resource_budget is not None:
            for node in procedure.definition.nodes:
                if (
                    isinstance(node, RepeatNode)
                    and node.max_attempts > admission.resource_budget.repeat_attempts_cap
                ):
                    raise ProcedureBoundaryRefused(
                        "budget_max_repeat_attempts_exceeded",
                        "Repeat exceeds admitted instance ceiling.",
                    )
        self._verify_effective_rung(admission)
        self._verify_input_planes(admission, accepted)
        retained_values = {
            item.input.input_name: normalize_canonical(item.value) for item in state_materials
        }
        retained_values.update(
            {
                node.as_: normalize_canonical(node.literal)
                for node in procedure.definition.nodes
                if isinstance(node, ProjectNode) and node.literal is not None
            }
        )
        expected_state_inputs = {
            node.as_: (
                self._pin(
                    cast(ArtifactNameRef, node.claim_type or node.query),
                    label=f"state read {node.node_id!r}",
                ),
                run_value_digest(
                    "state-parameters",
                    state_tap_parameters(
                        node, invocation_input=admission.invocation_input, values=retained_values
                    ),
                ),
            )
            for node in procedure.definition.nodes
            if isinstance(node, ReadNode)
        }
        actual_state_inputs = {item.input_name: item for item in admission.accepted_state_inputs}
        if set(actual_state_inputs) != set(expected_state_inputs):
            raise ProcedureBoundaryRefused(
                "input_material_mismatch",
                "Procedure admission state_tap input set differs.",
            )
        for name, (query, parameters_digest) in expected_state_inputs.items():
            actual = actual_state_inputs[name]
            if (
                accepted_read_definition_digest(actual) != query.artifact_digest
                or actual.parameters_digest != parameters_digest
                or actual.read_coordinate != admission.accepted_coordinate
            ):
                raise ProcedureBoundaryRefused(
                    "input_material_mismatch",
                    f"Procedure admission state_tap input {name!r} differs.",
                )

    def _verify_effective_rung(self, admission: ProcedureRunAdmission) -> None:
        """Refuse a five-term cap computed against any other admission binding.

        The rung is authority, so it is checked against the frozen tuple rather
        than recomputed here: every term already resolved through the mandate,
        calibration, and sensitivity coordinates this admission bound. A direct
        (``actor``) admission may also bind one: ruling C4's ephemeral Line
        spec, capped at propose. The digest correspondence below is what
        actually ties the rung to this exact admission in either case.
        """

        rung = self.effective_rung
        if rung is None:
            return
        if admission.invocation_origin == "actor":
            # PC-E1 reserves line_spec_digest/sensitivity_policy_digest/
            # mandate_coordinate_digest/calibration_coordinate_digest for Line
            # runs: a direct admission binds none of them. Its ephemeral rung
            # corresponds to this admission by the Procedure version alone.
            if rung.procedure_definition_digest != admission.definition_digest:
                raise ExecutionError(
                    "effective rung was computed against another admission binding"
                )
            return
        if admission.invocation_origin != "line":
            raise ExecutionError(
                "an effective rung binds a Line run or a direct run's ephemeral spec"
            )
        if (
            rung.procedure_definition_digest != admission.definition_digest
            or rung.line_spec_digest != admission.line_spec_digest
            or rung.sensitivity_policy_digest != admission.sensitivity_policy_digest
            or rung.mandate_coordinate_digest != admission.mandate_coordinate_digest
            or rung.calibration_coordinate_digest != admission.calibration_coordinate_digest
        ):
            raise ExecutionError("effective rung was computed against another admission binding")

    def _verify_input_planes(
        self,
        admission: ProcedureRunAdmission,
        accepted: AcceptedProcedure,
    ) -> None:
        """Refuse every cross-plane relabel before the first node can observe a value."""
        self._use_plan(accepted)

        # A read is named by its alias; an acquiring call's admitted input by the
        # capture output it binds, <alias>.<field>.
        nodes: dict[str, tuple[ReadNode | CallNode, str | None]] = {}
        for node in accepted.procedure.definition.nodes:
            if isinstance(node, ReadNode):
                nodes[node.as_] = (node, None)
            elif isinstance(node, CallNode):
                for name in node.captures:
                    nodes[f"{node.as_}.{name}"] = (node, name)
        for run_input in admission.run_inputs:
            found = nodes.get(run_input.input_name)
            if found is None:
                raise ProcedureBoundaryRefused(
                    "input_material_mismatch",
                    f"Admitted input {run_input.input_name!r} names no Procedure input node.",
                )
            node, field = found
            try:
                validate_node_input_plane(node, run_input)
                if (
                    isinstance(run_input, LandedCaptureRunInputV1)
                    and isinstance(node, CallNode)
                    and field is not None
                    and run_input.capture_contract_digest
                    != self._pin(
                        node.captures[field], label=f"call {node.node_id!r} captures"
                    ).artifact_digest
                ):
                    raise ValueError(
                        "landed Capture does not match its capture field's CaptureContract"
                    )
            except ValueError as exc:
                raise ProcedureBoundaryRefused(
                    "input_material_mismatch",
                    f"Admitted input plane was relabelled: {exc}",
                ) from exc

    def _require_current(self, admission: ProcedureRunAdmission) -> None:
        if admission.lane == "replay":
            return
        current = self.activation_authority.current_procedure_digest(
            admission.procedure_identity,
            coordinate=admission.accepted_coordinate,
        )
        if current != admission.procedure_artifact_digest:
            raise ProcedureBoundaryRefused(
                "not_current",
                "Procedure is not current at the admitted coordinate.",
            )

    def _checkpoint_current(self, admission: ProcedureRunAdmission, *, effect: bool) -> None:
        parent_binding = admission.parent_binding
        if parent_binding is not None and admission.lane != "replay":
            for pin, policy in zip(
                parent_binding.ancestors,
                parent_binding.activation_policies,
                strict=True,
            ):
                if policy == "abort" or (policy == "epoch-check" and effect):
                    if (
                        self.activation_authority.current_procedure_digest(
                            pin.target, coordinate=admission.accepted_coordinate
                        )
                        != pin.artifact_digest
                    ):
                        raise ProcedureBoundaryRefused(
                            "not_current", "An ancestor Procedure is no longer current."
                        )
        if admission.activation_policy == "abort" or (
            admission.activation_policy == "epoch-check" and effect
        ):
            self._require_current(admission)

    def _check_budget(
        self,
        admission: ProcedureRunAdmission,
        state: _RunState,
        *,
        started_ns: int,
        node_id: str,
        wall_clock: bool = True,
    ) -> None:
        """Refuse a run past any declared budget.

        ``wall_clock=False`` after a terminal node: its deadline was checked
        inside the terminal, before anything was delivered, and a run whose
        effect was delivered must not then report that it ran out of time.
        """

        elapsed_us = (self.clock.monotonic_ns() - started_ns) // 1000
        state.wall_clock_microseconds = max(state.wall_clock_microseconds, elapsed_us)
        if wall_clock and elapsed_us > admission.budget.wall_clock.microseconds:
            raise _BudgetExceeded(
                "wall_clock",
                limit=admission.budget.wall_clock.microseconds,
                observed=elapsed_us,
                node_id=node_id,
            )
        if state.provider_calls > admission.budget.max_provider_calls:
            raise _BudgetExceeded(
                "max_provider_calls",
                limit=admission.budget.max_provider_calls,
                observed=state.provider_calls,
                node_id=node_id,
            )
        if state.capture_bytes > admission.budget.max_capture_bytes:
            raise _BudgetExceeded(
                "max_capture_bytes",
                limit=admission.budget.max_capture_bytes,
                observed=state.capture_bytes,
                node_id=node_id,
            )

    def _walk(
        self,
        accepted: AcceptedProcedure,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> CanonicalValue:
        definition = accepted.procedure.definition
        graph = analyze_procedure(definition)
        nodes = {node.node_id: node for node in definition.nodes}
        current = definition.nodes[0].node_id
        while True:
            try:
                node = nodes[current]
            except KeyError as exc:
                raise ProcedureBoundaryRefused(
                    "compiler_invariant_broken",
                    "Compiled control flow names no Procedure node.",
                    details={"node_id": current},
                ) from exc
            self._checkpoint_current(admission, effect=False)
            self._check_budget(
                admission,
                state,
                started_ns=started_ns,
                node_id=node.node_id,
            )
            try:
                branch = self._execute_node(
                    node,
                    accepted=accepted,
                    admission=admission,
                    state=state,
                    records=records,
                    started_ns=started_ns,
                )
                self._check_budget(
                    admission,
                    state,
                    started_ns=started_ns,
                    node_id=node.node_id,
                    wall_clock=not isinstance(
                        node, CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode
                    ),
                )
                self._track_completeness(node, state)
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "succeeded"},
                )
                if isinstance(node, HaltNode):
                    raise _Halted(node_id=node.node_id, reason=node.reason, message=node.message)
            except _RunRefusal:
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "refused"},
                )
                raise
            except _Halted:
                raise
            except Exception:
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "failed"},
                )
                raise

            edges = graph.edges[node.node_id]
            target: str | None
            if isinstance(node, GuardNode):
                target = edges[branch or "on_false"]
            else:
                target = edges.get("next")
            if target == "$abort":
                if isinstance(node, GuardNode):
                    raise _RunRefusal(
                        "guard_refused",
                        node.message or "Procedure guard refused.",
                        node_id=node.node_id,
                        detail_code=node.refusal_code or "guard_refused",
                    )
                raise _RunRefusal(
                    "guard_refused",
                    "Procedure reached an explicit abort edge.",
                    node_id=node.node_id,
                    detail_code="procedure.abort",
                )
            if target is None:
                try:
                    if isinstance(node, ProjectNode) and node.leaf:
                        result = state.outputs[node.as_]
                        state.return_tokens = (
                            state.alias_tokens(frozenset({node.as_})) | state.control
                        )
                        state.return_complete = state.completeness.at_sink(_node_reads(node))
                    elif isinstance(node, CaptureEgressNode | ProposeChangeSetNode):
                        result = _resolve_node_template(
                            node.result,
                            node_id=node.node_id,
                            transform_kind=None,
                            input_payload=state.input_payload,
                            outputs=state.outputs,
                        )
                        state.return_tokens = _base_tokens(
                            node, state, node.result, self.references
                        )
                        state.return_complete = state.completeness.at_sink(
                            _referenced_reads(node.result)
                        )
                        if state.terminal_capture is not None:
                            state.return_tokens |= frozenset(
                                {produced_capture_token(state.terminal_capture)}
                            )
                    else:
                        if definition.returns is None:
                            raise ExecutionError("Procedure must finish at an explicit return path")
                        result = state.outputs[definition.returns]
                        state.return_tokens = (
                            state.alias_tokens(frozenset({definition.returns})) | state.control
                        )
                        state.return_complete = state.completeness.at_sink(
                            ((definition.returns, ()),)
                        )
                except KeyError as exc:  # pragma: no cover - static law should prevent
                    raise ExecutionError("Procedure return alias was not produced") from exc
                if self.completeness_probe is not None and not is_terminal_node(node):
                    self.completeness_probe(node.node_id, state.return_complete)
                return_contract = self._pin(
                    definition.contract_out,
                    label="Procedure contract_out",
                )
                result = _validate_node_contract(
                    self.contract_validator,
                    contract=return_contract,
                    payload=result,
                    direction="output",
                    node_id=node.node_id,
                    max_items=_effective_max_items(admission),
                    observe_items=state.observe_items,
                )
                _check_return_budget(
                    result,
                    max_items=None,
                    node_id=node.node_id,
                    result_bytes_cap=(
                        admission.resource_budget.result_bytes_cap
                        if admission.resource_budget is not None
                        else 1_048_576
                    ),
                    observe_result_bytes=state.observe_result_bytes,
                    boundary="procedure-return",
                    field_path=definition.returns or node.node_id,
                )
                try:
                    return normalize_canonical(result)
                except Exception as exc:
                    raise _RunRefusal(
                        "result_not_canonical",
                        "The Procedure result is not canonical.",
                        node_id=node.node_id,
                    ) from exc
            current = target

    def _execute_node(
        self,
        node: object,
        *,
        accepted: AcceptedProcedure,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> Literal["on_true", "on_false"] | None:
        if isinstance(node, InvokeNode):
            self._run_child(
                node,
                accepted=accepted,
                admission=admission,
                state=state,
                records=records,
                started_ns=started_ns,
            )
            return None
        if isinstance(node, ReadNode):
            if node.as_ not in state.outputs:
                raise ExecutionError("admitted read material is absent")
            self._extend_alias(
                state, node.as_, _node_policy_tokens(node, self.references) | state.control
            )
            return None
        if isinstance(node, HaltNode):
            return None
        if isinstance(node, CallNode) and node.acquires:
            self._run_acquiring_call(node, admission=admission, state=state, records=records)
            return None
        if isinstance(node, ExtractNode):
            self._run_extract(node, state=state)
            return None
        if isinstance(node, CallNode):
            self._run_provider_v4(
                node,
                admission=admission,
                state=state,
                records=records,
            )
            return None
        if isinstance(node, TransformNode) and node.transform_kind in CLOSED_TRANSFORM_KINDS:
            self._run_closed_transform(node, admission=admission, state=state)
            return None
        if isinstance(node, TransformNode):
            assert node.contract_in is not None and node.contract_out is not None
            declared_spec = _declared_transform_spec(node.transform_kind, node.spec)
            resolved = _resolve_node_template(
                declared_spec,
                node_id=node.node_id,
                transform_kind=node.transform_kind,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
            contract_in = self._pin(node.contract_in, label=f"transform {node.node_id!r} input")
            validated_input = _validate_node_contract(
                self.contract_validator,
                contract=contract_in,
                payload=resolved,
                direction="input",
                node_id=node.node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            contract = self._pin(node.contract_out, label=f"transform {node.node_id!r} output")
            value, lineage = _apply_node_transform(
                node.transform_kind,
                validated_input,
                max_items=_effective_max_items(admission),
                node_id=node.node_id,
                list_boundary=_kernel_list_boundary(self.contract_validator, contract),
            )
            state.outputs[node.as_] = _validate_node_contract(
                self.contract_validator,
                contract=contract,
                payload=value,
                direction="output",
                node_id=node.node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            state.provenance[node.as_] = _transform_provenance(
                node,
                state=state,
                lineage=lineage,
                base=_base_tokens(node, state, declared_spec, self.references),
            )
            return None
        if isinstance(node, ProjectNode | SelectNode):
            if isinstance(node, SelectNode):
                present = [alias for alias in node.sources if alias in state.outputs]
                if len(present) != 1:
                    raise ProcedureBoundaryRefused(
                        "compiler_invariant_broken",
                        "A branch join must select exactly one available producer.",
                        details={"node_id": node.node_id},
                    )
                fields: object = "$steps." + present[0]
            else:
                fields = node.template
            value = (
                normalize_canonical(node.literal)
                if isinstance(node, ProjectNode) and node.literal is not None
                else _resolve_node_template(
                    fields,
                    node_id=node.node_id,
                    transform_kind=None,
                    input_payload=state.input_payload,
                    outputs=state.outputs,
                )
            )
            contract = self._pin(node.contract_out, label=f"project {node.node_id!r} output")
            state.outputs[node.as_] = _validate_node_contract(
                self.contract_validator,
                contract=contract,
                payload=value,
                direction="output",
                node_id=node.node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            provenance_fields = fields
            state.provenance[node.as_] = _projected_provenance(
                provenance_fields,
                state=state,
                base=_base_tokens(node, state, provenance_fields, self.references),
                value=state.outputs[node.as_],
            )
            return None
        if isinstance(node, GuardNode):
            try:
                verdict, trace = _evaluate_predicate(
                    node.predicate,
                    input_payload=state.input_payload,
                    outputs=state.outputs,
                    parameters=state.parameters,
                )
            except ExecutionError as exc:
                raise _RunRefusal(
                    "runtime_reference_unresolved",
                    "A guard runtime reference did not resolve.",
                    node_id=node.node_id,
                ) from exc
            state.control = (
                state.control
                | _node_policy_tokens(node, self.references)
                | state.alias_tokens(frozenset(node.predicate.step_aliases()))
            )
            self._append_event(
                admission,
                records,
                "branch_evaluated",
                {
                    "node_id": node.node_id,
                    "verdict": verdict,
                    "selected_arm": "on_true" if verdict else "on_false",
                    "operands": trace,
                },
            )
            return "on_true" if verdict else "on_false"
        if isinstance(node, RepeatNode):
            self._run_repeat(
                node,
                admission=admission,
                state=state,
                records=records,
                started_ns=started_ns,
            )
            return None
        if isinstance(
            node,
            CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode,
        ):
            self._run_terminal(node, admission=admission, state=state, records=records)
            return None
        raise ExecutionError(f"unsupported Procedure node {type(node).__name__}")

    @staticmethod
    def _extend_alias(
        state: _RunState,
        alias: str,
        extra: frozenset[DependencyToken],
    ) -> None:
        current = state.provenance.get(alias)
        state.provenance[alias] = (
            AliasProvenanceV1(whole=extra) if current is None else current.merged(extra)
        )

    def _executed_workspace_request(
        self, node: CallNode, resolved_request: object
    ) -> ExecutedWorkspaceFileSourceRequestV2:
        """The authored workspace request as the daemon executes it, or a typed refusal."""

        if self.workspace_file_reader is None:
            raise _RunRefusal(
                "workspace_file_read_refused",
                "No daemon-local workspace reader is available.",
                node_id=node.node_id,
                detail_code="binding",
                details={
                    "path_class": "binding",
                    "repair_commands": ["attach a workspace or configure an allowed root"],
                },
            )
        try:
            authored = parse_authored_workspace_request(resolved_request)
        except WorkspaceBindingDigestAuthored as exc:
            raise _RunRefusal(
                "workspace_file_read_refused",
                str(exc),
                node_id=node.node_id,
                detail_code="binding",
                details={
                    "path_class": "binding",
                    "repair_commands": [
                        "remove workspace_binding_digest from the call input; the daemon "
                        "resolves the workspace"
                    ],
                },
            ) from exc
        except ValidationError as exc:
            raise _workspace_refusal(exc, node_id=node.node_id) from exc
        try:
            return self.workspace_file_reader.resolve(authored)
        except WorkspaceFileReadRefused as exc:
            raise _workspace_refusal(exc, node_id=node.node_id) from exc

    def _run_acquiring_call(
        self,
        node: CallNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        """Run a call with captures through its admitted Provider occurrence.

        One Provider invocation serves the call. Each ``capture<K>`` field of
        the slot's out Contract is its own capture output, ``<alias>.<field>``:
        committed byte-exact under its own pinned CaptureContract, decided by
        its own acquisition rule (availability, freshness, replayability,
        size), journaled with its own acquisition and produced-Capture records,
        associated with the occurrence under its field path, and charged to the
        run's Capture budget on its own. Every other field is normalized and
        checked by the out Contract. The call's value names each selected
        Capture by digest; an omitted field is absent from it, and its omission
        taints the control lineage.
        """

        if node.as_ in state.outputs:
            self._extend_alias(
                state, node.as_, _node_policy_tokens(node, self.references) | state.control
            )
            return
        if not admission.plans_providers:
            raise _RunRefusal(
                "provider_acquisition_plan_required",
                "An acquiring call requires the admitted acquisition carrier.",
                node_id=node.node_id,
            )
        fields = tuple(node.captures)
        rules: dict[str, AcquisitionRule] = {}
        for field in fields:
            rule = self._acquisition_rule(node, field)
            if rule is None:
                raise _RunRefusal(
                    "source_acquisition_unavailable",
                    f"No declared acquisition rule serves capture output {node.as_}.{field}.",
                    node_id=node.node_id,
                )
            rules[field] = rule
        occurrence = self._planned_external_occurrence(
            state=state,
            node_id=node.node_id,
            repeat_node_id=None,
            occurrence_kind="source",
        )
        pins = {
            field: self._pin(node.captures[field], label=f"call {node.node_id!r} captures")
            for field in fields
        }
        if (
            occurrence.input_name != node.as_
            or occurrence.capture_contracts
            != {field: pin.artifact_digest for field, pin in pins.items()}
            or not self._occurrence_matches_slot(occurrence, node.slot)
            or occurrence.effect_class == "external_mutation"
        ):
            raise _RunRefusal(
                "provider_acquisition_plan_mismatch",
                "The admitted acquiring occurrence differs from the graph binding.",
                node_id=node.node_id,
            )
        contracts: dict[str, CaptureContract] = {}
        for field, pin in pins.items():
            found = self.capture_contracts.get(pin.artifact_digest)
            if found is None or capture_contract_digest(found).tagged != pin.artifact_digest:
                raise _InternalFailure(
                    "provider_protocol_violation",
                    details={"node_id": node.node_id, "reason": "CaptureContract is unavailable"},
                )
            contracts[field] = found
        resolved_request = normalize_canonical(
            _resolve_template(
                node.input,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
        )
        for field, rule in rules.items():
            offending_origin = disallowed_origin(rule, resolved_request)
            if offending_origin is not None:
                # F05/§4.1,§4.4: a hard eligibility gate, never an omit choice,
                # exactly like permitted_replayability -- checked before any
                # provider is invoked.
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCode, ACQUISITION_ORIGIN_INELIGIBLE),
                    f"Capture output {node.as_}.{field} requests {offending_origin!r}, whose "
                    "origin is not in the Line's permitted acquisition.origins for this field.",
                    node_id=node.node_id,
                )
        workspace_request: ExecutedWorkspaceFileSourceRequestV2 | None = None
        if bound_operation(occurrence).builtin == "workspace.file":
            # The author names the workspace logically, or not at all; the daemon
            # resolves its root and executes -- and journals -- the request with
            # that root's binding digest, before anything is read.
            workspace_request = self._executed_workspace_request(node, resolved_request)
        derived_request = build_procedure_derived_source_request(
            run_id=admission.run_id,
            admission_binding_digest=admission.admission_binding_digest,
            occurrence_path=occurrence.occurrence_path,
            node_id=node.node_id,
            input_name=node.as_,
            request=(
                resolved_request
                if workspace_request is None
                else workspace_request.model_dump(mode="json")
            ),
        )
        self._append_event(
            admission,
            records,
            "source_request_derived",
            derived_request.model_dump(mode="json"),
        )
        source_read_receipt: SourceReadReceipt | None = None
        provider_payload = resolved_request
        if workspace_request is not None:
            assert self.workspace_file_reader is not None  # resolved by it above
            # The core-served read has exactly one capture field (an acceptance law).
            (workspace_contract,) = contracts.values()
            try:
                workspace_read = self.workspace_file_reader.read(
                    workspace_request,
                    run_id=admission.run_id,
                    admission_binding_digest=admission.admission_binding_digest,
                    occurrence_path=occurrence.occurrence_path,
                    policy_coordinate=admission.accepted_coordinate,
                    resolved_max_bytes=workspace_contract.selection_budget.max_bytes,
                    derived_request_digest=derived_request.request_digest,
                    read_at=self.clock.now(),
                )
            except (ValidationError, WorkspaceFileReadRefused) as exc:
                raise _workspace_refusal(exc, node_id=node.node_id) from exc
            provider_payload = workspace_read.provider_input
            source_read_receipt = workspace_read.receipt
            self._append_event(
                admission,
                records,
                "source_read",
                {
                    "receipt": source_read_receipt.model_dump(mode="json"),
                    "receipt_digest": source_read_receipt_digest(source_read_receipt),
                },
            )
        try:
            output, invocation_receipt, invocation_receipt_digest = self._invoke_provider_v4(
                node_id=node.node_id,
                slot=node.slot,
                repeat_node_id=None,
                effect_policy=None,
                payload=provider_payload,
                contract_out=None,
                admission=admission,
                state=state,
                records=records,
                occurrence=occurrence,
            )
        except _ProviderNodeRefusal as exc:
            # Only a typed refusal the operation declares absorbable is absorbed,
            # and only for a capture output whose rule omits an unavailable input.
            absorbable = provider_refusal_is_absorbable(occurrence.operation, exc.outcome)
            if not absorbable or any(rule.on_unavailable != "omit" for rule in rules.values()):
                raise
            unavailable: dict[str, ProcedureSourceAcquisitionResultV1] = {}
            unavailable_decisions: dict[str, AcquisitionInputDecision] = {}
            for field, rule in rules.items():
                unavailable[field] = ProcedureSourceAcquisitionResultV1(
                    node_id=node.node_id,
                    input_name=rule.input_name,
                    outcome="unavailable",
                    reason_code="cruxible.acquisition.unavailable",
                    detail=exc.refusal.message,
                )
                unavailable_decisions[field] = apply_acquisition_result(
                    rule,
                    unavailable[field],
                    default_authorized=rule.input_name in self.default_authorizations,
                    evaluation_time=admission.occurrence_evaluation_time,
                )
                self._journal_acquisition(
                    admission,
                    records,
                    node=node,
                    field=field,
                    result=unavailable[field],
                    decision=unavailable_decisions[field],
                    provider_refusal=exc.outcome.model_dump(mode="json"),
                )
            for field, rule in rules.items():
                self._bind_omission(
                    admission=admission,
                    state=state,
                    rule=rule,
                    decision=unavailable_decisions[field],
                )
            return
        for field, rule in rules.items():
            offending_result_origin = disallowed_origin(rule, output)
            if offending_result_origin is not None:
                # F05/§4.1,§4.4: the result's own url/final_url fields (e.g. after
                # a redirect) are checked too, before any Capture conversion.
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCode, ACQUISITION_ORIGIN_INELIGIBLE),
                    f"Capture output {node.as_}.{field} result names "
                    f"{offending_result_origin!r}, whose origin is not in the Line's permitted "
                    "acquisition.origins for this field.",
                    node_id=node.node_id,
                )
        out_contract = closure_lookup(occurrence.contracts)(occurrence.operation.out)
        # An operation whose out Contract carries no capture<K> field returns
        # its whole output as Capture wire; the call's value then names only
        # that Capture.
        whole_wire = workspace_request is None and (
            out_contract is None or not capture_fields(out_contract)
        )
        if whole_wire:
            output = self._validate_operation_value(
                occurrence,
                normalize_canonical(output),
                direction="out",
                admission=admission,
                state=state,
                node_id=node.node_id,
            )
        wires: dict[str, ProviderResultToExternalCapture] = {}
        for field in fields:
            try:
                if whole_wire:
                    wires[field] = ProviderResultToExternalCapture.model_validate(output)
                elif workspace_request is None or source_read_receipt is None:
                    if not isinstance(output, dict):
                        raise ValueError("an acquiring call's output is a record")
                    wires[field] = ProviderResultToExternalCapture.model_validate(output.get(field))
                else:
                    # The core-served read commits the file's exact bytes, the
                    # ones core read and handed the operation; its output
                    # structures them.
                    assert isinstance(provider_payload, dict)
                    wires[field] = ProviderResultToExternalCapture(
                        source_identity=workspace_request.logical_source,
                        coordinate_type=workspace_request.coordinate_type,
                        coordinate=workspace_request.coordinate,
                        selector_type=workspace_request.selector_type,
                        selector=workspace_request.selector,
                        replayability=workspace_request.replayability,
                        content_base64=str(provider_payload["bytes"]),
                        byte_length=int(cast(int, provider_payload["byte_length"])),
                        bytes_digest=str(provider_payload["bytes_digest"]),
                        observed_at=source_read_receipt.read_at,
                    )
            except (ValidationError, ValueError) as exc:
                raise _InternalFailure(
                    "provider_protocol_violation",
                    details={
                        "node_id": node.node_id,
                        "reason": f"capture field {field!r} is not Capture wire",
                    },
                ) from exc
        producer = self.slot_bindings[node.slot].build.target
        bound_generation = admission.accepted_coordinate.generation_root
        for field in fields:
            try:
                # F05b: every check independent of the result's byte length runs
                # before classifying it as oversized, so a schema, replayability
                # or receipt-binding violation is never hidden behind
                # on_oversized just because the same result is also over budget.
                validate_provider_external_capture_result(
                    contract=contracts[field],
                    result=wires[field],
                    receipt=invocation_receipt,
                    occurrence=occurrence,
                    producer=producer,
                    bound_generation=bound_generation,
                    field=field,
                    source_read_receipt=source_read_receipt,
                )
            except (CaptureFormatError, ValidationError, ValueError) as exc:
                raise _InternalFailure(
                    "provider_protocol_violation",
                    details={"node_id": node.node_id, "reason": "Capture conversion failed"},
                ) from exc
        reserved_store = ReservedCaptureStore(
            bodies=self.bodies,
            reservations=self.material_reservations,
            admission=admission,
        )
        results: dict[str, ProcedureSourceAcquisitionResultV1] = {}
        built: dict[str, Any] = {}
        bodies: dict[str, bytes] = {}
        for field in fields:
            wire, contract = wires[field], contracts[field]
            if wire.byte_length > contract.selection_budget.max_bytes:
                # F05: a real oversized read is the typed acquisition outcome
                # (on_oversized) of this capture output alone, classified before
                # its Capture is committed.
                results[field] = ProcedureSourceAcquisitionResultV1(
                    node_id=node.node_id,
                    input_name=rules[field].input_name,
                    outcome="oversized",
                    reason_code=ACQUISITION_OVERSIZED,
                    detail=(
                        f"Read {wire.byte_length} bytes against a "
                        f"{contract.selection_budget.max_bytes}-byte Capture budget."
                    ),
                )
                continue
            try:
                built[field] = build_provider_external_capture_v2(
                    store=reserved_store,
                    contract=contract,
                    result=wire,
                    receipt=invocation_receipt,
                    occurrence=occurrence,
                    producer=producer,
                    bound_generation=bound_generation,
                    field=field,
                    source_read_receipt=source_read_receipt,
                    provider_output=None if whole_wire else output,
                )
                bodies[field] = base64.b64decode(wire.content_base64, validate=True)
            except (CaptureFormatError, CasError, ValidationError, ValueError) as exc:
                raise _InternalFailure(
                    "provider_protocol_violation",
                    details={"node_id": node.node_id, "reason": "Capture conversion failed"},
                ) from exc
        digests = {field: item.capture_digest for field, item in built.items()}
        if whole_wire:
            material: CanonicalValue = normalize_canonical(digests)
        else:
            assert isinstance(output, dict)
            # The call's value: its out record, each committed capture field
            # naming its Capture. An oversized field is checked as absent.
            record = (
                {**output, **digests}
                if workspace_request is None
                else {
                    **self._validate_operation_value(
                        occurrence,
                        normalize_canonical(output),
                        direction="out",
                        admission=admission,
                        state=state,
                        node_id=node.node_id,
                    ),  # type: ignore[dict-item]
                    **digests,
                }
            )
            for field in fields:
                if field not in digests and workspace_request is None:
                    record.pop(field, None)
            material = (
                normalize_canonical(record)
                if workspace_request is not None
                else self._validate_operation_value(
                    occurrence,
                    normalize_canonical(record),
                    direction="out",
                    admission=admission,
                    state=state,
                    node_id=node.node_id,
                    absent_captures=frozenset(field for field in fields if field not in digests),
                )
            )
        for field, item in built.items():
            envelope = item.envelope
            source = envelope.source
            assert isinstance(source, ExternalSourceReference)
            results[field] = ProcedureSourceAcquisitionResultV1(
                node_id=node.node_id,
                input_name=rules[field].input_name,
                outcome="acquired",
                acquisition=ExternalCaptureAcquisitionV1(
                    receipt=CaptureAcquisitionReceiptV1(
                        capture_contract_digest=envelope.capture_contract_digest,
                        producer=envelope.producer,
                        producer_binding_digest=envelope.producer_binding_digest,
                        source_identity=source.source_identity,
                        coordinate_type=source.coordinate_type,
                        coordinate=source.coordinate,
                        selector_type=source.selector_type,
                        selector=source.selector,
                        commitment=envelope.commitment,
                        observed_at=envelope.observed_at,
                        replayability=source.replayability,
                        source_effective_time=envelope.source_effective_time,
                    ),
                    envelope=envelope,
                    capture_digest=item.capture_digest,
                    epistemic_grade=contracts[field].epistemic_grade,
                    provenance_grade="daemon-fetched",
                    canonical_material=material,
                ),
            )
        # Each capture output is decided by its own rule, on its own Capture:
        # one field can be stale or ineligible while another is selected.
        decisions = {
            field: apply_acquisition_result(
                rules[field],
                results[field],
                default_authorized=rules[field].input_name in self.default_authorizations,
                evaluation_time=admission.occurrence_evaluation_time,
            )
            for field in fields
        }
        for field in fields:
            self._journal_acquisition(
                admission,
                records,
                node=node,
                field=field,
                result=results[field],
                decision=decisions[field],
            )
        for field in fields:
            if decisions[field].disposition == "refused":
                # The attempted reads remain journaled, but an ineligible
                # Capture is never published as an output of this run.
                reserved_store.release()
                self._refuse_acquisition(
                    node,
                    admission=admission,
                    rule=rules[field],
                    result=results[field],
                    decision=decisions[field],
                )
        selected = tuple(field for field in fields if decisions[field].disposition == "selected")
        for field in fields:
            if decisions[field].disposition == "omitted":
                self._bind_omission(
                    admission=admission, state=state, rule=rules[field], decision=decisions[field]
                )
        base = _node_policy_tokens(node, self.references) | state.control
        if admission.acquisition_policy_digest is not None:
            base = base | {policy_token(admission.acquisition_policy_digest)}
        tokens: set[DependencyToken] = set()
        for field in selected:
            acquisition = results[field].acquisition
            assert acquisition is not None  # a selected result carries its Capture
            tokens |= self._bind_capture_output(
                node,
                admission=admission,
                state=state,
                records=records,
                field=field,
                rule=rules[field],
                acquisition=acquisition,
                body=bodies[field],
                contract_digest=pins[field].artifact_digest,
                occurrence_path=occurrence.occurrence_path,
                invocation_receipt_digest=invocation_receipt_digest,
            )
        if selected:
            assert isinstance(material, dict)
            omitted = set(fields) - set(selected)
            state.outputs[node.as_] = normalize_canonical(
                {name: value for name, value in material.items() if name not in omitted}
            )
            state.provenance[node.as_] = AliasProvenanceV1(whole=frozenset(base | tokens))
        reserved_store.release()

    def _capture_body(self, state: _RunState, capture_digest: str) -> bytes:
        """The exact bytes of a Capture this run committed or was admitted."""

        held = state.capture_bodies.get(capture_digest)
        if held is not None:
            return held
        access = BodyAccessContext(principal_id="procedure-extract", can_read_body=True)
        envelope = parse_capture_envelope(self.bodies.read(capture_digest, access=access))
        body = self.bodies.read(envelope.commitment.digest, access=access)
        state.capture_bodies[capture_digest] = body
        state.capture_contracts[capture_digest] = envelope.capture_contract_digest
        return body

    def _run_extract(self, node: ExtractNode, *, state: _RunState) -> None:
        """Read one Capture's records, typed, by its CaptureContract's decoding."""

        call = state.outputs.get(node.capture_alias)
        capture_digest = call.get(node.capture_field) if isinstance(call, dict) else None
        if not isinstance(capture_digest, str):
            raise _RunRefusal(
                "runtime_reference_unresolved",
                f"extract {node.node_id!r}: {node.capture!r} holds no Capture this run made "
                "or was admitted (an omitted or defaulted acquisition binds none)",
                node_id=node.node_id,
            )
        try:
            content = self._capture_body(state, capture_digest)
        except Exception as exc:
            raise _RunRefusal(
                "source_material_unavailable",
                f"extract {node.node_id!r}: the Capture's bytes are unavailable",
                node_id=node.node_id,
            ) from exc
        contract = self.capture_contracts.get(state.capture_contracts.get(capture_digest, ""))
        row = None if self.contract_resolver is None else self.contract_resolver(node.row)
        if contract is None or row is None:
            raise _InternalFailure(
                "provider_protocol_violation",
                details={
                    "node_id": node.node_id,
                    "reason": "the extract's CaptureContract or row Contract is unavailable",
                },
            )
        only_keys = None
        if node.only_keys is not None:
            resolved = _resolve_node_template(
                node.only_keys,
                node_id=node.node_id,
                transform_kind=None,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
            if not isinstance(resolved, list) or not all(
                isinstance(item, dict) for item in resolved
            ):
                raise _RunRefusal(
                    "runtime_reference_unresolved",
                    f"extract {node.node_id!r}: only_keys resolves to a list of key records",
                    node_id=node.node_id,
                )
            only_keys = cast(list[dict[str, object]], resolved)
        try:
            outcome = run_extract(
                node,
                capture_digest=capture_digest,
                content=content,
                decoding=getattr(contract, "decoding", None),
                row=row,
                resolve=self.contract_resolver,
                enums=self.enum_resolver,
                only_keys=only_keys,
            )
        except ExtractRefused as exc:
            raise _RunRefusal(
                cast(ProcedureNodeRefusalCode, exc.code),
                str(exc),
                node_id=node.node_id,
                details=exc.details or None,
            ) from exc
        value = normalize_canonical(outcome.value())
        state.observe_items(len(outcome.held), f"extract:{node.node_id}", "results")
        state.outputs[node.as_] = value
        state.completeness.mark(
            node.as_,
            outcome.complete and state.completeness.complete((node.capture_alias,)),
        )
        state.completeness.drop(node.as_, outcome.dropped_rows())
        state.provenance[node.as_] = AliasProvenanceV1(
            whole=_node_policy_tokens(node, self.references)
            | state.control
            | state.alias_tokens(frozenset({node.capture_alias}))
        )

    def _run_closed_transform(
        self,
        node: TransformNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
    ) -> None:
        """One closed transform over records: typed, three-valued, completeness carried."""

        def resolve(reference: object) -> object:
            return _resolve_node_template(
                reference,
                node_id=node.node_id,
                transform_kind=None,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )

        try:
            value, complete = apply_closed_transform(
                node.transform_kind,
                node.spec,
                resolve,
                parameters=state.parameters if isinstance(state.parameters, dict) else None,
                enums=self.enum_resolver,
                max_items=_effective_max_items(admission),
            )
        except TransformItemBudgetExceeded as exc:
            raise _budget_refusal(
                budget_kind="max_items",
                limit=exc.limit,
                observed=exc.observed,
                node_id=node.node_id,
                boundary=f"transform:{node.node_id}",
                field_path="records",
            ) from exc
        except TransformInputInvalid as exc:
            raise _RunRefusal(
                "contract_input_refused",
                f"The {node.transform_kind} transform input is invalid: {exc}",
                node_id=node.node_id,
            ) from exc
        aliases = _referenced_aliases(node.spec)
        state.outputs[node.as_] = value
        state.completeness.mark(
            node.as_,
            complete
            and state.completeness.reads_complete(_referenced_reads(node.spec), records_kept=True),
        )
        state.provenance[node.as_] = AliasProvenanceV1(
            whole=_node_policy_tokens(node, self.references)
            | state.control
            | state.alias_tokens(aliases)
        )

    def _track_completeness(self, node: object, state: _RunState) -> None:
        """Carry the completeness law past one node.

        ``extract``, the closed transforms, ``invoke`` and ``repeat`` set their
        own outputs' completeness as they run; every other node's output is the
        AND of what it reads, and a ``call`` is also incomplete when its bound
        operation declares ``may_truncate``.
        """

        tracker = state.completeness
        if isinstance(node, GuardNode):
            tracker.guard(_predicate_reads(node.predicate))
            return
        if isinstance(node, ExtractNode | InvokeNode | RepeatNode) or (
            isinstance(node, TransformNode) and node.transform_kind in CLOSED_TRANSFORM_KINDS
        ):
            return
        if isinstance(node, SelectNode):
            reads: tuple[Read, ...] = tuple(
                (alias, ()) for alias in node.sources if alias in state.outputs
            )
        else:
            reads = _node_reads(node)
        if is_terminal_node(node):
            if self.completeness_probe is not None:
                self.completeness_probe(node.node_id, tracker.at_sink(reads))  # type: ignore[attr-defined]
            return
        alias = getattr(node, "as_", None)
        if isinstance(alias, str) and alias in state.outputs:
            if isinstance(node, ReadNode):
                # A read completed within its ceiling, or the node refused.
                tracker.mark(alias, True)
            elif isinstance(node, CallNode):
                tracker.mark(
                    alias, tracker.reads_complete(reads) and not self._truncates(node.slot)
                )
            else:
                tracker.mark(alias, tracker.reads_complete(reads))

    def _truncates(self, slot: str | None) -> bool:
        """Whether the operation bound to ``slot`` declares that it may truncate."""

        binding = self.slot_bindings.get(slot or "")
        return binding is not None and bool(getattr(binding.operation, "may_truncate", False))

    def _occurrence_matches_slot(
        self, occurrence: ProviderExternalOccurrencePlan, slot: str | None
    ) -> bool:
        """Whether an admitted occurrence is its node's slot binding, exactly."""

        binding = self.slot_bindings.get(slot or "")
        return (
            slot is not None
            and binding is not None
            and occurrence.slot == slot
            and occurrence.provider_artifact_digest == binding.build_digest
            and occurrence.operation == binding.operation
            and occurrence.operation_signature_digest == binding.operation_signature_digest
        )

    def _acquisition_rule(self, node: CallNode, field: str) -> AcquisitionRule | None:
        if self.acquisition_rules is None:
            return None
        # Each capture output's rule is named <alias>.<field>.
        return self.acquisition_rules.rule_for(node.node_id, f"{node.as_}.{field}")

    def _journal_acquisition(
        self,
        admission: ProcedureRunAdmission,
        records: list[StoredProcedureJournalRecordV1],
        *,
        node: CallNode,
        field: str,
        result: ProcedureSourceAcquisitionResultV1,
        decision: AcquisitionInputDecision,
        provider_refusal: object | None = None,
    ) -> None:
        """Journal one capture output's acquisition attempt and its decision."""

        self._append_event(
            admission,
            records,
            "source_acquisition",
            {
                "tag": "playbill-procedure-source-acquisition-v1",
                "node_id": node.node_id,
                "input_name": result.input_name,
                "field": field,
                "result": result.model_dump(mode="json", exclude={"acquisition"}),
                "decision": decision.model_dump(mode="json"),
                **({} if provider_refusal is None else {"provider_refusal": provider_refusal}),
                "audit": {
                    "deployment_digest": admission.deployment_snapshot_digest,
                    "recorded_at": format_datetime(self.clock.now()),
                },
            },
        )

    def _refuse_acquisition(
        self,
        node: CallNode,
        *,
        admission: ProcedureRunAdmission,
        rule: AcquisitionRule,
        result: ProcedureSourceAcquisitionResultV1,
        decision: AcquisitionInputDecision,
    ) -> NoReturn:
        """Refuse the run for one capture output its rule refuses."""

        details: dict[str, object] = {"input_name": rule.input_name}
        message = f"Capture output {rule.input_name!r} refuses this result. "
        if (
            result.acquisition is not None
            and rule.max_age is not None
            and "cruxible.acquisition.stale" in decision.reason_codes
        ):
            evaluation_time = admission.occurrence_evaluation_time
            delta = evaluation_time - result.acquisition.envelope.observed_at
            details.update(
                age_microseconds=(delta.days * 86_400 + delta.seconds) * 1_000_000
                + delta.microseconds,
                max_age_microseconds=rule.max_age.microseconds,
            )
            message += (
                f"Capture age is {delta.total_seconds()}s; max_age is "
                f"{rule.max_age.microseconds / 1_000_000}s. "
            )
        message += (
            f"Repair: fetch a fresh available Capture for {rule.input_name!r} "
            "within its contract and age bounds."
        )
        raise _RunRefusal(
            cast(
                ProcedureNodeRefusalCode,
                decision.reason_codes[0]
                if decision.reason_codes
                else "cruxible.acquisition.refused",
            ),
            message,
            node_id=node.node_id,
            details=details,
        )

    def _bind_omission(
        self,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        rule: AcquisitionRule,
        decision: AcquisitionInputDecision,
    ) -> None:
        """An omitted capture output binds no Capture and taints the control lineage."""

        marker = _decision_digest(admission.run_id, decision)
        state.facts[marker] = DependencyEvidenceFactsV1(
            epistemic_grade="predicted",
            provenance_grade="self-asserted",
            taint_labels=(TAINT_OMITTED_OPTIONAL,),
            acquisition_input_name=rule.input_name,
        )
        state.outcomes[rule.input_name] = AcquisitionInputOutcomeV1(
            input_name=rule.input_name,
            disposition="omitted",
        )
        state.control = state.control | {receipt_token(marker)}

    def _bind_capture_output(
        self,
        node: CallNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        field: str,
        rule: AcquisitionRule,
        acquisition: ExternalCaptureAcquisitionV1,
        body: bytes,
        contract_digest: str,
        occurrence_path: str,
        invocation_receipt_digest: str,
    ) -> frozenset[DependencyToken]:
        """Bind one selected capture output: its Capture, budget, evidence and association.

        Returns the tokens the call's value carries for it.
        """

        digest = acquisition.capture_digest
        state.capture_bytes += len(body)
        state.capture_bodies[digest] = body
        state.capture_contracts[digest] = contract_digest
        state.facts[digest] = DependencyEvidenceFactsV1(
            epistemic_grade=acquisition.epistemic_grade,
            provenance_grade=acquisition.provenance_grade,
            acquisition_input_name=rule.input_name,
        )
        state.outcomes[rule.input_name] = AcquisitionInputOutcomeV1(
            input_name=rule.input_name,
            disposition="acquired",
            capture_digests=(digest,),
        )
        self._append_event(
            admission,
            records,
            "produced_capture",
            {
                "tag": "playbill-procedure-produced-capture-v1",
                "node_id": node.node_id,
                "input_name": rule.input_name,
                "field": field,
                "capture_digest": digest,
                "capture_contract_digest": acquisition.envelope.capture_contract_digest,
                "acquisition_receipt_digest": acquisition.receipt.digest,
                **_source_capture_association_fields(occurrence_path, invocation_receipt_digest),
                "observed_at": format_datetime(acquisition.envelope.observed_at),
                "epistemic_grade": acquisition.epistemic_grade,
                "provenance_grade": acquisition.provenance_grade,
                "audit": {
                    "deployment_digest": admission.deployment_snapshot_digest,
                    "recorded_at": format_datetime(self.clock.now()),
                },
            },
        )
        state.source_capture_associations.append(
            ProcedureSourceCaptureAssociation(
                occurrence_path=occurrence_path,
                field=field,
                invocation_receipt_digest=invocation_receipt_digest,
                capture_digest=digest,
            )
        )
        return frozenset(
            {
                produced_capture_token(digest),
                receipt_token(acquisition.receipt.digest),
                policy_token(acquisition.envelope.capture_contract_digest),
            }
        )

    def _run_child(
        self,
        node: InvokeNode,
        *,
        accepted: AcceptedProcedure,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> None:
        from cruxible_core.procedures.nested import ParentInvocationContext

        if self.nested_runner is None:
            raise ExecutionError("nested Procedure invocation requires an admitted runner")
        remaining_us = (
            admission.budget.wall_clock.microseconds
            - (self.clock.monotonic_ns() - started_ns) // 1000
        )
        if remaining_us <= 0:
            raise _BudgetExceeded(
                "wall_clock",
                limit=admission.budget.wall_clock.microseconds,
                observed=admission.budget.wall_clock.microseconds + 1,
                node_id=node.node_id,
            )
        remaining = admission.budget.model_copy(
            update={
                "wall_clock": admission.budget.wall_clock.model_copy(
                    update={"microseconds": remaining_us}
                ),
                "max_provider_calls": max(
                    0, admission.budget.max_provider_calls - state.provider_calls
                ),
                "max_capture_bytes": max(
                    0, admission.budget.max_capture_bytes - state.capture_bytes
                ),
            }
        )
        context = ParentInvocationContext(
            admission,
            accepted,
            node,
            remaining,
            self.effective_rung,
            deadline_ns=started_ns + admission.budget.wall_clock.microseconds * 1000,
        )
        value = _resolve_node_template(
            node.input,
            node_id=node.node_id,
            transform_kind=None,
            input_payload=state.input_payload,
            outputs=state.outputs,
        )
        self._append_event(
            admission,
            records,
            "child_invocation",
            {
                "node_id": node.node_id,
                "verdict": "started",
                "procedure": self._pin(node.procedure, label=f"invoke {node.node_id!r}").model_dump(
                    mode="json"
                ),
                "input_digest": run_value_digest("nested-input", value),
                "remaining_budget": remaining.model_dump(mode="json"),
            },
        )
        result = self.nested_runner.run(context, value)
        child_records = [
            row
            for row in self.journal.all_records(result.receipt.stream, result.receipt.partition_id)
            if row.record.run_id == result.run_id
        ]
        if (
            not child_records
            or tuple(row.record_digest for row in child_records) != result.receipt.record_digests
        ):
            raise ExecutionError("nested receipt does not reproduce retained child records")
        access = BodyAccessContext(principal_id="nested-procedure", can_read_body=True)
        admission_records = [
            row for row in child_records if row.record.event_kind == "admission_bound"
        ]
        if len(admission_records) != 1:
            raise ExecutionError("nested receipt requires exactly one child admission")
        payload = read_payload(
            self.bodies, admission_records[0].record.payload_digest, access=access
        )
        if isinstance(payload, UnavailablePayload):
            raise _OperationalFailure(
                "replay_material_unavailable", details={"payload_digest": payload.digest}
            )
        admitted = parse_admission_payload(payload).admission
        if admitted.parent_binding != context.binding:
            raise ExecutionError("nested receipt is not bound to this parent occurrence")
        if (
            admitted.procedure_identity != node.procedure.target
            or admitted.procedure_artifact_digest
            != self._pin(node.procedure, label=f"invoke {node.node_id!r}").artifact_digest
            or admitted.run_id != result.run_id
            or admitted.admission_binding_digest != result.receipt.admission_binding_digest
            or admitted.journal_stream != result.receipt.stream
            or admitted.journal_partition_id != result.receipt.partition_id
        ):
            raise ExecutionError("nested receipt names another Procedure")
        final = read_payload(self.bodies, child_records[-1].record.payload_digest, access=access)
        if isinstance(final, UnavailablePayload):
            raise _OperationalFailure(
                "replay_material_unavailable", details={"payload_digest": final.digest}
            )
        if child_records[-1].record.event_kind != "attempt_finalized" or not isinstance(
            final, dict
        ):
            raise ExecutionError("nested execution has no durable completion")
        if final.get("status") != result.status or final.get("output") != result.output:
            raise ExecutionError("nested completion differs from returned result")
        # The child's completion records an incomplete return (data or control
        # lineage); the invoke's output is that AND the inputs it passed.
        child_complete = final.get("return_complete", True) is not False
        budget = ProcedureRunBudget.model_validate(final["budget"]).observed
        state.provider_calls += budget.provider_calls
        state.capture_bytes += budget.capture_bytes
        state.observe_items(
            budget.max_items.high_water, "child:" + node.node_id, budget.max_items.field_path or ""
        )
        state.observe_result_bytes(
            budget.result_bytes.high_water,
            "child:" + node.node_id,
            budget.result_bytes.field_path or "",
        )
        digest = procedure_run_receipt_digest(result.receipt)
        returned_manifest = TerminalItemDependencyManifestV1.model_validate(
            final["return_manifest"]
        )
        if returned_manifest.run_id != result.run_id:
            raise ExecutionError("nested return lineage names another run")
        returned_tokens = manifest_dependency_tokens(returned_manifest)
        retained_facts = final.get("dependency_facts", {})
        if not isinstance(retained_facts, dict):
            raise ExecutionError("nested return facts must be an object")
        for key, facts in retained_facts.items():
            state.facts[key] = DependencyEvidenceFactsV1.model_validate(facts)
        tokens = (
            _base_tokens(node, state, node.input, self.references)
            | returned_tokens
            | frozenset({receipt_token(digest)})
        )
        state.provenance[node.as_] = AliasProvenanceV1(whole=tokens)
        outcome: dict[str, object] = {
            "succeeded": result.status == "succeeded",
            "status": result.status,
            "receipt": {
                "run_id": result.run_id,
                "procedure_identity": admitted.procedure_identity.qualified,
                "procedure_artifact_digest": admitted.procedure_artifact_digest,
                "receipt_digest": digest,
            },
        }
        if result.status == "succeeded":
            outcome["value"] = result.output
            if final.get("terminal_capture") is not None:
                capture = final["terminal_capture"]
                if not isinstance(capture, str):
                    raise ExecutionError("nested capture reference must name an exact capture")
                if produced_capture_token(capture) not in returned_tokens:
                    raise ExecutionError("nested capture has no retained dependency binding")
                outcome["terminal"] = {"capture": {"capture_digest": capture}}
        state.outputs[node.as_] = normalize_canonical(outcome)
        state.completeness.mark(
            node.as_,
            child_complete and state.completeness.reads_complete(_referenced_reads(node.input)),
        )
        self._append_event(
            admission,
            records,
            "child_invocation",
            {
                "node_id": node.node_id,
                "verdict": "completed",
                "status": result.status,
                "procedure": admitted.procedure_identity.model_dump(mode="json"),
                "receipt": result.receipt.model_dump(mode="json"),
            },
        )

    def _run_terminal(
        self,
        node: CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        """Bind this terminal's per-item closure, then cap it and deliver it.

        The closure is bound before the cap is applied on purpose: a run that a
        term refused still owes an auditable account of exactly what it would
        have emitted and which term stopped it.

        An effectful terminal is journaled twice around its sink: a `prepared`
        record carrying the exact request before the sink is called, and a
        resolving record (`delivered`, `refused`, or `failed`) after. The pair
        is what lets a crash between the two be recovered against the same
        operation instead of re-run as a second one.
        """

        inbox_cap = _inbox_cap(node) if isinstance(node, InboxEgressNode) else None
        children, values, manifests, selected = self._record_terminal_items(
            node,
            admission=admission,
            state=state,
            records=records,
            limit=None if inbox_cap is None else max(inbox_cap[0] - state.inbox_items, 0),
        )
        truncation = (
            None
            if inbox_cap is None or selected <= len(values)
            else InboxPostTruncation(
                cap=inbox_cap[0],
                cap_source=inbox_cap[1],
                selected=selected,
                posted=len(values),
            ).model_dump(mode="json")
        )
        if not values and truncation is None:
            # A fan-out over an empty list has nothing to emit: no egress is
            # prepared or delivered (a proposal with no member is no proposal),
            # and the run ends at this terminal with its typed result.
            return
        required = TERMINAL_REQUIRED_RUNGS[node.kind]
        payload: dict[str, object] = {
            "node_id": node.node_id,
            "kind": node.kind,
            "required_rung": required,
            "children": [item.model_dump(mode="json") for item in children],
        }
        if truncation is not None:
            # Every record of this terminal carries what its cap left unposted.
            payload["inbox_truncation"] = truncation
        rung = self.effective_rung
        if rung is None or self.egress_sink is None:
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {**payload, "verdict": "dependencies_bound_egress_pending"},
            )
            raise _RunRefusal(
                "terminal_not_available",
                "Governed terminal egress requires a bound effective rung and an egress sink; "
                "this run bound its per-item dependency closure only.",
                node_id=node.node_id,
            )
        payload = {
            **payload,
            "effective_rung": rung.effective_rung,
            "effective_rung_digest": effective_rung_digest(rung),
            "limiting_term": rung.limiting_term,
            "terms": [item.model_dump(mode="json") for item in rung.terms],
        }
        # A settle terminal capped at propose proposes instead, as its fallback
        # for that cap; one capped below propose is refused like any other.
        capped_by: EffectiveRungTermV1 | None = None
        if not rung.permits(node.kind):
            if isinstance(node, SettleChangeSetNode) and rung.permits("propose_change_set"):
                capped_by = rung.limiting_term
            else:
                self._append_event(
                    admission,
                    records,
                    "terminal_egress",
                    {**payload, "verdict": "refused_effective_rung"},
                )
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCode, rung.refusal_code),
                    f"Terminal {node.kind!r} requires {authority_for_rung(required)}; the "
                    f"{SERVED_AUTHORITY_TERMS[rung.limiting_term]} term capped this run at "
                    f"{authority_for_rung(rung.effective_rung)}. "
                    f"{rung.term(rung.limiting_term).reason}",
                    node_id=node.node_id,
                )
        elif (
            isinstance(node, SettleChangeSetNode)
            and isinstance(self.egress_sink, SettleGrantHolderProtocol)
            and not self.egress_sink.grants_settle()
        ):
            # A caller's tier lifted the mandate term to settle, but no live
            # mandate grants settling: the grant caps it at propose.
            capped_by = "mandate_grant"
        if not values:
            # The run's inbox cap was spent by an earlier post_inbox terminal:
            # nothing is posted, and the record says what the cap left out.
            self._append_event(
                admission, records, "terminal_egress", {**payload, "verdict": "delivered"}
            )
            return
        try:
            request, prepared = self._terminal_egress_request(
                node,
                admission=admission,
                rung=rung,
                children=children,
                values=values,
                manifests=manifests,
                capped_by=capped_by,
            )
        except TerminalEgressError as exc:
            refusal = self._terminal_refusal(exc, node_id=node.node_id)
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {**payload, "verdict": "refused", "refusal_code": refusal.refusal.code},
            )
            raise refusal from exc
        # Preparation (lowering every item) is where a large terminal spends its
        # time. The run's deadline is enforced here, before the prepared record
        # and before delivery, so a run out of time delivers nothing: its
        # report and the proposal door never disagree.
        elapsed_us = (self.clock.monotonic_ns() - state.run_started_monotonic_ns) // 1000
        state.wall_clock_microseconds = max(state.wall_clock_microseconds, elapsed_us)
        if elapsed_us > admission.budget.wall_clock.microseconds:
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {**payload, "verdict": "refused", "refusal_code": "wall_clock_exhausted"},
            )
            raise _BudgetExceeded(
                "wall_clock",
                limit=admission.budget.wall_clock.microseconds,
                observed=elapsed_us,
                node_id=node.node_id,
            )
        # A proposal or settle terminal is journaled around its sink: a durable
        # `prepared` record before the sink is called, a resolving record after,
        # so recovery can finish it. That holds for one whose every item is
        # unchanged, or that keeps its Line's open proposal, too: its delivery
        # records re-observations, a durable effect.
        effectful = (
            isinstance(request, TerminalEgressRequestV2)
            and request.kind in {"propose_change_set", "settle_change_set"}
        ) or (prepared is not None and prepared.proposes_nothing)
        if isinstance(request, TerminalEgressRequestV2) and effectful:
            payload = {
                **payload,
                "operation_key": request.operation_key,
                "target_paths": list(request.target_paths),
                "procedure_mandate_digest": request.procedure_mandate_digest,
            }
        if effectful:
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {
                    **payload,
                    "verdict": "prepared",
                    "request": request.model_dump(mode="json"),
                    "prepared": None if prepared is None else prepared.model_dump(mode="json"),
                    "evidence": {
                        item_key: citable[0]
                        for item_key, manifest in manifests.items()
                        if len(citable := citable_captures(manifest)) == 1
                    },
                },
            )
        if request.kind == "emit_capture":
            emitted_bytes = sum(len(canonical_bytes(item.value)) for item in request.items)
            if state.capture_bytes + emitted_bytes > admission.budget.max_capture_bytes:
                raise _BudgetExceeded(
                    "max_capture_bytes",
                    limit=admission.budget.max_capture_bytes,
                    observed=state.capture_bytes + emitted_bytes,
                    node_id=node.node_id,
                )
            state.capture_bytes += emitted_bytes
        try:
            receipt = self._deliver_terminal_egress(
                request, admission=admission, manifests=manifests
            )
        except TerminalEgressError as exc:
            refusal = self._terminal_refusal(exc, node_id=node.node_id)
            if effectful:
                self._append_event(
                    admission,
                    records,
                    "terminal_egress",
                    {**payload, "verdict": "refused", "refusal_code": refusal.refusal.code},
                )
            raise refusal from exc
        except BaseException:
            if effectful:
                self._append_event(
                    admission,
                    records,
                    "terminal_egress",
                    {**payload, "verdict": "failed", "refusal_code": "unexpected_exception"},
                )
            raise
        try:
            verify_terminal_egress_receipt(request, receipt)
        except TerminalEgressError as exc:
            if effectful:
                self._append_event(
                    admission,
                    records,
                    "terminal_egress",
                    {**payload, "verdict": "refused", "refusal_code": "terminal_egress_unverified"},
                )
            raise _RunRefusal(
                "terminal_egress_unverified",
                str(exc),
                node_id=node.node_id,
            ) from exc
        self._append_event(
            admission,
            records,
            "terminal_egress",
            {
                **payload,
                "verdict": "delivered",
                "granted_operation": request.granted_operation,
                "receipt": receipt.model_dump(mode="json"),
            },
        )
        if request.kind == "post_inbox":
            state.inbox_items += len(request.items)
        if isinstance(request, TerminalEgressRequestV2) and request.kind == "emit_capture":
            assert isinstance(receipt, TerminalEgressReceiptV2)  # verified above
            for child in receipt.children:
                if isinstance(node, CaptureEgressNode) and len(receipt.children) == 1:
                    state.terminal_capture = child.egress_digest
                    derived = derive_terminal_item_facts(
                        manifest_dependency_tokens(manifests[child.item_key]),
                        manifest=manifests[child.item_key],
                        child_index=child.child_index,
                        facts=state.facts,
                        outcomes=tuple(state.outcomes.values()),
                    )
                    state.facts[child.egress_digest] = DependencyEvidenceFactsV1(
                        epistemic_grade=derived.epistemic_grade,
                        provenance_grade=derived.provenance_grade,
                        taint_labels=derived.taint_labels,
                        selector_privacy=derived.selector_privacy,
                    )
                self._append_event(
                    admission,
                    records,
                    "produced_capture",
                    {
                        "tag": "playbill-procedure-produced-capture-v1",
                        "node_id": node.node_id,
                        "capture_digest": child.egress_digest,
                        "capture_contract_digest": receipt.bound_artifact_digest,
                        "producer_receipt_digest": receipt.producer_receipt_digest,
                        "observed_at": format_datetime(request.evaluation_time),
                    },
                )

    def _deliver_terminal_egress(
        self,
        request: TerminalEgressRequestV1,
        *,
        admission: ProcedureRunAdmission,
        manifests: Mapping[str, TerminalItemDependencyManifestV1],
    ) -> TerminalEgressReceiptV1:
        """Hand the request to the sink; a preparer sink also receives the admission.

        An inbox post goes to the sink's inbox door with each item's manifest,
        from which it attaches exactly the Captures that item's closure reached.
        """

        assert self.egress_sink is not None
        if request.kind == "post_inbox":
            if not isinstance(self.egress_sink, InboxEgressSinkProtocol):
                raise TerminalEgressError("this run's egress sink does not deliver post_inbox")
            return self.egress_sink.deliver_inbox_egress(request=request, manifests=manifests)
        if isinstance(self.egress_sink, TerminalEgressPreparerProtocol):
            return self.egress_sink.deliver_terminal_egress(  # type: ignore[call-arg]
                request=request,
                admission=admission,
            )
        return self.egress_sink.deliver_terminal_egress(request=request)

    @staticmethod
    def _terminal_refusal(exc: TerminalEgressError, *, node_id: str) -> _RunRefusal:
        """Project a typed door refusal as the run's own node refusal, code intact."""

        from cruxible_core.procedures.terminal_services import (
            ProposalDeliveryRefused,
        )

        if isinstance(exc, ProposalDeliveryRefused):
            return _RunRefusal(
                cast(ProcedureNodeRefusalCode, exc.code),
                str(exc),
                node_id=node_id,
                details=exc.details,
            )
        if isinstance(exc, TerminalAuthorityRefusal):
            return _RunRefusal(
                cast(ProcedureNodeRefusalCode, exc.code),
                str(exc),
                node_id=node_id,
                details={
                    "codes": list(exc.codes),
                    "repair_kind": exc.repair_kind,
                    "repair": exc.repair.model_dump(mode="json"),
                    "target_namespace": list(exc.target_namespace),
                },
            )
        return _RunRefusal("terminal_egress_unverified", str(exc), node_id=node_id)

    def _terminal_egress_request(
        self,
        node: CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode,
        *,
        admission: ProcedureRunAdmission,
        rung: EffectiveRungV1,
        children: tuple[TerminalChildReceiptV1, ...],
        values: tuple[CanonicalValue, ...],
        manifests: Mapping[str, TerminalItemDependencyManifestV1] | None = None,
        capped_by: EffectiveRungTermV1 | None = None,
    ) -> tuple[TerminalEgressRequestV1, PreparedTerminalEgressV1 | None]:
        """Hand the sink the exact pins this terminal kind's law traverses.

        A `propose_change_set` on a v5 admission is prepared first: the sink
        lowers the items through shared authoring and reports the paths that
        lowering actually changed and the exact live mandate it bound, and the
        v2 request is built from THOSE, so the mandate law sees what will be
        proposed rather than what the template declared. A settle terminal
        `capped_by` a term asks for propose authority only. One whose every item
        restated what is already live keeps its v1 request: nothing is proposed.
        """

        bound_pin: ArtifactPin | None = None
        if isinstance(node, CaptureEgressNode):
            bound_pin = self._pin(
                node.capture_contract,
                label=f"emit_capture {node.node_id!r} CaptureContract",
            )
        # The authority asked for: a capped settle asks for propose's alone.
        asked: TerminalEgressKindV1 = "propose_change_set" if capped_by is not None else node.kind
        request = TerminalEgressRequestV1(
            kind=node.kind,
            run_id=admission.run_id,
            node_id=node.node_id,
            accepted_coordinate=admission.accepted_coordinate,
            procedure_identity=admission.procedure_identity,
            procedure_artifact_digest=admission.procedure_artifact_digest,
            admission_binding_digest=admission.admission_binding_digest,
            effective_rung=rung.effective_rung,
            required_rung=TERMINAL_REQUIRED_RUNGS[asked],
            limiting_term=rung.limiting_term,
            granted_operation=rung.granted_operation(asked),
            capped_by=capped_by,
            bound_artifact_pin=bound_pin,
            actor_context=admission.actor_context,
            items=tuple(
                TerminalEgressItemV1(
                    child_index=child.child_index,
                    item_key=child.item_key,
                    manifest_digest=child.manifest_digest,
                    value=value,
                )
                for child, value in zip(children, values, strict=True)
            ),
            prepared_at=self.clock.now(),
        )
        # Ruling C4: a direct run's admission is never a V5+ Line carrier (it
        # has no accepted Line), but it still builds a real v2 request under
        # its own ephemeral Line spec -- the only authority basis it has.
        v2_capable = admission.plans_providers or self.ephemeral_spec_digest is not None
        if v2_capable and isinstance(node, CaptureEgressNode):
            return (
                build_terminal_egress_request_v2(
                    request,
                    admission=admission,
                    procedure_mandate_digest=None,
                    calibration_reading_digests=(),
                    target_paths=(),
                ),
                None,
            )
        if (
            v2_capable
            and isinstance(node, ProposeChangeSetNode)
            and isinstance(self.egress_sink, TerminalEgressPreparerProtocol)
        ):
            prepared = self.egress_sink.prepare_terminal_egress(
                request=request,
                admission=admission,
                manifests=manifests or {},
            )
            if prepared.proposes_nothing:
                # Every item restated what is already live, or the change is the
                # Line's open proposal's: there is nothing to propose and no
                # authority to exercise, so no effectful request.
                return request, prepared
            return (
                build_terminal_egress_request_v2(
                    request,
                    admission=admission,
                    procedure_mandate_digest=prepared.procedure_mandate_digest,
                    ephemeral_spec_digest=(
                        self.ephemeral_spec_digest
                        if prepared.procedure_mandate_digest is None
                        else None
                    ),
                    calibration_reading_digests=(),
                    target_paths=prepared.target_paths,
                ),
                prepared,
            )
        return request, None

    def _record_terminal_items(
        self,
        node: CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        limit: int | None = None,
    ) -> tuple[
        tuple[TerminalChildReceiptV1, ...],
        tuple[CanonicalValue, ...],
        dict[str, TerminalItemDependencyManifestV1],
        int,
    ]:
        """Derive one manifest per terminal item from the exact closure it consumed.

        ``limit`` keeps the first items only (an inbox cap); the last element is
        how many items the terminal selected before it.
        """

        declared = _terminal_item_templates(node)
        base = _node_policy_tokens(node, self.references) | state.control
        if isinstance(node, ProposeChangeSetNode) and isinstance(
            node.candidate_templates, ProposalItemsFanOut
        ):
            values, item_tokens = _fan_out_items(
                node.candidate_templates, state=state, node_id=node.node_id
            )
        elif isinstance(node, InboxEgressNode):
            values, item_tokens = _inbox_items(node, state=state)
        else:
            values, item_tokens = _terminal_items(declared, state=state)
        if isinstance(node, ProposeChangeSetNode):
            from cruxible_core.procedures.source_candidates import bind_source_candidate

            try:
                values = [
                    bind_source_candidate(
                        value,
                        procedure_identity=admission.procedure_identity,
                        procedure_digest=admission.procedure_artifact_digest,
                        outputs=state.outputs,
                        provenance=state.provenance,
                        item_tokens=base | item_tokens[index],
                    )
                    for index, value in enumerate(values)
                ]
            except (ValueError, KeyError) as exc:
                raise _RunRefusal("proposal_item_invalid", str(exc), node_id=node.node_id) from exc
        selected = len(values)
        if limit is not None:
            values, item_tokens = list(values[:limit]), tuple(item_tokens[:limit])
        if isinstance(node, InboxEgressNode):
            # Bytes are bounded before anything is journaled or posted.
            state.inbox_bytes += _check_inbox_bytes(values, state=state, node_id=node.node_id)
        outcomes = tuple(
            sorted(state.outcomes.values(), key=lambda item: item.input_name.encode("utf-8"))
        )
        receipts: list[TerminalChildReceiptV1] = []
        manifests: dict[str, TerminalItemDependencyManifestV1] = {}
        payloads: list[object] = []
        item_drafts: list[tuple[str, str]] = []
        recorded_at: list[datetime] = []
        for index, value in enumerate(values):
            tokens = base | item_tokens[index]
            item_key = terminal_item_key(
                terminal_node_id=node.node_id,
                child_index=index,
                item=value,
            )
            manifest = build_terminal_item_manifest(
                tokens,
                run_id=admission.run_id,
                terminal_node_id=node.node_id,
                item_key=item_key,
            )
            derived = derive_terminal_item_facts(
                tokens,
                manifest=manifest,
                child_index=index,
                facts=state.facts,
                outcomes=outcomes,
            )
            payloads.append(
                {
                    "tag": "playbill-procedure-item-dependencies-v1",
                    "manifest": manifest.model_dump(mode="json"),
                    "derived": derived.model_dump(mode="json"),
                    "audit": {
                        "deployment_digest": admission.deployment_snapshot_digest,
                        "recorded_at": format_datetime(self.clock.now()),
                    },
                }
            )
            # Preserve the per-item clock reads and their order from single appends.
            recorded_at.append(self.clock.now())
            item_drafts.append((item_key, derived.manifest_digest))
            manifests[item_key] = manifest
        group = self._append_events(
            admission,
            records,
            "item_dependencies",
            payloads,
            recorded_at=recorded_at,
        )
        for index, (stored, (item_key, manifest_digest)) in enumerate(zip(group, item_drafts)):
            receipts.append(
                TerminalChildReceiptV1(
                    child_index=index,
                    item_key=item_key,
                    manifest_digest=manifest_digest,
                    record_digest=stored.record_digest,
                    sequence=stored.record.sequence,
                )
            )
        return tuple(receipts), tuple(values), manifests, selected

    def _planned_external_occurrence(
        self,
        *,
        state: _RunState,
        node_id: str,
        repeat_node_id: str | None,
        occurrence_kind: Literal["provider", "source"],
    ) -> ProviderExternalOccurrencePlan:
        plan = state.acquisition_plan
        if plan is None:
            raise _RunRefusal(
                "provider_acquisition_plan_required",
                "Graph-v4 external execution requires a complete admitted acquisition plan.",
                node_id=node_id,
            )
        matches = tuple(
            item
            for item in plan.external_occurrences
            if (
                item.occurrence_kind == occurrence_kind
                or occurrence_kind == "provider"
                and item.occurrence_kind == "call"
            )
            and item.node_id == node_id
            and item.repeat_node_id == repeat_node_id
        )
        if len(matches) != 1:
            raise _RunRefusal(
                "provider_acquisition_plan_mismatch",
                "The admitted acquisition plan does not name this external occurrence exactly.",
                node_id=node_id,
            )
        return matches[0]

    def _validate_operation_value(
        self,
        occurrence: ProviderExternalOccurrencePlan,
        value: CanonicalValue,
        *,
        direction: Literal["in", "out"],
        admission: ProcedureRunAdmission,
        state: _RunState,
        node_id: str,
        absent_captures: frozenset[str] = frozenset(),
    ) -> CanonicalValue:
        """Check one value against the bound operation's exact in or out Contract.

        ``absent_captures`` names capture fields the run did not commit (an
        oversized capture output): the value is checked as if they were
        optional, since the call's value never names them.
        """

        ref = occurrence.operation.in_ if direction == "in" else occurrence.operation.out
        resolve = closure_lookup(occurrence.contracts)
        contract = resolve(ref)
        boundary = f"operation-{direction}:{occurrence.operation.name}"
        if contract is None:
            raise _InternalFailure(
                "provider_protocol_violation",
                details={"node_id": node_id, "reason": f"{ref.spelling} is not in the plan"},
            )
        if absent_captures:
            contract = contract.model_copy(
                update={
                    "fields": {
                        name: declared.model_copy(update={"optional": True})
                        if name in absent_captures
                        else declared
                        for name, declared in contract.fields.items()
                    }
                }
            )
        try:
            validate_operation_value(
                value,
                contract,
                resolve=resolve,
                enum_members=self.enum_resolver,
                max_items=_effective_max_items(admission),
                observe_items=lambda count, path: state.observe_items(count, boundary, path),
            )
        except OperationItemBudgetExceeded as exc:
            state.observe_items(exc.observed, boundary, exc.field_path)
            raise _budget_refusal(
                budget_kind="max_items",
                limit=exc.limit,
                observed=exc.observed,
                node_id=node_id,
                boundary=boundary,
                field_path=exc.field_path,
            ) from exc
        except OperationValueRefused as exc:
            raise _RunRefusal(
                "contract_input_refused" if direction == "in" else "contract_output_refused",
                f"{ref.spelling}: {exc}",
                node_id=node_id,
            ) from exc
        return normalize_canonical(value)

    def _refusal_detail_check(
        self, occurrence: ProviderExternalOccurrencePlan
    ) -> RefusalDetailCheck:
        """Check a declared refusal's detail against the Contract the operation pins."""

        resolve = closure_lookup(occurrence.contracts)

        def check(code: str, detail: object) -> tuple[str, ...] | None:
            declared = occurrence.operation.refusals[code]
            contract = resolve(declared.contract)
            if contract is None:
                return (f"{declared.contract.spelling} is not in the plan",)
            try:
                validate_operation_value(
                    provider_canonical_value(detail) if isinstance(detail, dict) else detail,
                    contract,
                    resolve=resolve,
                    enum_members=self.enum_resolver,
                )
            except OperationValueRefused as exc:
                return exc.problems or (str(exc),)
            return None

        return check

    def _invoke_provider_v4(
        self,
        *,
        node_id: str,
        slot: str | None,
        repeat_node_id: str | None,
        effect_policy: ArtifactNameRef | None,
        payload: CanonicalValue,
        contract_out: ArtifactPin | None,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        occurrence: ProviderExternalOccurrencePlan | None = None,
    ) -> tuple[CanonicalValue, ProviderInvocationReceipt, str]:
        occurrence = occurrence or self._planned_external_occurrence(
            state=state,
            node_id=node_id,
            repeat_node_id=repeat_node_id,
            occurrence_kind="provider",
        )
        if not self._occurrence_matches_slot(occurrence, slot):
            raise _RunRefusal(
                "provider_acquisition_plan_mismatch",
                "The admitted occurrence is not its node's slot binding.",
                node_id=node_id,
            )
        payload = self._validate_operation_value(
            occurrence,
            payload,
            direction="in",
            admission=admission,
            state=state,
            node_id=node_id,
        )
        if (
            self.provider_runtime_invoker is None
            and self.provider_runtime_invoker_factory is not None
        ):
            self.provider_runtime_invoker = self.provider_runtime_invoker_factory()
        if self.provider_runtime_invoker is None:
            raise _RunRefusal(
                "provider_unavailable",
                "No local Provider runtime invoker is installed.",
                node_id=node_id,
            )
        # The run's deadline, in this executor's clock, goes with the Provider
        # child spawned for this occurrence: the spawner refuses or clips
        # against it immediately before the child exists. Core classifies no
        # input: every input that passes the operation's input Contract reaches
        # the Provider, whose own typed refusals decline what it cannot serve.
        deadline = ProviderSpawnDeadline(
            deadline_ns=state.run_started_monotonic_ns
            + admission.budget.wall_clock.microseconds * 1000,
            monotonic_ns=self.clock.monotonic_ns,
        )
        if not isinstance(payload, dict):
            raise _RunRefusal(
                "provider_protocol_violation",
                "Provider runtime input must be a canonical object.",
                node_id=node_id,
            )
        effectful = occurrence.effect_class == "external_mutation"
        if effectful != (effect_policy is not None):
            raise _RunRefusal(
                "provider_effect_declaration_mismatch",
                "The node effect-policy presence differs from its bound operation's effect.",
                node_id=node_id,
            )
        input_digest = run_value_digest("provider-input", payload)
        effect_id = typed_digest(
            ArtifactDigest,
            "playbill-procedure-effect-intent-v1",
            {
                "run_id": admission.run_id,
                "node_id": node_id,
                "provider_digest": occurrence.provider_artifact_digest,
                "implementation_digest": occurrence.implementation_digest,
                "input_digest": input_digest,
            },
        ).tagged
        if effectful:
            assert effect_policy is not None
            self._checkpoint_current(admission, effect=True)
            self._append_event(
                admission,
                records,
                "effect_intent",
                {
                    "effect_id": effect_id,
                    "node_id": node_id,
                    "provider_artifact_digest": occurrence.provider_artifact_digest,
                    "implementation_digest": occurrence.implementation_digest,
                    "effect_policy": self._pin(
                        effect_policy,
                        label=f"provider {node_id!r} effect policy",
                    ).model_dump(mode="json"),
                    "input_digest": input_digest,
                },
            )
            refusal = effect_dispatch_refusal(
                invocation_origin=admission.invocation_origin,
                actor_context=admission.actor_context,
                declared_effect_grants=self.declared_effect_grants,
            )
            if refusal is not None:
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCode, refusal[0]),
                    refusal[1],
                    node_id=node_id,
                )
        if state.provider_calls >= admission.budget.max_provider_calls:
            raise _BudgetExceeded(
                "max_provider_calls",
                limit=admission.budget.max_provider_calls,
                observed=state.provider_calls + 1,
                node_id=node_id,
            )
        state.provider_calls += 1
        invocation_id = typed_digest(
            Sha256Value,
            "playbill-provider-invocation-v1",
            {
                "run_id": admission.run_id,
                "occurrence_path": occurrence.occurrence_path,
                "provider_call_ordinal": state.provider_calls,
                "admission_binding_digest": admission.admission_binding_digest,
            },
        ).tagged
        try:
            bound = self.provider_runtime_invoker.bind_provider(occurrence=occurrence)
        except ProviderLocalRuntimeRefused as exc:
            outcome = map_provider_refusal(
                exc.code,
                message=str(exc),
                detail=exc.details,
            )
            if outcome.outcome_class == "node_refusal":
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCode, outcome.code),
                    outcome.message or "Provider binding refused.",
                    node_id=node_id,
                    details=outcome.detail,
                ) from exc
            if outcome.outcome_class == "operational":
                raise _OperationalFailure(
                    outcome.code or "provider_execution_error",
                    details=outcome.detail,
                    cause=_provider_failure_cause(outcome, node_id=node_id),
                ) from exc
            raise _InternalFailure(
                outcome.code or "provider_protocol_violation", details=outcome.detail
            ) from exc
        budget = occurrence.budget_translation
        no_budget_message = "No Procedure wall-clock budget remains before Provider spawn."

        def effective_wall_clock_seconds() -> float:
            """The Provider's window: its own cap, clipped to what the run has left now."""
            elapsed_microseconds = max(
                0,
                (self.clock.monotonic_ns() - state.run_started_monotonic_ns) // 1000,
            )
            remaining_run_microseconds = max(
                0,
                admission.budget.wall_clock.microseconds - elapsed_microseconds,
            )
            return min(
                float(budget.runtime_wall_clock_seconds),
                remaining_run_microseconds / 1_000_000,
            )

        # A run already out of time refuses before anything is journaled, so no
        # start is recorded for an invocation that never spawns.
        if effective_wall_clock_seconds() <= 0:
            raise _RunRefusal("budget_wall_clock", no_budget_message, node_id=node_id)
        started = ProviderInvocationStarted(
            invocation_id=invocation_id,
            occurrence_path=occurrence.occurrence_path,
            implementation_digest=occurrence.implementation_digest,
            materialization_digest=occurrence.local_execution.materialization_digest,
            input_digest=input_digest,
        )
        self._append_event(
            admission,
            records,
            "provider_invocation_started",
            started.model_dump(mode="json"),
        )
        state.provider_invocations_started += 1
        # The durable start write takes time too, so the window is measured
        # again after it. That is not the last check: the run's deadline goes
        # with the invocation, and the spawner refuses or clips against it
        # immediately before the child exists. Whichever check finds the time
        # spent, the Provider never spawns and the durable start is closed by a
        # matching completion carrying the budget_wall_clock refusal.
        wall_clock_seconds = effective_wall_clock_seconds()
        driver_result: ProviderDriverOutcomeV1 | None = None
        try:
            if wall_clock_seconds <= 0:
                raise ProviderLocalRuntimeRefused("budget_wall_clock", no_budget_message)
            context = ProviderRuntimeRunContext(
                protocol_version=occurrence.local_execution.protocol_version,
                run_id=admission.run_id,
                # One operation of one build: exactly the admitted binding.
                operation=occurrence.operation.name,
                operation_signature_digest=occurrence.operation_signature_digest,
                build_digest=occurrence.provider_artifact_digest,
                implementation_digest=occurrence.implementation_digest,
                entrypoint=occurrence.local_execution.entrypoint,
                coordinates={
                    "accepted_coordinate": admission.accepted_coordinate.model_dump(mode="json"),
                    "occurrence_path": occurrence.occurrence_path,
                    "invocation_id": invocation_id,
                },
                input=payload,
                # The runtime protocol names one CaptureContract: the call's,
                # when it commits exactly one capture field.
                capture_contract=(
                    next(iter(occurrence.capture_contracts.values()))
                    if len(occurrence.capture_contracts) == 1
                    else None
                ),
                budgets=ProviderRuntimeBudgets(
                    wall_clock_seconds=wall_clock_seconds,
                    output_bytes=budget.runtime_output_bytes_cap,
                    cost_units=None,
                ),
                declared_endpoints=occurrence.local_execution.declared_endpoints,
            )
            driver_result = self.provider_runtime_invoker.invoke_provider(
                occurrence=occurrence,
                context=context,
                invocation_id=invocation_id,
                bound=bound,
                deadline=deadline,
            )
            # The provider's word: a harness runtime code or one the operation
            # declares, its detail checked before it is attributed to either.
            outcome = map_provider_envelope(
                driver_result.envelope,
                operation=occurrence.operation,
                check_detail=self._refusal_detail_check(occurrence),
            )
        except ProviderLocalRuntimeRefused as exc:
            # Core itself raised this (bind/spawn/deadline/budget failure), never
            # a parsed Provider envelope, so its runtime code is core's own.
            outcome = map_provider_refusal(
                exc.code,
                message=str(exc),
                detail=exc.details,
            )
        except ProviderRuntimeWireError as exc:
            outcome = map_provider_refusal(exc.code, message=str(exc), detail={})
        if driver_result is None:
            egress = ProviderEgressObservation(
                declared_endpoints=tuple(
                    item
                    for item in occurrence.local_execution.declared_endpoints
                    if not item.startswith("dynamic:")
                ),
                dynamic_endpoint_forms=cast(
                    tuple[Literal["dynamic:target-from-run-input"], ...],
                    tuple(
                        item
                        for item in occurrence.local_execution.declared_endpoints
                        if item == "dynamic:target-from-run-input"
                    ),
                ),
                # A core built-in has no child to self-report; core is the observer.
                observer_backend=(
                    "core.in-process"
                    if occurrence.local_execution.fence_scope == "in_process"
                    else "child-self-report"
                ),
                observer_grade="attribution",
            )
            output = None
            trace: CanonicalValue = {}
            stderr = ""
            duration_microseconds = 0
        else:
            egress = driver_result.egress
            output = (
                provider_canonical_value(driver_result.envelope.output)
                if driver_result.envelope.output is not None
                else None
            )
            trace = provider_canonical_value(driver_result.envelope.trace.model_dump(mode="python"))
            stderr = driver_result.stderr
            duration_microseconds = max(0, round(driver_result.duration_seconds * 1_000_000))
        verified_binding = (
            driver_result.verified_binding
            if driver_result is not None
            else occurrence.local_execution
        )
        journal_output = (
            ProviderInvocationOutputDigest(
                output_digest=provider_invocation_output_digest(output)
            ).model_dump(mode="json")
            if occurrence.occurrence_kind == "source" and output is not None
            else output
        )
        receipt = ProviderInvocationReceipt(
            invocation_id=invocation_id,
            occurrence_path=occurrence.occurrence_path,
            run_id=admission.run_id,
            admission_binding_digest=admission.admission_binding_digest,
            provider_artifact_digest=verified_binding.provider_artifact_digest,
            implementation_digest=verified_binding.implementation_digest,
            materialization_digest=verified_binding.materialization_digest,
            deployment_digest=verified_binding.deployment_digest,
            operation=verified_binding.operation,
            operation_signature_digest=verified_binding.operation_signature_digest,
            protocol_version=verified_binding.protocol_version,
            capture_contracts=occurrence.capture_contracts,
            input_digest=input_digest,
            outcome=outcome,
            output=journal_output,
            egress=egress,
            # From the binding: a subprocess Provider's process-group fence, or
            # "in_process" for a core built-in (providers/builtin_runtime.py).
            fence_scope=verified_binding.fence_scope,
            secret_references=tuple(
                sorted(
                    (
                        ProviderSecretReceiptReference(
                            binding_identity_digest=provider_secret_binding_identity_digest(
                                ProviderSecretBindingIdentity(
                                    realm=reference.realm,
                                    name=reference.name,
                                )
                            ),
                            purpose=reference.purpose,
                        )
                        for reference in occurrence.secret_plan.references
                    ),
                    key=lambda item: item.binding_identity_digest.encode("ascii"),
                )
            ),
            budget_translation=budget,
            duration_microseconds=duration_microseconds,
            trace=trace,
            stderr=stderr,
        )
        receipt_digest = provider_invocation_receipt_digest(receipt)
        completed = ProviderInvocationCompleted(
            invocation_id=invocation_id,
            receipt=receipt,
            receipt_digest=receipt_digest,
        )
        self._append_event(
            admission,
            records,
            "provider_invocation_completed",
            completed.model_dump(mode="json"),
        )
        state.provider_invocations_completed += 1
        state.invocation_receipt_digests.append(receipt_digest)
        if outcome.status != "ok":
            if outcome.outcome_class == "node_refusal":
                raise _ProviderNodeRefusal(
                    outcome,
                    node_id=node_id,
                    declared=operation_refusal_is_declared(occurrence.operation, outcome),
                )
            if outcome.outcome_class == "operational":
                raise _OperationalFailure(
                    outcome.code or "provider_execution_error",
                    details=outcome.detail,
                    cause=_provider_failure_cause(outcome, node_id=node_id),
                )
            raise _InternalFailure(
                outcome.code or "provider_protocol_violation", details=outcome.detail
            )
        assert output is not None
        if occurrence.occurrence_kind == "call":
            # F02: every operation output is checked against its own pinned
            # Contract. An acquiring call's output is checked by the same
            # Contract once core has committed each capture field and put its
            # Capture digest in that field (``_run_acquiring_call``): what the
            # operation returns there is Capture wire, not yet a digest.
            output = self._validate_operation_value(
                occurrence,
                output,
                direction="out",
                admission=admission,
                state=state,
                node_id=node_id,
            )
        validated = (
            _validate_node_contract(
                self.contract_validator,
                contract=contract_out,
                payload=output,
                direction="output",
                node_id=node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            if contract_out is not None
            else normalize_canonical(output)
        )
        if effectful:
            self._append_event(
                admission,
                records,
                "effect_result",
                {
                    "effect_id": effect_id,
                    "node_id": node_id,
                    "output_digest": run_value_digest("provider-output", validated),
                    "invocation_receipt_digest": receipt_digest,
                },
            )
        return validated, receipt, receipt_digest

    def _run_provider_v4(
        self,
        node: CallNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        resolved = normalize_canonical(
            _resolve_template(
                node.input,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
        )
        output, _receipt, _receipt_digest = self._invoke_provider_v4(
            node_id=node.node_id,
            slot=node.slot,
            repeat_node_id=None,
            effect_policy=node.effect_policy,
            payload=resolved,
            contract_out=None,
            admission=admission,
            state=state,
            records=records,
        )
        state.outputs[node.as_] = output
        state.provenance[node.as_] = AliasProvenanceV1(
            whole=_base_tokens(node, state, node.input, self.references)
        )

    def _run_repeat(
        self,
        node: RepeatNode,
        *,
        admission: ProcedureRunAdmission,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> None:
        attempts: list[CanonicalValue] = []
        # Repeat is the AND over the attempts used: every body output of every
        # attempt, each the AND of what it read (and a truncating call is not).
        attempts_complete = True
        for attempt in range(1, node.max_attempts + 1):
            local_outputs: dict[str, CanonicalValue] = {}
            local_provenance: dict[str, AliasProvenanceV1] = {}
            local_incomplete: set[str] = set()
            for body in node.body:
                self._run_repeat_body(
                    body,
                    repeat_node_id=node.node_id,
                    admission=admission,
                    state=state,
                    local_outputs=local_outputs,
                    local_provenance=local_provenance,
                    records=records,
                )
                body_complete = all(
                    alias not in local_incomplete
                    if alias in local_outputs and alias != body.as_
                    else state.completeness.reads_complete(((alias, path),))
                    for alias, path in _referenced_reads(body.spec)
                ) and not (body.operation == "call" and self._truncates(body.slot))
                if not body_complete:
                    local_incomplete.add(body.as_)
                    attempts_complete = False
                self._check_budget(
                    admission,
                    state,
                    started_ns=started_ns,
                    node_id=body.node_id,
                )
            try:
                verdict, trace = _evaluate_predicate(
                    node.until,
                    input_payload=state.input_payload,
                    outputs=local_outputs,
                    parameters=state.parameters,
                )
            except ExecutionError as exc:
                raise _RunRefusal(
                    "runtime_reference_unresolved",
                    "A repeat-until runtime reference did not resolve.",
                    node_id=node.node_id,
                ) from exc
            attempt_value = normalize_canonical(
                {"attempt": attempt, "outputs": local_outputs, "until": verdict}
            )
            attempts.append(attempt_value)
            self._append_event(
                admission,
                records,
                "branch_evaluated",
                {
                    "node_id": node.node_id,
                    "repeat_attempt": attempt,
                    "verdict": verdict,
                    "operands": trace,
                    "body_lineage": {
                        alias: _alias_provenance_payload(provenance)
                        for alias, provenance in sorted(local_provenance.items())
                    },
                },
            )
            if verdict:
                state.outputs[node.as_] = normalize_canonical(
                    {"attempts": attempts, "final": local_outputs}
                )
                state.completeness.mark(node.as_, attempts_complete)
                repeated_tokens = frozenset(
                    token for provenance in local_provenance.values() for token in provenance.whole
                )
                state.provenance[node.as_] = AliasProvenanceV1(
                    whole=_base_tokens(
                        node, state, tuple(body.spec for body in node.body), self.references
                    )
                    | repeated_tokens
                )
                return
        raise _RunRefusal(
            "repeat_exhausted",
            "Procedure repeat exhausted its declared attempt bound.",
            node_id=node.node_id,
        )

    def _run_repeat_body(
        self,
        body: RepeatBodyNode,
        *,
        repeat_node_id: str,
        admission: ProcedureRunAdmission,
        state: _RunState,
        local_outputs: dict[str, CanonicalValue],
        local_provenance: dict[str, AliasProvenanceV1],
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        combined = {**state.outputs, **local_outputs}
        declared_spec = (
            _declared_transform_spec(body.transform_kind, body.spec)
            if body.operation == "transform" and body.transform_kind is not None
            else body.spec
        )
        spec = _resolve_node_template(
            declared_spec,
            node_id=body.node_id,
            transform_kind=(body.transform_kind if body.operation == "transform" else None),
            input_payload=state.input_payload,
            outputs=combined,
        )
        if body.operation == "transform":
            assert body.contract_in is not None and body.contract_out is not None
            transform_kind = getattr(body, "transform_kind", None)
            if not isinstance(transform_kind, str):
                raise _RunRefusal(
                    "runtime_reference_unresolved",
                    "Repeat transform body has no declared transform kind.",
                    node_id=body.node_id,
                )
            contract_in = self._pin(body.contract_in, label=f"repeat {body.node_id!r} input")
            contract_out = self._pin(body.contract_out, label=f"repeat {body.node_id!r} output")
            validated = _validate_node_contract(
                self.contract_validator,
                contract=contract_in,
                payload=spec,
                direction="input",
                node_id=body.node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            value, lineage = _apply_node_transform(
                transform_kind,
                validated,
                max_items=_effective_max_items(admission),
                node_id=body.node_id,
                list_boundary=_kernel_list_boundary(self.contract_validator, contract_out),
            )
            local_outputs[body.as_] = _validate_node_contract(
                self.contract_validator,
                contract=contract_out,
                payload=value,
                direction="output",
                node_id=body.node_id,
                max_items=_effective_max_items(admission),
                observe_items=state.observe_items,
            )
            local_state = _RunState(
                outputs=combined,
                input_payload=state.input_payload,
                parameters=state.parameters,
                provenance={**state.provenance, **local_provenance},
                facts=state.facts,
                outcomes=state.outcomes,
                control=state.control,
                provider_calls=state.provider_calls,
                capture_bytes=state.capture_bytes,
            )
            local_provenance[body.as_] = _transform_provenance(
                body,
                state=local_state,
                lineage=lineage,
                base=_base_tokens(body, local_state, declared_spec, self.references),
            )
            return
        if body.operation == "call":
            local_outputs[body.as_], _receipt, _receipt_digest = self._invoke_provider_v4(
                node_id=body.node_id,
                slot=body.slot,
                repeat_node_id=repeat_node_id,
                effect_policy=body.effect_policy,
                payload=spec,
                contract_out=None,
                admission=admission,
                state=state,
                records=records,
            )
            local_state = _RunState(
                outputs=combined,
                input_payload=state.input_payload,
                parameters=state.parameters,
                provenance={**state.provenance, **local_provenance},
                facts=state.facts,
                outcomes=state.outcomes,
                control=state.control,
            )
            local_provenance[body.as_] = AliasProvenanceV1(
                whole=_base_tokens(body, local_state, body.spec, self.references)
            )
            return
        raise ExecutionError(f"unsupported repeat operation {body.operation!r}")

    def _append_events(
        self,
        admission: ProcedureRunAdmission,
        records: list[StoredProcedureJournalRecordV1],
        event_kind: JournalEventKindV1,
        payloads: list[object],
        *,
        recorded_at: list[datetime],
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        """Reserve, publish and append one terminal's item records as a group."""
        if not payloads:
            return ()
        payload_bytes = [journal_payload_bytes(payload) for payload in payloads]
        reservations = [
            make_run_reservation(
                instance_id=admission.instance_id,
                partition_id=admission.journal_partition_id,
                event_kind=event_kind,
                run_id=admission.run_id,
                admission_binding_digest=admission.admission_binding_digest,
                body_digest=self.bodies.digest_bytes(content).tagged,
            )
            for content in payload_bytes
        ]
        with self.material_reservations.locked():
            group_id = self.material_reservations.reserve_many_locked(reservations)
            metadata = self.bodies.store_many(payload_bytes, journal_payload=True)
            head = self.journal.read_head(admission.journal_stream, admission.journal_partition_id)
            drafts = [
                ProcedureJournalRecordDraftV1(
                    stream=admission.journal_stream,
                    partition_id=admission.journal_partition_id,
                    event_kind=event_kind,
                    accepted_coordinate=admission.accepted_coordinate,
                    procedure_artifact_digest=admission.procedure_artifact_digest,
                    definition_digest=admission.definition_digest,
                    run_id=admission.run_id,
                    line_spec_digest=admission.line_spec_digest,
                    occurrence_id=admission.occurrence_id,
                    attempt=admission.attempt,
                    admission_binding_digest=admission.admission_binding_digest,
                    payload_digest=item.digest,
                    actor_context=admission.actor_context,
                    recorded_at=when,
                )
                for item, when in zip(metadata, recorded_at)
            ]
            try:
                group = self.journal.append_many(
                    drafts,
                    expected_head=head,
                    fencing_token=self.fencing_token,
                )
            except JournalError as exc:
                raise ProcedureBoundaryRefused(
                    _journal_refusal_code(exc),
                    "Procedure journal append refused.",
                ) from exc
            if len(group) != len(reservations) or any(
                stored.record.payload_digest != reservation.body_digest
                or stored.record.event_kind != reservation.intended_event_kind
                or stored.record.run_id != reservation.run_id
                or stored.record.admission_binding_digest != reservation.admission_binding_digest
                or stored.record.stream != admission.journal_stream
                or stored.record.partition_id != admission.journal_partition_id
                for stored, reservation in zip(group, reservations)
            ):
                raise ExecutionError("journal append did not reproduce its material reservations")
            self.material_reservations.release_locked(group_id)
        records.extend(group)
        self.run_index.apply_records(group)
        return group

    def _append_event(
        self,
        admission: ProcedureRunAdmission,
        records: list[StoredProcedureJournalRecordV1],
        event_kind: JournalEventKindV1,
        payload: object,
    ) -> StoredProcedureJournalRecordV1:
        payload_bytes = journal_payload_bytes(payload)
        body_digest = self.bodies.digest_bytes(payload_bytes).tagged
        reservation = make_run_reservation(
            instance_id=admission.instance_id,
            partition_id=admission.journal_partition_id,
            event_kind=event_kind,
            run_id=admission.run_id,
            admission_binding_digest=admission.admission_binding_digest,
            body_digest=body_digest,
        )
        with self.material_reservations.locked():
            self.material_reservations.reserve_locked(reservation)
            pending_to_release: list[PendingAdmissionMaterialReservationV1] = []
            if event_kind == "admission_bound":
                active = self.material_reservations.active_locked()
                for member in (
                    payload.get("admission_material_manifest", {}).get("members", [])
                    if isinstance(payload, dict)
                    and isinstance(payload.get("admission_material_manifest"), dict)
                    else []
                ):
                    if not isinstance(member, dict) or member.get("body_digest") is None:
                        continue
                    matches = tuple(
                        item
                        for item in active
                        if isinstance(item, PendingAdmissionMaterialReservationV1)
                        and item.instance_id == admission.instance_id
                        and item.run_id == admission.run_id
                        and item.admission_binding_digest == admission.admission_binding_digest
                        and item.input_name == member.get("input_name")
                        and item.plane == member.get("plane")
                        and item.body_digest == member.get("body_digest")
                    )
                    if len(matches) != 1:
                        raise ExecutionError(
                            "run_recovery_required: admission material reservation is absent"
                        )
                    if not self.bodies.verify(matches[0].body_digest):
                        raise ExecutionError(
                            "run_recovery_required: reserved admission material is unavailable"
                        )
                    pending_to_release.append(matches[0])
            metadata = self.bodies.store(payload_bytes, journal_payload=True)
            head = self.journal.read_head(
                admission.journal_stream,
                admission.journal_partition_id,
            )
            draft = ProcedureJournalRecordDraftV1(
                stream=admission.journal_stream,
                partition_id=admission.journal_partition_id,
                event_kind=event_kind,
                accepted_coordinate=admission.accepted_coordinate,
                procedure_artifact_digest=admission.procedure_artifact_digest,
                definition_digest=admission.definition_digest,
                run_id=admission.run_id,
                line_spec_digest=admission.line_spec_digest,
                occurrence_id=admission.occurrence_id,
                attempt=admission.attempt,
                admission_binding_digest=admission.admission_binding_digest,
                payload_digest=metadata.digest,
                actor_context=admission.actor_context,
                recorded_at=self.clock.now(),
            )
            try:
                stored = self.journal.append(
                    draft,
                    expected_head=head,
                    fencing_token=self.fencing_token,
                )
            except JournalError as exc:
                # Classification is by exception class, never by grepping English
                # out of the message: the backend's prose is a diagnostic, and a
                # served refusal code derived from it misreads any wording the
                # keyword list does not happen to contain.
                raise ProcedureBoundaryRefused(
                    _journal_refusal_code(exc),
                    "Procedure journal append refused.",
                ) from exc
            # The reservation's invocation id is a function of exactly the six
            # values it was minted from (the admission's instance and partition,
            # the event kind, run, binding and body digest), so the stored record
            # reproduces it iff it carries those same six values.
            if (
                stored.record.payload_digest != reservation.body_digest
                or stored.record.event_kind != reservation.intended_event_kind
                or stored.record.run_id != reservation.run_id
                or stored.record.admission_binding_digest != reservation.admission_binding_digest
                or stored.record.stream.instance_id != admission.instance_id
                or stored.record.partition_id != admission.journal_partition_id
            ):
                raise ExecutionError("journal append did not reproduce its material reservation")
            for pending in pending_to_release:
                self.material_reservations.release_locked(pending.reservation_id)
            self.material_reservations.release_locked(reservation.reservation_id)
        records.append(stored)
        self.run_index.apply_record(stored, payload=normalize_canonical(payload))
        if event_kind == "provider_invocation_started":
            note_invocation_start(self.journal, stored, payload)
        return stored

    def _replay_completed(
        self,
        admission: ProcedureRunAdmission,
        records: tuple[StoredProcedureJournalRecordV1, ...],
    ) -> ProcedureRunResultV1:
        run_records = [
            item
            for item in records
            if item.record.run_id == admission.run_id
            and item.record.admission_binding_digest == admission.admission_binding_digest
        ]
        if not run_records or run_records[-1].record.event_kind != "attempt_finalized":
            raise ExecutionError("completed run index is not supported by journal exhaust")
        provider_starts = sum(
            item.record.event_kind == "provider_invocation_started" for item in run_records
        )
        provider_completions = sum(
            item.record.event_kind == "provider_invocation_completed" for item in run_records
        )
        if provider_starts != provider_completions:
            raise ProcedureBoundaryRefused(
                "provider_replay_receipt_required",
                "Replay requires one completed Provider receipt for every durable start.",
                details={
                    "provider_starts": provider_starts,
                    "provider_completions": provider_completions,
                },
            )
        access = BodyAccessContext(principal_id="procedure-run-replay", can_read_body=True)
        payload = read_payload(self.bodies, run_records[-1].record.payload_digest, access=access)
        if isinstance(payload, UnavailablePayload):
            return ProcedureRunResultV1(
                run_id=admission.run_id,
                status="failed",
                receipt=self._receipt(admission, run_records),
            )
        if not isinstance(payload, dict):
            raise ExecutionError("attempt-finalized payload is not an object")
        receipt = self._receipt(admission, run_records)
        try:
            return ProcedureRunResultV1.model_validate(
                {
                    "run_id": admission.run_id,
                    "status": payload.get("status"),
                    "output": payload.get("output"),
                    "refusal": payload.get("refusal"),
                    "receipt": receipt.model_dump(mode="json"),
                }
            )
        except ValueError as exc:
            raise ExecutionError(
                "attempt-finalized payload cannot reproduce its run result"
            ) from exc

    @staticmethod
    def _receipt(
        admission: ProcedureRunAdmission,
        records: list[StoredProcedureJournalRecordV1],
    ) -> ProcedureRunReceiptV1:
        if not records:  # pragma: no cover - attempt start always lands first
            raise ExecutionError("Procedure run produced no exhaust records")
        return ProcedureRunReceiptV1(
            run_id=admission.run_id,
            admission_binding_digest=admission.admission_binding_digest,
            stream=admission.journal_stream,
            partition_id=admission.journal_partition_id,
            first_sequence=records[0].record.sequence,
            last_sequence=records[-1].record.sequence,
            record_digests=tuple(item.record_digest for item in records),
            chain_head_digest=records[-1].record_digest,
        )


_STEP_PREFIX = "$steps."

_ITEM_SLOTS: tuple[str, ...] = ("items", "left_items", "right_items")


def _node_policy_tokens(node: object, resolved: Mapping[str, str]) -> frozenset[DependencyToken]:
    """Return the exact laws and policies one node's resolved pins commit it to."""

    return frozenset(
        policy_token(resolved[reference.target.qualified]) for reference in iter_references(node)
    )


def _referenced_aliases(value: object) -> frozenset[str]:
    """Return every step alias a declared template actually reads."""

    found: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.startswith(_STEP_PREFIX):
                found.add(item[len(_STEP_PREFIX) :].split(".")[0])
            return
        if isinstance(item, list | tuple):
            for member in item:
                visit(member)
            return
        if isinstance(item, dict):
            for member in item.values():
                visit(member)
            return
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="python", exclude={"tag"}))

    visit(value)
    return frozenset(found)


def _referenced_reads(value: object) -> tuple[Read, ...]:
    """Every ``(alias, path)`` a declared template reads, for the completeness law."""

    found: dict[Read, None] = {}

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.startswith(_STEP_PREFIX):
                alias, *path = item[len(_STEP_PREFIX) :].split(".")
                found[(alias, tuple(path))] = None
            return
        if isinstance(item, list | tuple):
            for member in item:
                visit(member)
            return
        if isinstance(item, dict):
            for member in item.values():
                visit(member)
            return
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="python", exclude={"tag"}))

    visit(value)
    return tuple(found)


def _node_reads(node: object) -> tuple[Read, ...]:
    """Every read a node's resolved fields make."""

    reads: dict[Read, None] = {}
    for _field, template in reference_templates(node):  # type: ignore[arg-type]
        reads.update(dict.fromkeys(_referenced_reads(template)))
    return tuple(reads)


def _predicate_reads(predicate: GuardPredicate) -> tuple[Read, ...]:
    """The reads a guard predicate's step operands make; a count reads no field."""

    found: list[Read] = []
    pending = [predicate]
    while pending:
        item = pending.pop()
        for operand in (item.left, item.right):
            if operand is None or operand.alias is None:
                continue
            reads_content = operand.kind in {"step", "exists"}
            found.append((operand.alias, tuple(operand.path) if reads_content else None))
        pending.extend(item.all_of or ())
        pending.extend(item.any_of or ())
        if item.not_of is not None:
            pending.append(item.not_of)
    return tuple(found)


def _template_alias(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(_STEP_PREFIX):
        return value[len(_STEP_PREFIX) :].split(".")[0]
    return None


def _lineage_alias(value: object) -> str | None:
    """The alias whose per-element lineage a template may use, or None.

    Per-element lineage is recorded for one exact path: the ``items`` list a
    transform or projection produced (``$steps.<alias>.items``). Any other path
    into the same alias -- a sibling field, a nested list -- has no lineage of
    its own and must not borrow that one.
    """

    if not isinstance(value, str) or not value.startswith(_STEP_PREFIX):
        return None
    parts = value[len(_STEP_PREFIX) :].split(".")
    return parts[0] if len(parts) == 2 and parts[1] == "items" else None


def _item_closure(state: _RunState, template: object, index: int) -> frozenset[DependencyToken]:
    """One element's closure through `template`: its own lineage, else the alias's whole."""

    alias = _template_alias(template)
    if alias is None:
        return frozenset()
    if _lineage_alias(template) == alias:
        return state.item_tokens(alias, index)
    return state.alias_tokens(frozenset({alias}))


def _base_tokens(
    node: object,
    state: _RunState,
    template: object,
    resolved: Mapping[str, str],
) -> frozenset[DependencyToken]:
    return (
        _node_policy_tokens(node, resolved)
        | state.control
        | state.alias_tokens(_referenced_aliases(template))
    )


def _transform_provenance(
    node: TransformNode | RepeatBodyNode,
    *,
    state: _RunState,
    lineage: tuple[tuple[tuple[str, int], ...], ...] | None,
    base: frozenset[DependencyToken],
) -> AliasProvenanceV1:
    if lineage is None:
        return AliasProvenanceV1(whole=base)
    spec = (
        node.spec
        if isinstance(node.spec, dict)
        else cast(BaseModel, node.spec).model_dump(mode="python", exclude={"tag"})
    )
    items: list[frozenset[DependencyToken]] = []
    for refs in lineage:
        tokens: frozenset[DependencyToken] = frozenset()
        for slot, index in refs:
            if slot in _ITEM_SLOTS:
                tokens = tokens | _item_closure(state, spec.get(slot), index)
        items.append(tokens)
    return AliasProvenanceV1(whole=base, items=tuple(items))


def _alias_provenance_payload(provenance: AliasProvenanceV1) -> CanonicalValue:
    def tokens(values: frozenset[DependencyToken]) -> list[CanonicalValue]:
        return [
            {"slot": token.slot, "digest": token.digest}
            for token in sorted(values, key=lambda item: (item.slot, item.digest))
        ]

    return {
        "whole": tokens(provenance.whole),
        "items": None if provenance.items is None else [tokens(item) for item in provenance.items],
    }


def _projected_provenance(
    fields: object,
    *,
    state: _RunState,
    base: frozenset[DependencyToken],
    value: CanonicalValue,
) -> AliasProvenanceV1:
    template = fields.get("items") if isinstance(fields, dict) else None
    if template is None or not isinstance(value, dict):
        return AliasProvenanceV1(whole=base)
    projected = value.get("items")
    if not isinstance(projected, list):
        return AliasProvenanceV1(whole=base)
    if _template_alias(template) is None:
        return AliasProvenanceV1(whole=base)
    return AliasProvenanceV1(
        whole=base,
        items=tuple(_item_closure(state, template, index) for index in range(len(projected))),
    )


def _inbox_cap(node: InboxEgressNode) -> tuple[int, Literal["declared", "default"]]:
    """The most items a run posts to the inbox, and whether the terminal declared it."""

    if node.max_items is not None:
        return node.max_items, "declared"
    return INBOX_RUN_ITEM_CAP_DEFAULT, "default"


def _terminal_item_templates(
    node: CaptureEgressNode | InboxEgressNode | ProposeChangeSetNode,
) -> object:
    if isinstance(node, ProposeChangeSetNode):
        templates = node.candidate_templates
        if isinstance(templates, tuple):
            return list(templates)
        return {"items": templates.items}
    return node.input


_CAPTURE_SLOTS = frozenset({"produced_capture", "admitted_capture"})


def _fan_out_items(
    fan_out: ProposalItemsFanOut,
    *,
    state: _RunState,
    node_id: str,
) -> tuple[list[CanonicalValue], tuple[frozenset[DependencyToken], ...]]:
    """One proposal item per element of a list the run produced, each with its own closure.

    Data decides the shape here, so a reference that does not resolve or a
    value that is not a list is a typed refusal, never an internal failure,
    and an object is never unwrapped. Each element's evidence closure is its
    own lineage where the producing step kept one: a Capture another element
    came from is not offered to it. Without per-element lineage, a list built
    from more than one Capture is refused rather than giving every element
    the union.
    """

    return _fan_out_list(
        fan_out.items,
        state=state,
        node_id=node_id,
        field="candidate_templates.items",
        noun="proposal items",
        invalid_code="proposal_item_invalid",
        ambiguous_code="proposal_item_evidence_ambiguous",
    )


def _fan_out_list(
    template: object,
    *,
    state: _RunState,
    node_id: str,
    field: str,
    noun: str,
    invalid_code: ProcedureNodeRefusalCode,
    ambiguous_code: ProcedureNodeRefusalCode,
) -> tuple[list[CanonicalValue], tuple[frozenset[DependencyToken], ...]]:
    """The elements of a list the run produced, each with only its own evidence closure."""

    try:
        resolved = _resolve_template(
            template, input_payload=state.input_payload, outputs=state.outputs
        )
    except ExecutionError as exc:
        raise _RunRefusal(
            invalid_code,
            f"The fan-out's items did not resolve: {exc}",
            node_id=node_id,
            details={"field": field, "reason": "items_unresolved"},
        ) from exc
    if not isinstance(resolved, list):
        raise _RunRefusal(
            invalid_code,
            f"The fan-out's items resolved to a {type(resolved).__name__}, not a list of {noun}.",
            node_id=node_id,
            details={"field": field, "reason": "items_not_a_list"},
        )
    values = [normalize_canonical(item) for item in resolved]
    alias = _template_alias(template)
    provenance = None if alias is None else state.provenance.get(alias)
    if provenance is None:
        whole = state.alias_tokens(_referenced_aliases(template))
        return values, tuple(whole for _ in values)
    # Lineage belongs to the exact path it was recorded for; a sibling or
    # nested path has none of its own.
    lineage = provenance.items if _lineage_alias(template) == alias else None
    if lineage is None or len(lineage) < len(values):
        if len({token for token in provenance.whole if token.slot in _CAPTURE_SLOTS}) > 1:
            raise _RunRefusal(
                ambiguous_code,
                "The fan-out's list was built from more than one Capture and keeps no "
                "per-element lineage, so no element's own evidence can be told apart.",
                node_id=node_id,
                details={"field": field, "reason": "lineage_unavailable"},
            )
        return values, tuple(provenance.whole for _ in values)
    shared = frozenset(token for token in provenance.whole if token.slot not in _CAPTURE_SLOTS)
    return values, tuple(shared | lineage[index] for index in range(len(values)))


def _inbox_items(
    node: InboxEgressNode,
    *,
    state: _RunState,
) -> tuple[list[CanonicalValue], tuple[frozenset[DependencyToken], ...]]:
    """What a post_inbox terminal posts: its input as one item, or an explicit fan-out.

    Only an input that is exactly ``{"items": ...}`` fans out, and then its
    items must resolve to a list (a typed refusal otherwise, posting nothing).
    Each element carries only its own evidence: a literal list's elements
    their own references, a produced list its recorded per-element lineage,
    and a list built from more than one Capture without that lineage refuses
    rather than lending every element every Capture. Any other input -- an
    array, an empty array, an object that happens to hold ``items`` -- is one
    item, exactly as it resolved.
    """

    declared = node.input
    if isinstance(declared, dict) and set(declared) == {"items"}:
        template = declared["items"]
        if not isinstance(template, list):
            return _fan_out_list(
                template,
                state=state,
                node_id=node.node_id,
                field="input.items",
                noun="inbox items",
                invalid_code="inbox_item_invalid",
                ambiguous_code="inbox_item_evidence_ambiguous",
            )
        values: list[CanonicalValue] = []
        for member in template:
            try:
                values.append(
                    normalize_canonical(
                        _resolve_template(
                            member, input_payload=state.input_payload, outputs=state.outputs
                        )
                    )
                )
            except ExecutionError as exc:
                raise _RunRefusal(
                    "inbox_item_invalid",
                    f"An inbox item did not resolve: {exc}",
                    node_id=node.node_id,
                    details={"field": "input.items", "reason": "items_unresolved"},
                ) from exc
        return values, tuple(
            _exact_evidence(member, state=state, node_id=node.node_id, field="input.items")
            for member in template
        )
    try:
        resolved = _resolve_template(
            declared, input_payload=state.input_payload, outputs=state.outputs
        )
    except ExecutionError as exc:
        raise _RunRefusal(
            "inbox_item_invalid",
            f"The inbox input did not resolve: {exc}",
            node_id=node.node_id,
            details={"field": "input", "reason": "input_unresolved"},
        ) from exc
    return [normalize_canonical(resolved)], (
        _exact_evidence(declared, state=state, node_id=node.node_id, field="input"),
    )


def _step_references(value: object) -> list[str]:
    """Every ``$steps.`` reference a template holds, wherever it sits."""

    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.startswith(_STEP_PREFIX):
                found.append(item)
        elif isinstance(item, list | tuple):
            for member in item:
                visit(member)
        elif isinstance(item, dict):
            for member in item.values():
                visit(member)

    visit(value)
    return found


def _exact_evidence(
    template: object, *, state: _RunState, node_id: str, field: str
) -> frozenset[DependencyToken]:
    """The closure of exactly what a template's own references select, never their siblings'.

    A reference keeps its alias's non-Capture dependencies. Its Captures are the
    ones its exact path reaches: the whole alias, all of it; the alias's
    ``items`` list with recorded per-element lineage, those elements'
    Captures; one element of that list (``items.<i>`` and below), that
    element's own. Any other path -- a sibling field, a nested list, an
    element without lineage -- is exact only when its alias was built from at
    most one Capture; otherwise the item's evidence cannot be told apart and
    it refuses ``inbox_item_evidence_ambiguous`` rather than borrowing.
    """

    tokens: frozenset[DependencyToken] = frozenset()
    for reference in _step_references(template):
        alias, *path = reference[len(_STEP_PREFIX) :].split(".")
        provenance = state.provenance.get(alias)
        if provenance is None:
            continue
        captures = frozenset(token for token in provenance.whole if token.slot in _CAPTURE_SLOTS)
        shared = provenance.whole - captures
        lineage = provenance.items
        if not path:
            selected = captures
        elif path == ["items"] and lineage is not None:
            selected = frozenset().union(*lineage) if lineage else frozenset()
        elif (
            len(path) >= 2
            and path[0] == "items"
            and path[1].isdigit()
            and lineage is not None
            and int(path[1]) < len(lineage)
        ):
            selected = lineage[int(path[1])]
        elif len(captures) <= 1:
            selected = captures
        else:
            raise _RunRefusal(
                "inbox_item_evidence_ambiguous",
                f"{reference!r} selects part of a value built from more than one Capture "
                "with no lineage for that part, so its own evidence cannot be told apart.",
                node_id=node_id,
                details={"field": field, "reason": "lineage_unavailable"},
            )
        tokens |= shared | selected
    return tokens


def _check_inbox_bytes(values: list[CanonicalValue], *, state: _RunState, node_id: str) -> int:
    """Refuse a post whose item or run total exceeds the inbox byte bounds; nothing is posted."""

    total = 0
    for index, value in enumerate(values):
        size = len(canonical_bytes(value))
        if size > INBOX_ITEM_MAX_BYTES:
            raise _RunRefusal(
                "inbox_item_over_budget",
                f"Inbox item {index} is {size} canonical bytes; one item may be at most "
                f"{INBOX_ITEM_MAX_BYTES}.",
                node_id=node_id,
                details={
                    "bound": "item",
                    "child_index": index,
                    "limit": INBOX_ITEM_MAX_BYTES,
                    "observed": size,
                },
            )
        total += size
    if state.inbox_bytes + total > INBOX_RUN_MAX_BYTES:
        raise _RunRefusal(
            "inbox_item_over_budget",
            f"This run would post {state.inbox_bytes + total} canonical bytes to the inbox; a "
            f"run may post at most {INBOX_RUN_MAX_BYTES}.",
            node_id=node_id,
            details={
                "bound": "run",
                "limit": INBOX_RUN_MAX_BYTES,
                "observed": state.inbox_bytes + total,
            },
        )
    return total


def _terminal_items(
    declared: object,
    *,
    state: _RunState,
) -> tuple[list[CanonicalValue], tuple[frozenset[DependencyToken], ...]]:
    """Fan one terminal node out into deterministic children with exact provenance."""

    resolved = _resolve_template(
        declared,
        input_payload=state.input_payload,
        outputs=state.outputs,
    )
    if isinstance(declared, list | tuple):
        values = list(resolved) if isinstance(resolved, list) else [resolved]
        tokens = tuple(
            state.alias_tokens(_referenced_aliases(member)) for member in declared[: len(values)]
        )
        return values, tokens + tuple(frozenset() for _ in range(len(values) - len(tokens)))
    if isinstance(declared, dict) and "items" in declared:
        template = declared["items"]
        shared = state.alias_tokens(
            _referenced_aliases({key: item for key, item in declared.items() if key != "items"})
        )
    else:
        template = declared
        shared = frozenset()
    source = resolved.get("items") if isinstance(resolved, dict) else resolved
    nested = source.get("items") if isinstance(source, dict) else None
    if isinstance(source, list):
        values = [normalize_canonical(item) for item in source]
    elif isinstance(nested, list):
        values = [normalize_canonical(item) for item in nested]
    else:
        return [resolved], (state.alias_tokens(_referenced_aliases(declared)),)
    if _template_alias(template) is None:
        fallback = shared | state.alias_tokens(_referenced_aliases(template))
        return values, tuple(fallback for _ in values)
    return values, tuple(
        shared | _item_closure(state, template, index) for index in range(len(values))
    )


def _decision_digest(run_id: str, decision: AcquisitionInputDecision) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-procedure-acquisition-decision-v1",
        {"decision": decision.model_dump(mode="json"), "run_id": run_id},
    ).tagged


def _resolve_path(value: object, path: tuple[str, ...], *, reference: str) -> object:
    current = value
    for member in path:
        if isinstance(current, dict) and member in current:
            current = current[member]
            continue
        if isinstance(current, list) and member.isdigit() and int(member) < len(current):
            current = current[int(member)]
            continue
        raise ExecutionError(f"runtime reference {reference!r} does not resolve")
    return current


def _resolve_reference(
    value: str,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    item: CanonicalValue | None = None,
) -> object:
    if value == "$input":
        return input_payload
    if value.startswith("$input."):
        return _resolve_path(input_payload, tuple(value[7:].split(".")), reference=value)
    if value.startswith("$steps."):
        parts = tuple(value[7:].split("."))
        alias = parts[0]
        if alias not in outputs:
            raise ExecutionError(f"runtime reference {value!r} names absent output")
        return _resolve_path(outputs[alias], parts[1:], reference=value)
    if value == "$item":
        if item is None:
            raise ExecutionError("$item is unavailable outside item transforms")
        return item
    if value.startswith("$item."):
        if item is None:
            raise ExecutionError("$item is unavailable outside item transforms")
        return _resolve_path(item, tuple(value[6:].split(".")), reference=value)
    return value


def _resolve_template(
    value: object,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    item: CanonicalValue | None = None,
    preserve_item_references: bool = False,
) -> CanonicalValue:
    if isinstance(value, str):
        if preserve_item_references and (value == "$item" or value.startswith("$item.")):
            return value
        return normalize_canonical(
            _resolve_reference(
                value,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
            )
        )
    if isinstance(value, list | tuple):
        return [
            _resolve_template(
                member,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
                preserve_item_references=preserve_item_references,
            )
            for member in value
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_template(
                member,
                input_payload=input_payload,
                outputs=outputs,
                item=item,
                preserve_item_references=preserve_item_references,
            )
            for key, member in value.items()
        }
    return normalize_canonical(value)


def _contains_item_reference(value: object) -> bool:
    if isinstance(value, str):
        return value == "$item" or value.startswith("$item.")
    if isinstance(value, list | tuple):
        return any(_contains_item_reference(member) for member in value)
    if isinstance(value, dict):
        return any(_contains_item_reference(member) for member in value.values())
    return False


def _resolve_transform_template(
    value: object,
    *,
    transform_kind: str,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
) -> CanonicalValue:
    """Resolve a transform spec while leaving only per-item field templates deferred."""

    if not isinstance(value, dict):
        return _resolve_template(
            value,
            input_payload=input_payload,
            outputs=outputs,
        )
    deferred_fields = transform_kind in {"shape_items", "join_items"}
    for key, member in value.items():
        if key == "fields" and deferred_fields:
            continue
        if _contains_item_reference(member):
            raise ExecutionError(
                f"$item is unavailable in {transform_kind}.{key} outside an item field template"
            )
    return {
        key: _resolve_template(
            member,
            input_payload=input_payload,
            outputs=outputs,
            preserve_item_references=key == "fields" and deferred_fields,
        )
        for key, member in value.items()
    }


def _declared_transform_spec(kind: str, spec: object) -> CanonicalValue:
    if not isinstance(spec, BaseModel):
        raise ExecutionError("typed transform spec is absent")
    payload = spec.model_dump(mode="json", exclude={"tag"})
    return normalize_canonical(payload)


_TRANSFORM_INPUT_REFUSAL_CODES: dict[str, ProcedureNodeRefusalCode] = {
    "shape_items": "shape_items_input_invalid",
    "filter_items": "filter_items_input_invalid",
    "dedupe_items": "dedupe_items_input_invalid",
    "join_items": "join_items_left_input_invalid",
    "aggregate_items": "aggregate_items_input_invalid",
}


def _resolve_node_template(
    value: object,
    *,
    node_id: str,
    transform_kind: str | None,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
) -> CanonicalValue:
    try:
        return (
            _resolve_template(
                value,
                input_payload=input_payload,
                outputs=outputs,
            )
            if transform_kind is None
            else _resolve_transform_template(
                value,
                transform_kind=transform_kind,
                input_payload=input_payload,
                outputs=outputs,
            )
        )
    except ExecutionError as exc:
        raise _RunRefusal(
            "runtime_reference_unresolved",
            "A Procedure runtime reference did not resolve.",
            node_id=node_id,
        ) from exc
    except Exception as exc:
        code: ProcedureNodeRefusalCode = (
            "result_not_canonical"
            if transform_kind is None
            else _TRANSFORM_INPUT_REFUSAL_CODES[transform_kind]
        )
        raise _RunRefusal(
            code,
            "A resolved Procedure value is not canonical for this node.",
            node_id=node_id,
        ) from exc


def _validate_node_contract(
    validator: ContractValidatorProtocol,
    *,
    contract: ArtifactPin,
    payload: CanonicalValue,
    direction: Literal["input", "output"],
    node_id: str,
    max_items: int | None = None,
    observe_items: Callable[[int, str, str], None] | None = None,
) -> CanonicalValue:
    boundary = f"contract-{'in' if direction == 'input' else 'out'}:{contract.target.name}"
    try:
        if max_items is not None:
            budget_result = validator.validate_contract_with_budget(
                contract=contract,
                payload=payload,
                direction=direction,
                max_items=max_items,
            )
            if not isinstance(budget_result, ValidatedProcedureContract):
                raise ExecutionError("Contract budget validator returned an invalid result")
            if observe_items is not None:
                for observation in budget_result.list_observations:
                    observe_items(observation.observed, boundary, observation.field_path)
            return normalize_canonical(budget_result.value)
        return normalize_canonical(
            validator.validate_contract(
                contract=contract,
                payload=payload,
                direction=direction,
            )
        )
    except ProcedureContractItemBudgetExceeded as exc:
        if observe_items is not None and exc.field_path is not None:
            observe_items(exc.observed, boundary, exc.field_path)
        raise _budget_refusal(
            budget_kind="max_items",
            limit=exc.limit,
            observed=exc.observed,
            node_id=node_id,
            boundary=boundary,
            field_path=exc.field_path or "",
        ) from exc
    except Exception as exc:
        code: ProcedureNodeRefusalCode = (
            "contract_input_refused" if direction == "input" else "contract_output_refused"
        )
        details: dict[str, object] = {
            "boundary": boundary,
        }
        field_path = getattr(exc, "field_path", None)
        element_index = getattr(exc, "element_index", None)
        if isinstance(field_path, str):
            details["field_path"] = field_path
        if isinstance(element_index, int):
            details["element_index"] = element_index
        raise _RunRefusal(
            code,
            f"The Procedure node {direction} contract refused its value: {exc}",
            node_id=node_id,
            details=details,
        ) from exc


def _apply_node_transform(
    kind: str,
    spec: CanonicalValue,
    *,
    max_items: int,
    node_id: str,
    list_boundary: tuple[str, str] | None = None,
) -> tuple[CanonicalValue, _ItemLineage | None]:
    # Kernel checks are only an early-abort optimization for an exact list-typed
    # output boundary. Opaque or ambiguous collections remain unmetered here and
    # are governed solely by the contract seams, result-byte cap, and wall clock.
    kernel_max_items = max_items if list_boundary is not None else None
    try:
        return _apply_transform(
            kind,
            spec,
            max_items=kernel_max_items,
            node_id=node_id,
            list_boundary=list_boundary,
        )
    except _RunRefusal:
        raise
    except _TransformInputInvalid as exc:
        code = _TRANSFORM_INPUT_REFUSAL_CODES[kind]
        if kind == "join_items" and exc.slot == "right_items":
            code = "join_items_right_input_invalid"
        raise _RunRefusal(
            code,
            f"The {kind} transform input is invalid.",
            node_id=node_id,
        ) from exc
    except ExecutionError as exc:
        raise _RunRefusal(
            _TRANSFORM_INPUT_REFUSAL_CODES[kind],
            f"The {kind} transform input is invalid.",
            node_id=node_id,
        ) from exc


# Retained admissions without resource_budget use the original 1 MiB return semantics.


def _item_count(value: CanonicalValue, *, extractable_only: bool = False) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return len(items)
    return None if extractable_only else 1


def _budget_refusal(
    *,
    budget_kind: Literal["max_items", "result_bytes"],
    limit: int,
    observed: int,
    node_id: str,
    boundary: str | None = None,
    field_path: str | None = None,
) -> _RunRefusal:
    if budget_kind == "max_items":
        return _RunRefusal(
            "budget_max_items_exceeded",
            "A Procedure collection exceeded its declared item bound.",
            node_id=node_id,
            details={
                "dimension": "max_items",
                "limit": limit,
                "observed": observed,
                "boundary": boundary,
                "field_path": field_path,
            },
        )
    return _RunRefusal(
        "budget_exhausted",
        f"Procedure {budget_kind} budget exhausted.",
        node_id=node_id,
        budget=ProcedureBudgetRefusalDetail(
            budget_kind=budget_kind,
            limit=limit,
            observed=observed,
        ),
    )


def _extract_items(
    value: object,
    *,
    label: str,
    max_items: int | None = None,
    node_id: str = "transform",
    list_boundary: tuple[str, str] | None = None,
) -> list[CanonicalValue]:
    if isinstance(value, list):
        if max_items is not None and list_boundary is not None and len(value) > max_items:
            raise _budget_refusal(
                budget_kind="max_items",
                limit=max_items,
                observed=len(value),
                node_id=node_id,
                boundary=None if list_boundary is None else list_boundary[0],
                field_path=None if list_boundary is None else list_boundary[1],
            )
        return [normalize_canonical(item) for item in value]
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        if max_items is not None and list_boundary is not None and len(value["items"]) > max_items:
            raise _budget_refusal(
                budget_kind="max_items",
                limit=max_items,
                observed=len(value["items"]),
                node_id=node_id,
                boundary=None if list_boundary is None else list_boundary[0],
                field_path=None if list_boundary is None else list_boundary[1],
            )
        return [normalize_canonical(item) for item in value["items"]]
    slot = "right_items" if label == "join_items right_items" else "left_items"
    raise _TransformInputInvalid(
        f"{label} requires a list or object with an items list",
        slot=slot,
    )


_ItemLineage = tuple[tuple[tuple[str, int], ...], ...]


def _apply_transform(
    kind: str,
    spec: CanonicalValue,
    *,
    max_items: int | None = None,
    node_id: str = "transform",
    list_boundary: tuple[str, str] | None = None,
) -> tuple[CanonicalValue, _ItemLineage | None]:
    """Apply one deterministic transform and report which input item fed each output."""

    if list_boundary is None:
        max_items = None
    if not isinstance(spec, dict):
        raise ExecutionError(f"transform {kind!r} requires an object spec")
    if kind == "shape_items":
        items = _extract_items(
            spec.get("items"),
            label=kind,
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        fields = spec.get("fields", {})
        if not isinstance(fields, dict):
            raise ExecutionError("shape_items fields must be an object")
        shaped: list[CanonicalValue] = []
        for item in items:
            base = (
                dict(item) if spec.get("include_input") is True and isinstance(item, dict) else {}
            )
            for name, template in fields.items():
                base[name] = _resolve_template(
                    template,
                    input_payload={},
                    outputs={},
                    item=item,
                )
            if max_items is not None and len(shaped) >= max_items:
                raise _budget_refusal(
                    budget_kind="max_items",
                    limit=max_items,
                    observed=len(shaped) + 1,
                    node_id=node_id,
                    boundary=None if list_boundary is None else list_boundary[0],
                    field_path=None if list_boundary is None else list_boundary[1],
                )
            shaped.append(normalize_canonical(base))
        return (
            {"items": shaped, "input_count": len(items), "output_count": len(shaped)},
            tuple((("items", index),) for index in range(len(shaped))),
        )
    if kind == "filter_items":
        items = _extract_items(
            spec.get("items"),
            label=kind,
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        where = spec.get("where", {})
        if not isinstance(where, dict):
            raise ExecutionError("filter_items where must be an object")
        kept_indices: list[int] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not all(
                item.get(key) == value for key, value in where.items()
            ):
                continue
            if max_items is not None and len(kept_indices) >= max_items:
                raise _budget_refusal(
                    budget_kind="max_items",
                    limit=max_items,
                    observed=len(kept_indices) + 1,
                    node_id=node_id,
                    boundary=None if list_boundary is None else list_boundary[0],
                    field_path=None if list_boundary is None else list_boundary[1],
                )
            kept_indices.append(index)
        kept = [items[index] for index in kept_indices]
        return (
            normalize_canonical(
                {"items": kept, "input_count": len(items), "output_count": len(kept)}
            ),
            tuple((("items", index),) for index in kept_indices),
        )
    if kind == "dedupe_items":
        items = _extract_items(
            spec.get("items"),
            label=kind,
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        keys = spec.get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise ExecutionError("dedupe_items keys must be a string list")
        key_names = [str(item) for item in keys]
        seen: set[bytes] = set()
        output: list[CanonicalValue] = []
        deduped_indices: list[int] = []
        for index, item in enumerate(items):
            identity = canonical_bytes(
                [item.get(key) for key in key_names] if isinstance(item, dict) else item
            )
            if identity in seen:
                continue
            if max_items is not None and len(output) >= max_items:
                raise _budget_refusal(
                    budget_kind="max_items",
                    limit=max_items,
                    observed=len(output) + 1,
                    node_id=node_id,
                    boundary=None if list_boundary is None else list_boundary[0],
                    field_path=None if list_boundary is None else list_boundary[1],
                )
            seen.add(identity)
            output.append(item)
            deduped_indices.append(index)
        return (
            normalize_canonical(
                {"items": output, "input_count": len(items), "output_count": len(output)}
            ),
            tuple((("items", index),) for index in deduped_indices),
        )
    if kind == "join_items":
        left = _extract_items(
            spec.get("left_items"),
            label="join_items left_items",
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        right = _extract_items(
            spec.get("right_items"),
            label="join_items right_items",
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        left_key = spec.get("left_key")
        right_key = spec.get("right_key")
        fields = spec.get("fields", {})
        if (
            not isinstance(left_key, str)
            or not isinstance(right_key, str)
            or not isinstance(fields, dict)
        ):
            raise ExecutionError("join_items keys and fields are malformed")
        joined_output: list[CanonicalValue] = []
        joined_lineage: list[tuple[tuple[str, int], ...]] = []
        for left_index, left_item in enumerate(left):
            for right_index, right_item in enumerate(right):
                if not isinstance(left_item, dict) or not isinstance(right_item, dict):
                    continue
                if left_item.get(left_key) != right_item.get(right_key):
                    continue
                if max_items is not None and len(joined_output) >= max_items:
                    raise _budget_refusal(
                        budget_kind="max_items",
                        limit=max_items,
                        observed=len(joined_output) + 1,
                        node_id=node_id,
                        boundary=None if list_boundary is None else list_boundary[0],
                        field_path=None if list_boundary is None else list_boundary[1],
                    )
                joined = {"left": left_item, "right": right_item}
                joined_output.append(
                    {
                        name: _resolve_template(
                            template,
                            input_payload={},
                            outputs={},
                            item=normalize_canonical(joined),
                        )
                        for name, template in fields.items()
                    }
                )
                joined_lineage.append((("left_items", left_index), ("right_items", right_index)))
        return (
            normalize_canonical({"items": joined_output, "output_count": len(joined_output)}),
            tuple(joined_lineage),
        )
    if kind == "aggregate_items":
        items = _extract_items(
            spec.get("items"),
            label=kind,
            max_items=None,
            node_id=node_id,
            list_boundary=list_boundary,
        )
        return normalize_canonical({"count": len(items)}), None
    raise ExecutionError(f"unsupported deterministic transform {kind!r}")


def _check_return_budget(
    value: CanonicalValue,
    *,
    max_items: int | None,
    node_id: str,
    result_bytes_cap: int = 1_048_576,
    observe_result_bytes: Callable[[int, str, str], None] | None = None,
    boundary: str = "procedure-return",
    field_path: str = "result",
) -> None:
    count = _item_count(value, extractable_only=True)
    if max_items is not None and count is not None and count > max_items:
        raise _budget_refusal(
            budget_kind="max_items",
            limit=max_items,
            observed=count,
            node_id=node_id,
        )
    size = len(canonical_bytes(value))
    if observe_result_bytes is not None:
        observe_result_bytes(size, boundary, field_path)
    if size > result_bytes_cap:
        raise _budget_refusal(
            budget_kind="result_bytes",
            limit=result_bytes_cap,
            observed=size,
            node_id=node_id,
        )


def _operand_value(
    operand: PredicateOperand,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    parameters: CanonicalValue,
) -> CanonicalValue:
    if operand.kind == "literal":
        return normalize_canonical(operand.value)
    if operand.kind == "parameter":
        if not isinstance(parameters, dict) or operand.parameter_name not in parameters:
            raise ExecutionError("guard parameter is absent")
        return normalize_canonical(parameters[operand.parameter_name])
    if operand.kind in {"input", "exists"} and operand.input_name is not None:
        if not isinstance(input_payload, dict) or operand.input_name not in input_payload:
            return False if operand.kind == "exists" else _missing_operand(operand.input_name)
        value = _resolve_path(
            input_payload[operand.input_name],
            operand.path,
            reference=operand.input_name,
        )
        return True if operand.kind == "exists" else normalize_canonical(value)
    if operand.alias is None or operand.alias not in outputs:
        return False if operand.kind == "exists" else _missing_operand(str(operand.alias))
    value = outputs[operand.alias]
    if operand.path:
        value = normalize_canonical(
            _resolve_path(value, operand.path, reference=f"$steps.{operand.alias}")
        )
    if operand.kind == "exists":
        return True
    if operand.kind == "count":
        return _item_count(value)
    if operand.kind == "truncated":
        return bool(isinstance(value, dict) and value.get("truncated") is True)
    return normalize_canonical(value)


def _missing_operand(name: str) -> CanonicalValue:
    raise ExecutionError(f"guard operand {name!r} is absent")


def _compare(left: CanonicalValue, operator: str, right: CanonicalValue) -> bool:
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if isinstance(left, bool) or isinstance(right, bool):
        raise ExecutionError("ordered guard operands cannot be booleans")
    if isinstance(left, int) and isinstance(right, int):
        comparable_left: int | str = left
        comparable_right: int | str = right
    elif isinstance(left, str) and isinstance(right, str):
        comparable_left = left
        comparable_right = right
    else:
        raise ExecutionError("ordered guard operands must share an int or string type")
    if operator in {"gt", "after"}:
        return comparable_left > comparable_right  # type: ignore[operator]
    if operator in {"gte", "on_or_after"}:
        return comparable_left >= comparable_right  # type: ignore[operator]
    if operator in {"lt", "before"}:
        return comparable_left < comparable_right  # type: ignore[operator]
    if operator in {"lte", "on_or_before"}:
        return comparable_left <= comparable_right  # type: ignore[operator]
    raise ExecutionError(f"unknown guard operator {operator!r}")


def _evaluate_predicate(
    predicate: GuardPredicate,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    parameters: CanonicalValue,
) -> tuple[bool, CanonicalValue]:
    if predicate.left is not None and predicate.right is not None and predicate.operator:
        left = _operand_value(
            predicate.left,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        right = _operand_value(
            predicate.right,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        verdict = _compare(left, predicate.operator, right)
        return verdict, normalize_canonical(
            {"left": left, "operator": predicate.operator, "right": right}
        )
    if predicate.not_of is not None:
        child, trace = _evaluate_predicate(
            predicate.not_of,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        return not child, {"not": trace}
    children = predicate.all_of if predicate.all_of is not None else predicate.any_of or ()
    results = [
        _evaluate_predicate(
            child,
            input_payload=input_payload,
            outputs=outputs,
            parameters=parameters,
        )
        for child in children
    ]
    verdicts = [item[0] for item in results]
    verdict = all(verdicts) if predicate.all_of is not None else any(verdicts)
    return verdict, normalize_canonical(
        {"all" if predicate.all_of is not None else "any": [item[1] for item in results]}
    )


__all__ = [
    "AcceptedStateRunInput",
    "AcceptedStateRunMaterial",
    "ContractValidatorProtocol",
    "ExhaustRunMaterialV1",
    "LandedCaptureRunMaterialV1",
    "PROCEDURE_RUN_ADMISSION_TAG",
    "PROCEDURE_RUN_RECEIPT_DOMAIN",
    "PreparedProcedureRun",
    "ProcedureActivationAuthorityProtocol",
    "ProcedureAdmissionBoundPayload",
    "ProcedureBoundaryRefused",
    "ProcedureClockProtocol",
    "ProcedureExecutor",
    "ProcedureNodePinSetV1",
    "ProcedureParentBindingV1",
    "ProcedureRunAdmission",
    "ProcedureRunReceiptV1",
    "ProcedureRunRefusalV1",
    "ProcedureRunResultV1",
    "ProcedureRunStatusV1",
    "ProviderInvocationResultV1",
    "StateTapReaderProtocol",
    "SystemProcedureClock",
    "accepted_procedure_pin_set_digest",
    "admission_fields",
    "bind_accepted_state_materials",
    "bind_line_admission_runtime_policy",
    "bind_prepared_investigation",
    "empty_admission_material_manifest",
    "parse_admission_payload",
    "procedure_direct_partition",
    "procedure_node_pin_sets",
    "procedure_semantic_run_id",
    "prepare_direct_procedure_run",
    "procedure_admission_digest",
    "procedure_pin_set_digest",
    "procedure_run_declared_budget",
    "procedure_run_receipt_digest",
    "procedure_semantic_replay_key_digest",
    "run_value_digest",
    "seal_procedure_run_admission",
]
