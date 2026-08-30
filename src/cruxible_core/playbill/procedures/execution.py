"""Authenticated graph-v3 admission and log-sufficient Procedure execution."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from typing import Callable, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.acquisition_policies import (
    AcquisitionInputDecisionV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CanonicalValue,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillExecutionError, PlaybillJournalError
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    procedure_artifact_digest,
)
from cruxible_client.contracts.procedures.contracts import (
    ProcedureContractItemBudgetExceeded,
    ValidatedProcedureContract,
)
from cruxible_client.contracts.procedures.graph import analyze_procedure_v3
from cruxible_client.contracts.procedures.models import (
    TERMINAL_REQUIRED_RUNGS,
    CaptureEgressNodeV3,
    ExhaustTapNodeV3,
    GuardNodeV3,
    GuardPredicateV1,
    HaltNodeV3,
    InboxEgressNodeV3,
    MandateSettlementNodeV3,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProjectNodeV3,
    ProposeChangeSetNodeV3,
    ProviderNodeV3,
    RepeatBodyNodeV3,
    RepeatNodeV3,
    SourceNodeV3,
    StateTapNodeV3,
    TransformNodeV3,
    iter_pin_bindings,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureBudgetBoundaryObservationV1,
    ProcedureBudgetRefusalDetailV1,
    ProcedureNodeRefusalCodeV1,
    ProcedureRunBudgetDeclaredV1,
    ProcedureRunBudgetObservedV1,
    ProcedureRunBudgetV1,
)
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, utc_now
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    JournalEventKindV1,
    JournalStreamIdentityV1,
    LocalJournalBackend,
    ProcedureJournalRecordDraftV1,
    StoredProcedureJournalRecordV1,
    journal_payload_bytes,
    parse_journal_payload,
)
from cruxible_core.playbill.procedures.acquisition import (
    ProcedureCaptureMaterialV1,
    ProcedureSourceAcquirerProtocol,
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
)
from cruxible_core.playbill.procedures.egress import (
    EffectiveRungV1,
    TerminalEgressError,
    TerminalEgressItemV1,
    TerminalEgressRequestV1,
    TerminalEgressSinkProtocol,
    effect_dispatch_refusal,
    effective_rung_digest,
    verify_terminal_egress_receipt,
)
from cruxible_core.playbill.procedures.input_planes import (
    AcceptedStateRunInputV1,
    AcceptedStateRunInputV2,
    ExhaustRunInputV1,
    LandedCaptureRunInputV1,
    ProcedureRunInputV1,
    merge_run_input_vector,
    run_input_digest,
    validate_node_input_plane,
    validate_run_input_vector,
)
from cruxible_core.playbill.procedures.run_index import ProcedureRunIndex
from cruxible_core.playbill.procedures.terminal_dependencies import (
    TAINT_ACCEPTED_STATE,
    TAINT_CONSERVATIVE_DEFAULT,
    TAINT_OMITTED_OPTIONAL,
    TAINT_UNPROMOTED_EXHAUST,
    AcquisitionInputOutcomeV1,
    AliasProvenanceV1,
    DependencyEvidenceFactsV1,
    DependencyToken,
    TerminalChildReceiptV1,
    accepted_state_token,
    admitted_capture_token,
    build_terminal_item_manifest,
    derive_terminal_item_facts,
    exhaust_token,
    policy_token,
    produced_capture_token,
    receipt_token,
    terminal_item_key,
    terminal_item_manifest_digest,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

ProcedureRunStatusV1 = Literal[
    "succeeded",
    "refused",
    "failed",
    "budget_exhausted",
    "halted",
]

PROCEDURE_SEMANTIC_REPLAY_KEY_DOMAIN = "playbill-procedure-semantic-replay-key-v1"
PROCEDURE_SEMANTIC_RESULT_DOMAIN = "playbill-procedure-semantic-result-v1"
PROCEDURE_ADMISSION_BINDING_V2_DOMAIN = "playbill-procedure-run-admission-v2"
PROCEDURE_RUN_ID_V2_DOMAIN = "playbill-procedure-run-id-v2"
PROCEDURE_RUN_RECEIPT_V2_DOMAIN = "playbill-procedure-run-receipt-v2"
PROCEDURE_RUN_RECEIPT_V3_DOMAIN = "playbill-procedure-run-receipt-v3"


class _StrictExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: str, *, label: str) -> str:
    try:
        Sha256Value.from_tagged(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be tagged lowercase SHA-256") from exc
    return value


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


class AcceptedStateRunMaterialV1(_StrictExecutionModel):
    tag: Literal["playbill-accepted-state-run-material-v1"] = (
        "playbill-accepted-state-run-material-v1"
    )
    input: AcceptedStateRunInputV1
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _digest(self) -> "AcceptedStateRunMaterialV1":
        if self.input.result_digest != run_value_digest("state-result", self.value):
            raise ValueError("accepted-state material does not reproduce its result digest")
        return self


class AcceptedStateRunMaterialV2(_StrictExecutionModel):
    tag: Literal["playbill-accepted-state-run-material-v2"] = (
        "playbill-accepted-state-run-material-v2"
    )
    input: AcceptedStateRunInputV2
    value: object

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _digest(self) -> "AcceptedStateRunMaterialV2":
        if self.input.result_digest != run_value_digest("state-result", self.value):
            raise ValueError("accepted-state material does not reproduce its result digest")
        return self


class ProcedureRunAdmissionV1(_StrictExecutionModel):
    """The complete run binding fixed before any result is visible.

    A direct actor invocation binds the accepted-state plane alone.  A Line run
    additionally binds the occurrence, deployment snapshot, acquisition policy,
    mandate and calibration coordinates, sensitivity policy, and epsilon
    membership, and may bind the landed-Capture and exhaust planes.
    """

    tag: Literal["playbill-procedure-run-admission-v1"] = "playbill-procedure-run-admission-v1"
    instance_id: str
    run_id: str
    attempt: int = Field(ge=1)
    accepted_coordinate: AcceptedCoordinate
    procedure_identity: ArtifactIdentity
    procedure_path: str
    procedure_artifact_digest: str
    definition_digest: str
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    full_pins: tuple[ArtifactPin, ...]
    node_pin_sets: tuple[ProcedureNodePinSetV1, ...]
    pin_set_digest: str
    invocation_input: object
    accepted_state_inputs: tuple[AcceptedStateRunInputV1, ...]
    landed_capture_inputs: tuple[LandedCaptureRunInputV1, ...] = ()
    exhaust_inputs: tuple[ExhaustRunInputV1, ...] = ()
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    actor_context: GovernedActorContext
    invocation_origin: Literal["actor", "line"] = "actor"
    journal_stream: JournalStreamIdentityV1
    journal_partition_id: str
    line_spec_digest: str | None = None
    occurrence_id: str | None = None
    deployment_snapshot_digest: str | None = None
    acquisition_policy_digest: str | None = None
    selection_receipt_digest: str | None = None
    sensitivity_policy_digest: str | None = None
    mandate_coordinate_digest: str | None = None
    calibration_coordinate_digest: str | None = None
    taint_labels: tuple[str, ...] = ()
    epsilon_member: bool = False
    admitted_at: datetime
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
        "sensitivity_policy_digest",
        "mandate_coordinate_digest",
        "calibration_coordinate_digest",
        "admission_binding_digest",
    )
    @classmethod
    def _digests(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _sha256(value, label=str(getattr(info, "field_name", "run digest")))

    @field_validator("admitted_at")
    @classmethod
    def _admitted_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("accepted_state_inputs")
    @classmethod
    def _state_inputs(
        cls, value: tuple[AcceptedStateRunInputV1, ...]
    ) -> tuple[AcceptedStateRunInputV1, ...]:
        names = tuple(item.input_name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("accepted-state run inputs must be sorted and unique")
        return value

    @field_validator("node_pin_sets")
    @classmethod
    def _node_pin_sets(
        cls, value: tuple[ProcedureNodePinSetV1, ...]
    ) -> tuple[ProcedureNodePinSetV1, ...]:
        node_ids = tuple(item.node_id for item in value)
        if node_ids != tuple(sorted(set(node_ids), key=lambda item: item.encode("utf-8"))):
            raise ValueError("Procedure node pin sets must be sorted and unique")
        return value

    @field_validator("taint_labels")
    @classmethod
    def _taint(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
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

    @model_validator(mode="after")
    def _admission_shape(self) -> "ProcedureRunAdmissionV1":
        if self.procedure_identity.kind != "Procedure":
            raise ValueError("Procedure run admission requires a Procedure identity")
        if self.journal_stream.instance_id != self.instance_id:
            raise ValueError("Procedure run and journal stream instance identities differ")
        validate_run_input_vector(
            self.run_inputs,
            expected_accepted=self.accepted_coordinate,
        )
        line_state = (
            self.line_spec_digest,
            self.occurrence_id,
            self.deployment_snapshot_digest,
            self.acquisition_policy_digest,
            self.mandate_coordinate_digest,
            self.calibration_coordinate_digest,
        )
        if self.invocation_origin == "actor":
            if any(value is not None for value in line_state) or self.epsilon_member:
                raise ValueError("PC-E1 direct admission cannot claim Line/deployment policy state")
            if (
                self.landed_capture_inputs
                or self.exhaust_inputs
                or self.selection_receipt_digest is not None
                or self.sensitivity_policy_digest is not None
                or self.taint_labels
            ):
                raise ValueError("direct admission binds only the accepted-state input plane")
        elif any(value is None for value in line_state):
            raise ValueError(
                "Line run admission must bind LineSpec, occurrence, deployment snapshot, "
                "acquisition policy, mandate, and calibration coordinates together"
            )
        elif self.sensitivity_policy_digest is None:
            raise ValueError("Line run admission must bind its sensitivity policy")
        expected_pin_digest = procedure_pin_set_digest(self.full_pins, self.node_pin_sets)
        if self.pin_set_digest != expected_pin_digest:
            raise ValueError("Procedure run pin_set_digest does not reproduce")
        expected = procedure_admission_digest(self)
        if self.admission_binding_digest != expected:
            raise ValueError("Procedure run admission_binding_digest does not reproduce")
        return self


class ProcedureRunAdmissionV2(ProcedureRunAdmissionV1):
    tag: Literal["playbill-procedure-run-admission-v2"] = "playbill-procedure-run-admission-v2"  # type: ignore[assignment]
    accepted_state_inputs: tuple[AcceptedStateRunInputV2, ...]  # type: ignore[assignment]
    bound_coordinate: AcceptedCoordinate
    head_at_admission: AcceptedCoordinate
    lane: Literal["current", "replay"]
    semantic_replay_key_digest: str

    @field_validator("semantic_replay_key_digest")
    @classmethod
    def _semantic_replay_key(cls, value: str) -> str:
        return _sha256(value, label="semantic_replay_key_digest")

    @model_validator(mode="after")
    def _v2_shape(self) -> "ProcedureRunAdmissionV2":
        if self.accepted_coordinate != self.bound_coordinate:
            raise ValueError("v2 admission bound and accepted coordinates differ")
        if self.semantic_replay_key_digest != procedure_semantic_replay_key_digest(self):
            raise ValueError("semantic replay key does not reproduce")
        return self


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


class PreparedProcedureRunV1(_StrictExecutionModel):
    tag: Literal["playbill-prepared-procedure-run-v1"] = "playbill-prepared-procedure-run-v1"
    admission: ProcedureRunAdmissionV1
    accepted_state_materials: tuple[AcceptedStateRunMaterialV1, ...]
    landed_capture_materials: tuple[LandedCaptureRunMaterialV1, ...] = ()
    exhaust_materials: tuple[ExhaustRunMaterialV1, ...] = ()
    acquisition_outcomes: tuple[AcquisitionInputOutcomeV1, ...] = ()

    @model_validator(mode="after")
    def _materials(self) -> "PreparedProcedureRunV1":
        if tuple(item.input for item in self.accepted_state_materials) != (
            self.admission.accepted_state_inputs
        ):
            raise ValueError("prepared run materials must exactly match admitted state inputs")
        if tuple(item.input for item in self.landed_capture_materials) != (
            self.admission.landed_capture_inputs
        ):
            raise ValueError("prepared run materials must exactly match admitted landed Captures")
        if tuple(item.input for item in self.exhaust_materials) != self.admission.exhaust_inputs:
            raise ValueError("prepared run materials must exactly match admitted exhaust inputs")
        names = tuple(item.input_name for item in self.acquisition_outcomes)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("prepared acquisition outcomes must be sorted and unique")
        return self


class PreparedProcedureRunV2(PreparedProcedureRunV1):
    tag: Literal["playbill-prepared-procedure-run-v2"] = "playbill-prepared-procedure-run-v2"  # type: ignore[assignment]
    admission: ProcedureRunAdmissionV2
    accepted_state_materials: tuple[AcceptedStateRunMaterialV2, ...]  # type: ignore[assignment]


class ProcedureAdmissionBoundPayloadV2(_StrictExecutionModel):
    tag: Literal["playbill-procedure-admission-bound-payload-v2"] = (
        "playbill-procedure-admission-bound-payload-v2"
    )
    admission: ProcedureRunAdmissionV2
    accepted_state_materials: tuple[AcceptedStateRunMaterialV2, ...]


class ProcedureRunRefusalV1(_StrictExecutionModel):
    tag: Literal["playbill-procedure-run-refusal-v1"] = "playbill-procedure-run-refusal-v1"
    code: str
    message: str
    node_id: str | None = None
    detail_code: str | None = None
    details: object = Field(default_factory=dict)
    budget: ProcedureBudgetRefusalDetailV1 | None = None

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
    effective_budgets: QueryBudgetsV1

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
    ) -> StateTapReadResultV1: ...


class ProviderExecutorProtocol(Protocol):
    def execute_provider(
        self,
        *,
        provider: ArtifactPin,
        environment: ArtifactPin,
        contract_in: ArtifactPin,
        contract_out: ArtifactPin,
        payload: CanonicalValue,
        actor_context: GovernedActorContext,
    ) -> ProviderInvocationResultV1: ...


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


def procedure_semantic_replay_key_digest(admission: ProcedureRunAdmissionV2) -> str:
    pins = [
        {
            "role": pin.role,
            "target": pin.target.model_dump(mode="json"),
            "artifact_digest": pin.artifact_digest,
        }
        for pin in admission.full_pins
    ]
    pins.sort(key=canonical_bytes)
    admitted_inputs = [
        {
            "input_name": item.input_name,
            "read_coordinate": item.read_coordinate.model_dump(mode="json"),
            "query_definition_digest": item.query_definition_digest,
            "parameters_digest": item.parameters_digest,
            "result_digest": item.result_digest,
            "effective_query_budgets": item.effective_query_budgets.model_dump(mode="json"),
        }
        for item in admission.accepted_state_inputs
    ]
    admitted_inputs.sort(key=lambda item: str(item["input_name"]).encode("utf-8"))
    return typed_digest(
        Sha256Value,
        PROCEDURE_SEMANTIC_REPLAY_KEY_DOMAIN,
        {
            "procedure_identity": admission.procedure_identity.model_dump(mode="json"),
            "procedure_artifact_digest": admission.procedure_artifact_digest,
            "invocation_input": admission.invocation_input,
            "bound_coordinate": admission.bound_coordinate.model_dump(mode="json"),
            "evaluation_time": format_datetime(admission.admitted_at),
            "validated_pins": pins,
            "admitted_inputs": admitted_inputs,
            "budget": admission.budget.model_dump(mode="json"),
            "hard_caps": admission.hard_caps.model_dump(mode="json"),
        },
    ).tagged


def procedure_admission_digest(admission: ProcedureRunAdmissionV1) -> str:
    if isinstance(admission, ProcedureRunAdmissionV2):
        return typed_digest(
            ArtifactDigest,
            PROCEDURE_ADMISSION_BINDING_V2_DOMAIN,
            {"semantic_replay_key_digest": admission.semantic_replay_key_digest},
        ).tagged
    payload = admission.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("admission_binding_digest")
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-run-admission-v1",
        {"admission": payload},
    ).tagged


def _exact_pin(
    binding: ArtifactPin | ProcedurePinSlotRefV1,
    *,
    label: str,
    slot_pins: Mapping[str, ArtifactPin] | None = None,
) -> ArtifactPin:
    """Resolve one binding to an exact pin, using the LineSpec closure when bound."""

    if isinstance(binding, ProcedurePinSlotRefV1):
        bound = None if slot_pins is None else slot_pins.get(binding.slot_name)
        if bound is None:
            raise PlaybillExecutionError(f"line_binding_required: {label} uses a LineSpec pin slot")
        return bound
    return binding


def _node_pin_sets(
    accepted: AcceptedProcedureV1,
    slot_pins: Mapping[str, ArtifactPin] | None = None,
) -> tuple[ProcedureNodePinSetV1, ...]:
    result: list[ProcedureNodePinSetV1] = []
    for node in accepted.procedure.definition.nodes:
        bindings = iter_pin_bindings(node)
        pins = tuple(
            _exact_pin(
                item,
                label=f"Procedure node {node.node_id!r}",
                slot_pins=slot_pins,
            )
            for item in bindings
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


def resolve_procedure_pin(
    binding: ArtifactPin | ProcedurePinSlotRefV1,
    *,
    label: str,
    slot_pins: Mapping[str, ArtifactPin] | None = None,
) -> ArtifactPin:
    """Public seam for resolving one v3 binding under an accepted LineSpec closure."""

    return _exact_pin(binding, label=label, slot_pins=slot_pins)


def procedure_node_pin_sets(
    accepted: AcceptedProcedureV1,
    slot_pins: Mapping[str, ArtifactPin] | None = None,
) -> tuple[ProcedureNodePinSetV1, ...]:
    """Public seam for the exact per-node pin commitment one run must reproduce."""

    return _node_pin_sets(accepted, slot_pins)


def accepted_procedure_pin_set_digest(accepted: AcceptedProcedureV1) -> str:
    """Reproduce the direct-runtime pin commitment for one accepted Procedure."""

    return procedure_pin_set_digest(accepted.procedure.pins, _node_pin_sets(accepted))


def prepare_direct_procedure_run(
    accepted: AcceptedProcedureV1,
    *,
    instance_id: str,
    run_id: str | None,
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
) -> PreparedProcedureRunV2:
    """Bind exact accepted state and all pins for an actor-authenticated direct run."""

    procedure = accepted.procedure
    if not procedure.directly_runnable:
        raise PlaybillExecutionError("line_binding_required: Procedure has unresolved pin slots")
    if procedure_artifact_digest(procedure).tagged != accepted.artifact_digest:
        raise PlaybillExecutionError("accepted Procedure artifact digest does not reproduce")

    materials: list[AcceptedStateRunMaterialV2] = []
    for node in procedure.definition.nodes:
        if not isinstance(node, StateTapNodeV3):
            continue
        query = _exact_pin(node.query, label=f"state_tap {node.node_id!r}")
        parameters = normalize_canonical(node.parameters)
        read = state_reader.read_accepted_state(
            query=query,
            parameters=parameters,
            coordinate=accepted_coordinate,
        )
        value = normalize_canonical(read.value)
        retained = bodies.store(canonical_bytes(value))
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
    materials.sort(key=lambda item: item.input.input_name.encode("utf-8"))

    node_pin_sets = _node_pin_sets(accepted)
    full_pins = procedure.pins
    pin_digest = procedure_pin_set_digest(full_pins, node_pin_sets)
    placeholder_run_id = run_id or "RUN-" + "0" * 64
    placeholder_partition = journal_partition_id or "direct:" + "0" * 64
    provisional = ProcedureRunAdmissionV2.model_construct(
        _fields_set=None,
        instance_id=instance_id,
        run_id=placeholder_run_id,
        attempt=attempt,
        accepted_coordinate=accepted_coordinate,
        procedure_identity=procedure.identity,
        procedure_path=accepted.path,
        procedure_artifact_digest=accepted.artifact_digest,
        definition_digest=procedure.definition_digest,
        activation_policy=procedure.activation_policy,
        full_pins=full_pins,
        node_pin_sets=node_pin_sets,
        pin_set_digest=pin_digest,
        invocation_input=invocation_input,
        accepted_state_inputs=tuple(item.input for item in materials),
        budget=procedure.definition.budget,
        hard_caps=procedure.definition.hard_caps,
        actor_context=actor_context,
        invocation_origin="actor",
        journal_stream=journal_stream,
        journal_partition_id=placeholder_partition,
        line_spec_digest=None,
        occurrence_id=None,
        deployment_snapshot_digest=None,
        acquisition_policy_digest=None,
        mandate_coordinate_digest=None,
        calibration_coordinate_digest=None,
        epsilon_member=False,
        admitted_at=ensure_utc(admitted_at),
        admission_binding_digest="sha256:" + "0" * 64,
        bound_coordinate=accepted_coordinate,
        head_at_admission=head_at_admission or accepted_coordinate,
        lane=lane,
        semantic_replay_key_digest="sha256:" + "0" * 64,
    )
    replay_key = procedure_semantic_replay_key_digest(provisional)
    semantic_run_id = "RUN-" + typed_digest(
        Sha256Value,
        PROCEDURE_RUN_ID_V2_DOMAIN,
        {"semantic_replay_key_digest": replay_key},
    ).tagged.removeprefix("sha256:")
    semantic_partition = "direct:" + replay_key.removeprefix("sha256:")
    provisional = provisional.model_copy(
        update={
            "run_id": semantic_run_id,
            "journal_partition_id": journal_partition_id or semantic_partition,
            "semantic_replay_key_digest": replay_key,
        }
    )
    admission = ProcedureRunAdmissionV2(
        instance_id=instance_id,
        run_id=semantic_run_id,
        attempt=attempt,
        accepted_coordinate=accepted_coordinate,
        procedure_identity=procedure.identity,
        procedure_path=accepted.path,
        procedure_artifact_digest=accepted.artifact_digest,
        definition_digest=procedure.definition_digest,
        activation_policy=procedure.activation_policy,
        full_pins=full_pins,
        node_pin_sets=node_pin_sets,
        pin_set_digest=pin_digest,
        invocation_input=invocation_input,
        accepted_state_inputs=tuple(item.input for item in materials),
        budget=procedure.definition.budget,
        hard_caps=procedure.definition.hard_caps,
        actor_context=actor_context,
        invocation_origin="actor",
        journal_stream=journal_stream,
        journal_partition_id=journal_partition_id or semantic_partition,
        admitted_at=ensure_utc(admitted_at),
        bound_coordinate=accepted_coordinate,
        head_at_admission=head_at_admission or accepted_coordinate,
        lane=lane,
        semantic_replay_key_digest=replay_key,
        admission_binding_digest=procedure_admission_digest(provisional),
    )
    return PreparedProcedureRunV2(
        admission=admission,
        accepted_state_materials=tuple(materials),
    )


class _RunRefusal(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        detail_code: str | None = None,
        details: object | None = None,
        budget: ProcedureBudgetRefusalDetailV1 | None = None,
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
        code: Literal["cas_unavailable_at_replay", "replay_material_mismatch"],
    ) -> None:
        super().__init__(code)
        self.code = code


class _Halted(Exception):
    def __init__(self, *, node_id: str, reason: str | None) -> None:
        super().__init__("Procedure halted")
        self.node_id = node_id
        self.reason = reason


class _TransformInputInvalid(PlaybillExecutionError):
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
    provider_calls: int = 0
    capture_bytes: int = 0
    max_items_high_water: int = 0
    max_items_boundary: str | None = None
    max_items_field_path: str | None = None
    result_bytes_high_water: int = 0
    result_bytes_boundary: str | None = None
    result_bytes_field_path: str | None = None
    wall_clock_microseconds: int = 0

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


def _effective_max_items(admission: ProcedureRunAdmissionV1) -> int:
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
        provider_executor: ProviderExecutorProtocol | None = None,
        source_acquirer: ProcedureSourceAcquirerProtocol | None = None,
        acquisition_policy: SourceAcquisitionPolicyV1 | None = None,
        default_authorizations: tuple[str, ...] = (),
        slot_pins: Mapping[str, ArtifactPin] | None = None,
        effective_rung: EffectiveRungV1 | None = None,
        egress_sink: TerminalEgressSinkProtocol | None = None,
        declared_effect_grants: tuple[str, ...] = (),
        clock: ProcedureClockProtocol | None = None,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.run_index = run_index
        self.fencing_token = fencing_token
        self.activation_authority = activation_authority
        self.contract_validator = contract_validator
        self.provider_executor = provider_executor
        self.source_acquirer = source_acquirer
        self.acquisition_policy = acquisition_policy
        self.default_authorizations = default_authorizations
        self.slot_pins: Mapping[str, ArtifactPin] = slot_pins or {}
        self.effective_rung = effective_rung
        self.egress_sink = egress_sink
        self.declared_effect_grants = declared_effect_grants
        self.clock = clock or SystemProcedureClock()

    def _pin(
        self,
        binding: ArtifactPin | ProcedurePinSlotRefV1,
        *,
        label: str,
    ) -> ArtifactPin:
        """Resolve one node binding through this run's accepted LineSpec closure."""

        return _exact_pin(binding, label=label, slot_pins=self.slot_pins)

    def execute(
        self,
        prepared: PreparedProcedureRunV1,
        accepted: AcceptedProcedureV1,
    ) -> ProcedureRunResultV1:
        admission = prepared.admission
        self._verify_correspondence(admission, accepted)
        existing_records = self.journal.all_records(
            admission.journal_stream,
            admission.journal_partition_id,
        )
        self.run_index.rebuild(existing_records, bodies=self.bodies)
        indexed = self.run_index.get(admission.run_id)
        if indexed is not None:
            if indexed.admission_binding_digest != admission.admission_binding_digest:
                raise PlaybillExecutionError("run_id collides across distinct admission bindings")
            if indexed.status != "running":
                return self._replay_completed(admission, existing_records)
            effect_state = (
                " with an unresolved durable effect intent"
                if indexed.effect_intent_count != indexed.effect_result_count
                else ""
            )
            raise PlaybillExecutionError(
                "run_recovery_required: admitted run has incomplete exhaust" + effect_state
            )
        self._require_current(admission)
        records: list[StoredProcedureJournalRecordV1] = []
        started_ns = self.clock.monotonic_ns()

        self._append_event(
            admission,
            records,
            "attempt_started",
            {"attempt": admission.attempt, "admitted_at": admission.admitted_at.isoformat()},
        )
        self._append_event(
            admission,
            records,
            "admission_bound",
            (
                ProcedureAdmissionBoundPayloadV2(
                    admission=admission,
                    accepted_state_materials=prepared.accepted_state_materials,
                ).model_dump(mode="json")
                if isinstance(admission, ProcedureRunAdmissionV2)
                and isinstance(prepared, PreparedProcedureRunV2)
                else admission.model_dump(mode="json")
            ),
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
        halt: CanonicalValue | None = None
        try:
            state = self._seed_state(prepared)
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
        except _RunRefusal as exc:
            status = "refused"
            refusal = exc.refusal
        except _Halted as exc:
            status = "halted"
            halt = normalize_canonical(
                {
                    "node_id": exc.node_id,
                    "reason": exc.reason,
                }
            )
        except _BudgetExceeded as exc:
            if isinstance(admission, ProcedureRunAdmissionV2) and exc.budget_kind == "wall_clock":
                status = "failed"
                failure_code = "wall_clock_exhausted"
                failure_message = "Procedure execution exceeded its operational budget."
            else:
                status = (
                    "refused"
                    if isinstance(admission, ProcedureRunAdmissionV2)
                    else "budget_exhausted"
                )
                failure_message = "Procedure execution exhausted its declared budget."
                refusal = ProcedureRunRefusalV1(
                    code="budget_exhausted",
                    message=failure_message,
                    node_id=exc.node_id,
                    budget=ProcedureBudgetRefusalDetailV1(
                        budget_kind=exc.budget_kind,
                        limit=exc.limit,
                        observed=exc.observed,
                    ),
                )
        except _OperationalFailure as exc:
            status = "failed"
            failure_code = exc.code
            failure_message = "Procedure execution failed."
        except PlaybillExecutionError:
            status = "failed"
            failure_code = "unexpected_exception"
            failure_message = "Procedure execution failed."
        except Exception as exc:
            status = "failed"
            failure_message = f"{type(exc).__name__}: {exc}"
            failure_code = "unexpected_exception"

        semantic_result_digest = None
        if isinstance(admission, ProcedureRunAdmissionV2) and status in {
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
            "output": output,
            "refusal": None if refusal is None else refusal.model_dump(mode="json"),
            "failure": failure_message,
            "failure_code": failure_code,
            "halt": halt,
            "semantic_result_digest": semantic_result_digest,
            "provider_calls": state.provider_calls,
            "capture_bytes": state.capture_bytes,
            "budget": ProcedureRunBudgetV1(
                declared=ProcedureRunBudgetDeclaredV1(
                    budget=admission.budget,
                    hard_caps=admission.hard_caps,
                ),
                observed=ProcedureRunBudgetObservedV1(
                    max_items=ProcedureBudgetBoundaryObservationV1(
                        high_water=state.max_items_high_water,
                        boundary=state.max_items_boundary,
                        field_path=state.max_items_field_path,
                    ),
                    result_bytes=ProcedureBudgetBoundaryObservationV1(
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

    def _seed_state(self, prepared: PreparedProcedureRunV1) -> _RunState:
        """Bind every admitted plane's material and its provenance before node one."""

        admission = prepared.admission
        state = _RunState(
            outputs={},
            input_payload=normalize_canonical(admission.invocation_input),
            parameters={},
            outcomes={item.input_name: item for item in prepared.acquisition_outcomes},
        )
        for accepted_state in prepared.accepted_state_materials:
            if isinstance(accepted_state, AcceptedStateRunMaterialV2):
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
            else:
                value = accepted_state.value
            digest = run_input_digest(accepted_state.input)
            name = accepted_state.input.input_name
            state.outputs[name] = normalize_canonical(value)
            state.provenance[name] = AliasProvenanceV1(
                whole=frozenset(
                    {
                        accepted_state_token(digest),
                        policy_token(accepted_state.input.query_definition_digest),
                    }
                )
            )
            state.facts[digest] = DependencyEvidenceFactsV1(
                epistemic_grade="observed",
                provenance_grade="self-asserted",
                taint_labels=(TAINT_ACCEPTED_STATE,),
            )
        for landed in prepared.landed_capture_materials:
            name = landed.input.input_name
            state.outputs[name] = normalize_canonical(landed.material.value)
            tokens = {
                admitted_capture_token(landed.input.capture_digest),
                policy_token(landed.input.capture_contract_digest),
            }
            if admission.acquisition_policy_digest is not None:
                tokens.add(policy_token(admission.acquisition_policy_digest))
            if admission.selection_receipt_digest is not None:
                tokens.add(receipt_token(admission.selection_receipt_digest))
            tokens.add(receipt_token(landed.material.envelope.run_receipt_digest))
            state.provenance[name] = AliasProvenanceV1(whole=frozenset(tokens))
            state.facts[landed.input.capture_digest] = DependencyEvidenceFactsV1(
                epistemic_grade=landed.material.epistemic_grade,
                provenance_grade=landed.material.provenance_grade,
                acquisition_input_name=name,
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
        admission: ProcedureRunAdmissionV1,
        accepted: AcceptedProcedureV1,
    ) -> None:
        procedure = accepted.procedure
        if (
            accepted.path != admission.procedure_path
            or accepted.artifact_digest != admission.procedure_artifact_digest
            or procedure.definition_digest != admission.definition_digest
            or procedure.identity != admission.procedure_identity
            or procedure.activation_policy != admission.activation_policy
            or procedure.definition.hard_caps != admission.hard_caps
        ):
            raise PlaybillExecutionError("Procedure admission and accepted artifact differ")
        if admission.invocation_origin == "actor":
            if procedure.pins != admission.full_pins:
                raise PlaybillExecutionError("Procedure admission and accepted artifact differ")
        elif not set(procedure.pins).issubset(set(admission.full_pins)) or not set(
            self.slot_pins.values()
        ).issubset(set(admission.full_pins)):
            raise PlaybillExecutionError(
                "Line run pins must close the accepted Procedure pins exactly"
            )
        if admission.invocation_origin == "actor":
            if procedure.definition.budget != admission.budget:
                raise PlaybillExecutionError("Procedure admission and accepted artifact differ")
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
                raise PlaybillExecutionError("Line run budget exceeds the Procedure hard caps")
        if _node_pin_sets(accepted, self.slot_pins) != admission.node_pin_sets:
            raise PlaybillExecutionError("Procedure node pins changed after admission")
        self._verify_effective_rung(admission)
        self._verify_input_planes(admission, accepted)
        expected_state_inputs = {
            node.as_: (
                self._pin(node.query, label=f"state_tap {node.node_id!r}"),
                run_value_digest("state-parameters", normalize_canonical(node.parameters)),
            )
            for node in procedure.definition.nodes
            if isinstance(node, StateTapNodeV3)
        }
        actual_state_inputs = {item.input_name: item for item in admission.accepted_state_inputs}
        if set(actual_state_inputs) != set(expected_state_inputs):
            raise PlaybillExecutionError("Procedure admission state_tap input set differs")
        for name, (query, parameters_digest) in expected_state_inputs.items():
            actual = actual_state_inputs[name]
            if (
                actual.query_definition_digest != query.artifact_digest
                or actual.parameters_digest != parameters_digest
                or actual.read_coordinate != admission.accepted_coordinate
            ):
                raise PlaybillExecutionError(
                    f"Procedure admission state_tap input {name!r} differs"
                )

    def _verify_effective_rung(self, admission: ProcedureRunAdmissionV1) -> None:
        """Refuse a five-term cap computed against any other admission binding.

        The rung is authority, so it is checked against the frozen tuple rather
        than recomputed here: every term already resolved through the mandate,
        calibration, and sensitivity coordinates this admission bound.
        """

        rung = self.effective_rung
        if rung is None:
            return
        if admission.invocation_origin != "line":
            raise PlaybillExecutionError(
                "an effective rung binds a Line run, never a direct actor invocation"
            )
        if (
            rung.procedure_definition_digest != admission.definition_digest
            or rung.line_spec_digest != admission.line_spec_digest
            or rung.sensitivity_policy_digest != admission.sensitivity_policy_digest
            or rung.mandate_coordinate_digest != admission.mandate_coordinate_digest
            or rung.calibration_coordinate_digest != admission.calibration_coordinate_digest
        ):
            raise PlaybillExecutionError(
                "effective rung was computed against another admission binding"
            )

    def _verify_input_planes(
        self,
        admission: ProcedureRunAdmissionV1,
        accepted: AcceptedProcedureV1,
    ) -> None:
        """Refuse every cross-plane relabel before the first node can observe a value."""

        nodes = {
            node.as_: node
            for node in accepted.procedure.definition.nodes
            if isinstance(node, StateTapNodeV3 | SourceNodeV3 | ExhaustTapNodeV3)
        }
        for run_input in admission.run_inputs:
            node = nodes.get(run_input.input_name)
            if node is None:
                raise PlaybillExecutionError(
                    f"admitted input {run_input.input_name!r} names no v3 input node"
                )
            try:
                validate_node_input_plane(node, run_input)
            except ValueError as exc:
                raise PlaybillExecutionError(f"input_plane_relabelled: {exc}") from exc
        expected_exhaust = {
            node.as_
            for node in accepted.procedure.definition.nodes
            if isinstance(node, ExhaustTapNodeV3)
        }
        if {item.input_name for item in admission.exhaust_inputs} != expected_exhaust and (
            admission.invocation_origin == "line"
        ):
            raise PlaybillExecutionError("Line admission exhaust_tap input set differs")
        for exhaust in admission.exhaust_inputs:
            node = nodes[exhaust.input_name]
            if not isinstance(node, ExhaustTapNodeV3):  # pragma: no cover - plane law covers it
                raise PlaybillExecutionError("exhaust input names a non-exhaust node")
            reducer = self._pin(
                node.reducer_or_query,
                label=f"exhaust_tap {node.node_id!r}",
            )
            if (
                exhaust.reducer_or_query_digest != reducer.artifact_digest
                or exhaust.journal_identity != node.journal_identity
            ):
                raise PlaybillExecutionError(
                    f"Procedure admission exhaust input {exhaust.input_name!r} differs"
                )

    def _require_current(self, admission: ProcedureRunAdmissionV1) -> None:
        if isinstance(admission, ProcedureRunAdmissionV2) and admission.lane == "replay":
            return
        current = self.activation_authority.current_procedure_digest(
            admission.procedure_identity,
            coordinate=admission.accepted_coordinate,
        )
        if current != admission.procedure_artifact_digest:
            raise PlaybillExecutionError("Procedure is not current at the admitted coordinate")

    def _checkpoint_current(self, admission: ProcedureRunAdmissionV1, *, effect: bool) -> None:
        if admission.activation_policy == "abort" or (
            admission.activation_policy == "epoch-check" and effect
        ):
            self._require_current(admission)

    def _check_budget(
        self,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        *,
        started_ns: int,
        node_id: str,
    ) -> None:
        elapsed_us = (self.clock.monotonic_ns() - started_ns) // 1000
        state.wall_clock_microseconds = max(state.wall_clock_microseconds, elapsed_us)
        if elapsed_us > admission.budget.wall_clock.microseconds:
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
        accepted: AcceptedProcedureV1,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> CanonicalValue:
        definition = accepted.procedure.definition
        graph = analyze_procedure_v3(definition)
        nodes = {node.node_id: node for node in definition.nodes}
        guard_halt_targets = frozenset(
            target
            for candidate in definition.nodes
            if isinstance(candidate, GuardNodeV3)
            for target in (candidate.on_true, candidate.on_false)
            if target is not None and target != "$abort" and isinstance(nodes[target], HaltNodeV3)
        )
        current = definition.nodes[0].node_id
        while True:
            node = nodes[current]
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
                )
                self._append_event(
                    admission,
                    records,
                    "node_fired",
                    {"node_id": node.node_id, "kind": node.kind, "verdict": "succeeded"},
                )
                if isinstance(node, HaltNodeV3):
                    raise _Halted(node_id=node.node_id, reason=node.reason)
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
            if isinstance(node, GuardNodeV3):
                target = edges[branch or "on_false"]
            else:
                target = edges.get("next")
                if (
                    target in guard_halt_targets
                    and graph.produced_alias[node.node_id] == definition.returns
                    and getattr(node, "next", None) is None
                ):
                    # A trailing Halt used as one guard arm is a sibling terminal,
                    # not an implicit continuation after the other arm returns.
                    target = None
            if target == "$abort":
                if isinstance(node, GuardNodeV3):
                    raise _RunRefusal(
                        "guard_refused",
                        node.message,
                        node_id=node.node_id,
                        detail_code=node.refusal_code,
                    )
                raise _RunRefusal(
                    "guard_refused",
                    "Procedure reached an explicit abort edge.",
                    node_id=node.node_id,
                    detail_code="procedure.abort",
                )
            if target is None:
                try:
                    result = state.outputs[definition.returns]
                except KeyError as exc:  # pragma: no cover - static law should prevent
                    raise PlaybillExecutionError("Procedure return alias was not produced") from exc
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
                    observe_result_bytes=state.observe_result_bytes,
                    boundary="procedure-return",
                    field_path=definition.returns,
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
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> Literal["on_true", "on_false"] | None:
        if isinstance(node, StateTapNodeV3):
            if node.as_ not in state.outputs:
                raise PlaybillExecutionError("admitted state_tap material is absent")
            self._extend_alias(state, node.as_, _node_policy_tokens(node) | state.control)
            return None
        if isinstance(node, SourceNodeV3):
            self._run_source(node, admission=admission, state=state, records=records)
            return None
        if isinstance(node, ExhaustTapNodeV3):
            if node.as_ not in state.outputs:
                raise _RunRefusal(
                    "line_binding_required",
                    "An exhaust_tap input is admitted only under a Line binding.",
                    node_id=node.node_id,
                )
            self._extend_alias(state, node.as_, _node_policy_tokens(node) | state.control)
            return None
        if isinstance(node, HaltNodeV3):
            return None
        if isinstance(node, ProviderNodeV3):
            self._run_provider(
                node,
                admission=admission,
                state=state,
                records=records,
            )
            return None
        if isinstance(node, TransformNodeV3):
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
                base=_base_tokens(node, state, declared_spec),
            )
            return None
        if isinstance(node, ProjectNodeV3):
            value = _resolve_node_template(
                node.fields,
                node_id=node.node_id,
                transform_kind=None,
                input_payload=state.input_payload,
                outputs=state.outputs,
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
            state.provenance[node.as_] = _projected_provenance(
                node.fields,
                state=state,
                base=_base_tokens(node, state, node.fields),
                value=state.outputs[node.as_],
            )
            return None
        if isinstance(node, GuardNodeV3):
            try:
                verdict, trace = _evaluate_predicate(
                    node.predicate,
                    input_payload=state.input_payload,
                    outputs=state.outputs,
                    parameters=state.parameters,
                )
            except PlaybillExecutionError as exc:
                raise _RunRefusal(
                    "runtime_reference_unresolved",
                    "A guard runtime reference did not resolve.",
                    node_id=node.node_id,
                ) from exc
            state.control = (
                state.control
                | _node_policy_tokens(node)
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
        if isinstance(node, RepeatNodeV3):
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
            CaptureEgressNodeV3
            | InboxEgressNodeV3
            | ProposeChangeSetNodeV3
            | MandateSettlementNodeV3,
        ):
            self._run_terminal(node, admission=admission, state=state, records=records)
            return None
        raise PlaybillExecutionError(f"unsupported graph-v3 node {type(node).__name__}")

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

    def _run_source(
        self,
        node: SourceNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        """Consume an admitted landed Capture, or really acquire one now."""

        if node.as_ in state.outputs:
            self._extend_alias(state, node.as_, _node_policy_tokens(node) | state.control)
            return
        if admission.invocation_origin != "line":
            raise _RunRefusal(
                "line_binding_required",
                "Source acquisition requires a Line binding and an acquisition policy.",
                node_id=node.node_id,
            )
        rule = self._acquisition_rule(node.as_)
        if self.source_acquirer is None or rule is None:
            raise _RunRefusal(
                "source_acquisition_unavailable",
                "No acquirer or declared acquisition rule serves this source node.",
                node_id=node.node_id,
            )
        capture_contract = self._pin(node.capture_contract, label=f"source {node.node_id!r}")
        provider = self._pin(node.provider, label=f"source {node.node_id!r} provider")
        request = _resolve_template(
            node.request,
            input_payload=state.input_payload,
            outputs=state.outputs,
        )
        result = self.source_acquirer.acquire(
            node_id=node.node_id,
            input_name=node.as_,
            capture_contract=capture_contract,
            provider=provider,
            request=request,
            run_id=admission.run_id,
            bound_generation=admission.accepted_coordinate.generation_root,
            observed_at=self.clock.now(),
        )
        decision = apply_acquisition_result(
            rule,
            result,
            default_authorized=rule.input_name in self.default_authorizations,
        )
        self._append_event(
            admission,
            records,
            "source_acquisition",
            {
                "tag": "playbill-procedure-source-acquisition-v1",
                "node_id": node.node_id,
                "input_name": node.as_,
                "result": result.model_dump(mode="json", exclude={"acquisition"}),
                "decision": decision.model_dump(mode="json"),
                "audit": {
                    "deployment_digest": admission.deployment_snapshot_digest,
                    "recorded_at": format_datetime(self.clock.now()),
                },
            },
        )
        self._bind_acquisition(
            node,
            admission=admission,
            state=state,
            records=records,
            rule=rule,
            result=result,
            decision=decision,
        )

    def _acquisition_rule(self, input_name: str) -> InputAcquisitionRuleV1 | None:
        if self.acquisition_policy is None:
            return None
        for rule in self.acquisition_policy.inputs:
            if rule.input_name == input_name:
                return rule
        return None

    def _bind_acquisition(
        self,
        node: SourceNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        rule: InputAcquisitionRuleV1,
        result: ProcedureSourceAcquisitionResultV1,
        decision: AcquisitionInputDecisionV1,
    ) -> None:
        base = _node_policy_tokens(node) | state.control
        if admission.acquisition_policy_digest is not None:
            base = base | {policy_token(admission.acquisition_policy_digest)}
        if decision.disposition == "refused":
            state.outcomes[rule.input_name] = AcquisitionInputOutcomeV1(
                input_name=rule.input_name,
                disposition="refused",
            )
            raise _RunRefusal(
                cast(
                    ProcedureNodeRefusalCodeV1,
                    decision.reason_codes[0]
                    if decision.reason_codes
                    else "playbill.acquisition.refused",
                ),
                "The declared acquisition rule refuses this typed source result.",
                node_id=node.node_id,
            )
        if decision.disposition == "omitted":
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
            return
        if decision.disposition == "defaulted":
            marker = _decision_digest(admission.run_id, decision)
            state.outputs[node.as_] = normalize_canonical(decision.default_value)
            state.facts[marker] = DependencyEvidenceFactsV1(
                epistemic_grade="predicted",
                provenance_grade="self-asserted",
                taint_labels=(TAINT_CONSERVATIVE_DEFAULT,),
                acquisition_input_name=rule.input_name,
            )
            state.outcomes[rule.input_name] = AcquisitionInputOutcomeV1(
                input_name=rule.input_name,
                disposition="defaulted",
            )
            state.provenance[node.as_] = AliasProvenanceV1(whole=base | {receipt_token(marker)})
            return
        acquisition = result.acquisition
        if acquisition is None:  # pragma: no cover - typed-result invariant
            raise PlaybillExecutionError("selected acquisition carries no Capture")
        material = acquisition.canonical_material
        if material is None:
            raise _RunRefusal(
                "source_material_unavailable",
                "This acquisition mode produced no canonical material for the run.",
                node_id=node.node_id,
            )
        state.capture_bytes += len(canonical_bytes(normalize_canonical(material)))
        state.outputs[node.as_] = normalize_canonical(material)
        state.facts[acquisition.capture_digest] = DependencyEvidenceFactsV1(
            epistemic_grade=acquisition.epistemic_grade,
            provenance_grade=acquisition.provenance_grade,
            acquisition_input_name=rule.input_name,
        )
        state.outcomes[rule.input_name] = AcquisitionInputOutcomeV1(
            input_name=rule.input_name,
            disposition="acquired",
            capture_digests=(acquisition.capture_digest,),
        )
        state.provenance[node.as_] = AliasProvenanceV1(
            whole=base
            | {
                produced_capture_token(acquisition.capture_digest),
                receipt_token(acquisition.receipt.digest),
                policy_token(acquisition.envelope.capture_contract_digest),
            }
        )
        self._append_event(
            admission,
            records,
            "produced_capture",
            {
                "tag": "playbill-procedure-produced-capture-v1",
                "node_id": node.node_id,
                "input_name": rule.input_name,
                "capture_digest": acquisition.capture_digest,
                "capture_contract_digest": acquisition.envelope.capture_contract_digest,
                "acquisition_receipt_digest": acquisition.receipt.digest,
                "observed_at": format_datetime(acquisition.envelope.observed_at),
                "epistemic_grade": acquisition.epistemic_grade,
                "provenance_grade": acquisition.provenance_grade,
                "audit": {
                    "deployment_digest": admission.deployment_snapshot_digest,
                    "recorded_at": format_datetime(self.clock.now()),
                },
            },
        )

    def _run_terminal(
        self,
        node: CaptureEgressNodeV3
        | InboxEgressNodeV3
        | ProposeChangeSetNodeV3
        | MandateSettlementNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        """Bind this terminal's per-item closure, then cap it and deliver it.

        The closure is bound before the cap is applied on purpose: a run that a
        term refused still owes an auditable account of exactly what it would
        have emitted and which term stopped it.
        """

        children, values = self._record_terminal_items(
            node,
            admission=admission,
            state=state,
            records=records,
        )
        required = TERMINAL_REQUIRED_RUNGS[node.kind]
        payload: dict[str, object] = {
            "node_id": node.node_id,
            "kind": node.kind,
            "required_rung": required,
            "children": [item.model_dump(mode="json") for item in children],
        }
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
        if not rung.permits(node.kind):
            self._append_event(
                admission,
                records,
                "terminal_egress",
                {**payload, "verdict": "refused_effective_rung"},
            )
            raise _RunRefusal(
                cast(ProcedureNodeRefusalCodeV1, rung.refusal_code),
                f"Terminal {node.kind!r} requires rung {required}; the "
                f"{rung.limiting_term} term capped this run at {rung.effective_rung}. "
                f"{rung.term(rung.limiting_term).reason}",
                node_id=node.node_id,
            )
        request = self._terminal_egress_request(
            node,
            admission=admission,
            rung=rung,
            children=children,
            values=values,
        )
        receipt = self.egress_sink.deliver_terminal_egress(request=request)
        try:
            verify_terminal_egress_receipt(request, receipt)
        except TerminalEgressError as exc:
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

    def _terminal_egress_request(
        self,
        node: CaptureEgressNodeV3
        | InboxEgressNodeV3
        | ProposeChangeSetNodeV3
        | MandateSettlementNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        rung: EffectiveRungV1,
        children: tuple[TerminalChildReceiptV1, ...],
        values: tuple[CanonicalValue, ...],
    ) -> TerminalEgressRequestV1:
        """Hand the sink the exact pins this terminal kind's law traverses."""

        bound_pin: ArtifactPin | None = None
        mandate_pin: ArtifactPin | None = None
        mandate_basis: tuple[str, ...] = ()
        if isinstance(node, CaptureEgressNodeV3):
            bound_pin = self._pin(
                node.capture_contract,
                label=f"emit_capture {node.node_id!r} CaptureContract",
            )
        elif isinstance(node, MandateSettlementNodeV3):
            bound_pin = self._pin(
                node.target_law,
                label=f"mandate_settlement {node.node_id!r} target law",
            )
            mandate_pin = self._pin(
                node.mandate,
                label=f"mandate_settlement {node.node_id!r} mandate",
            )
            mandate_basis = rung.mandate_basis_digests
        return TerminalEgressRequestV1(
            kind=node.kind,
            run_id=admission.run_id,
            node_id=node.node_id,
            accepted_coordinate=admission.accepted_coordinate,
            procedure_identity=admission.procedure_identity,
            procedure_artifact_digest=admission.procedure_artifact_digest,
            admission_binding_digest=admission.admission_binding_digest,
            effective_rung=rung.effective_rung,
            required_rung=TERMINAL_REQUIRED_RUNGS[node.kind],
            limiting_term=rung.limiting_term,
            granted_operation=rung.granted_operation(node.kind),
            bound_artifact_pin=bound_pin,
            mandate_pin=mandate_pin,
            mandate_basis_digests=mandate_basis,
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

    def _record_terminal_items(
        self,
        node: CaptureEgressNodeV3
        | InboxEgressNodeV3
        | ProposeChangeSetNodeV3
        | MandateSettlementNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> tuple[tuple[TerminalChildReceiptV1, ...], tuple[CanonicalValue, ...]]:
        """Derive one manifest per terminal item from the exact closure it consumed."""

        declared = _terminal_item_templates(node)
        base = _node_policy_tokens(node) | state.control
        values, item_tokens = _terminal_items(declared, state=state)
        outcomes = tuple(
            sorted(state.outcomes.values(), key=lambda item: item.input_name.encode("utf-8"))
        )
        receipts: list[TerminalChildReceiptV1] = []
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
            stored = self._append_event(
                admission,
                records,
                "item_dependencies",
                {
                    "tag": "playbill-procedure-item-dependencies-v1",
                    "manifest": manifest.model_dump(mode="json"),
                    "derived": derived.model_dump(mode="json"),
                    "audit": {
                        "deployment_digest": admission.deployment_snapshot_digest,
                        "recorded_at": format_datetime(self.clock.now()),
                    },
                },
            )
            receipts.append(
                TerminalChildReceiptV1(
                    child_index=index,
                    item_key=item_key,
                    manifest_digest=terminal_item_manifest_digest(manifest),
                    record_digest=stored.record_digest,
                    sequence=stored.record.sequence,
                )
            )
        return tuple(receipts), tuple(values)

    def _run_provider(
        self,
        node: ProviderNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
    ) -> None:
        if self.provider_executor is None:
            raise _RunRefusal(
                "provider_unavailable",
                "No Provider executor is registered for this Procedure runtime.",
                node_id=node.node_id,
            )
        provider = self._pin(node.provider, label=f"provider {node.node_id!r}")
        environment = self._pin(node.environment, label=f"provider {node.node_id!r} environment")
        contract_in = self._pin(node.contract_in, label=f"provider {node.node_id!r} input")
        contract_out = self._pin(node.contract_out, label=f"provider {node.node_id!r} output")
        resolved = normalize_canonical(
            _resolve_template(
                node.input,
                input_payload=state.input_payload,
                outputs=state.outputs,
            )
        )
        payload = _validate_node_contract(
            self.contract_validator,
            contract=contract_in,
            payload=resolved,
            direction="input",
            node_id=node.node_id,
            max_items=_effective_max_items(admission),
            observe_items=state.observe_items,
        )
        if state.provider_calls >= admission.budget.max_provider_calls:
            raise _BudgetExceeded(
                "max_provider_calls",
                limit=admission.budget.max_provider_calls,
                observed=state.provider_calls + 1,
                node_id=node.node_id,
            )
        state.provider_calls += 1
        effect_policy = node.effect_policy
        effectful = effect_policy is not None
        effect_id = typed_digest(
            ArtifactDigest,
            "playbill-procedure-effect-intent-v1",
            {
                "run_id": admission.run_id,
                "node_id": node.node_id,
                "provider_digest": provider.artifact_digest,
                "environment_digest": environment.artifact_digest,
                "input_digest": run_value_digest("provider-input", payload),
            },
        ).tagged
        if effect_policy is not None:
            self._checkpoint_current(admission, effect=True)
            self._append_event(
                admission,
                records,
                "effect_intent",
                {
                    "effect_id": effect_id,
                    "node_id": node.node_id,
                    "provider": provider.model_dump(mode="json"),
                    "environment": environment.model_dump(mode="json"),
                    "effect_policy": self._pin(
                        effect_policy,
                        label=f"provider {node.node_id!r} effect policy",
                    ).model_dump(mode="json"),
                    "input_digest": run_value_digest("provider-input", payload),
                },
            )
            refusal = effect_dispatch_refusal(
                invocation_origin=admission.invocation_origin,
                actor_context=admission.actor_context,
                declared_effect_grants=self.declared_effect_grants,
            )
            if refusal is not None:
                raise _RunRefusal(
                    cast(ProcedureNodeRefusalCodeV1, refusal[0]),
                    refusal[1],
                    node_id=node.node_id,
                )
        result = self.provider_executor.execute_provider(
            provider=provider,
            environment=environment,
            contract_in=contract_in,
            contract_out=contract_out,
            payload=payload,
            actor_context=admission.actor_context,
        )
        output = _validate_node_contract(
            self.contract_validator,
            contract=contract_out,
            payload=normalize_canonical(result.output),
            direction="output",
            node_id=node.node_id,
            max_items=_effective_max_items(admission),
            observe_items=state.observe_items,
        )
        state.outputs[node.as_] = output
        state.provenance[node.as_] = AliasProvenanceV1(whole=_base_tokens(node, state, node.input))
        if effectful:
            self._append_event(
                admission,
                records,
                "effect_result",
                {
                    "effect_id": effect_id,
                    "node_id": node.node_id,
                    "output_digest": run_value_digest("provider-output", output),
                    "trace": result.trace,
                },
            )

    def _run_repeat(
        self,
        node: RepeatNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
        state: _RunState,
        records: list[StoredProcedureJournalRecordV1],
        started_ns: int,
    ) -> None:
        attempts: list[CanonicalValue] = []
        for attempt in range(1, node.max_attempts + 1):
            local_outputs: dict[str, CanonicalValue] = {}
            local_provenance: dict[str, AliasProvenanceV1] = {}
            for body in node.body:
                self._run_repeat_body(
                    body,
                    admission=admission,
                    state=state,
                    local_outputs=local_outputs,
                    local_provenance=local_provenance,
                    records=records,
                )
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
            except PlaybillExecutionError as exc:
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
                repeated_tokens = frozenset(
                    token for provenance in local_provenance.values() for token in provenance.whole
                )
                state.provenance[node.as_] = AliasProvenanceV1(
                    whole=_base_tokens(node, state, tuple(body.spec for body in node.body))
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
        body: RepeatBodyNodeV3,
        *,
        admission: ProcedureRunAdmissionV1,
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
                base=_base_tokens(body, local_state, declared_spec),
            )
            return
        if self.provider_executor is None or body.provider is None or body.environment is None:
            raise _RunRefusal(
                "provider_unavailable",
                "Repeat provider operation has no registered executor.",
                node_id=body.node_id,
            )
        provider = self._pin(body.provider, label=f"repeat {body.node_id!r} provider")
        environment = self._pin(body.environment, label=f"repeat {body.node_id!r} environment")
        contract_in = self._pin(body.contract_in, label=f"repeat {body.node_id!r} input")
        contract_out = self._pin(body.contract_out, label=f"repeat {body.node_id!r} output")
        if state.provider_calls >= admission.budget.max_provider_calls:
            raise _BudgetExceeded(
                "max_provider_calls",
                limit=admission.budget.max_provider_calls,
                observed=state.provider_calls + 1,
                node_id=body.node_id,
            )
        state.provider_calls += 1
        validated_input = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_in,
                payload=spec,
                direction="input",
            )
        )
        result = self.provider_executor.execute_provider(
            provider=provider,
            environment=environment,
            contract_in=contract_in,
            contract_out=contract_out,
            payload=validated_input,
            actor_context=admission.actor_context,
        )
        local_outputs[body.as_] = normalize_canonical(
            self.contract_validator.validate_contract(
                contract=contract_out,
                payload=normalize_canonical(result.output),
                direction="output",
            )
        )

    def _append_event(
        self,
        admission: ProcedureRunAdmissionV1,
        records: list[StoredProcedureJournalRecordV1],
        event_kind: JournalEventKindV1,
        payload: object,
    ) -> StoredProcedureJournalRecordV1:
        payload_bytes = journal_payload_bytes(payload)
        metadata = self.bodies.store(payload_bytes)
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
        except PlaybillJournalError:
            raise
        records.append(stored)
        self.run_index.apply_record(stored, payload=normalize_canonical(payload))
        return stored

    def _replay_completed(
        self,
        admission: ProcedureRunAdmissionV1,
        records: tuple[StoredProcedureJournalRecordV1, ...],
    ) -> ProcedureRunResultV1:
        run_records = [
            item
            for item in records
            if item.record.run_id == admission.run_id
            and item.record.admission_binding_digest == admission.admission_binding_digest
        ]
        if not run_records or run_records[-1].record.event_kind != "attempt_finalized":
            raise PlaybillExecutionError("completed run index is not supported by journal exhaust")
        access = BodyAccessContext(principal_id="procedure-run-replay", can_read_body=True)
        payload = parse_journal_payload(
            self.bodies.read(run_records[-1].record.payload_digest, access=access)
        )
        if not isinstance(payload, dict):
            raise PlaybillExecutionError("attempt-finalized payload is not an object")
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
            raise PlaybillExecutionError(
                "attempt-finalized payload cannot reproduce its run result"
            ) from exc

    @staticmethod
    def _receipt(
        admission: ProcedureRunAdmissionV1,
        records: list[StoredProcedureJournalRecordV1],
    ) -> ProcedureRunReceiptV1:
        if not records:  # pragma: no cover - attempt start always lands first
            raise PlaybillExecutionError("Procedure run produced no exhaust records")
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


def _node_policy_tokens(node: object) -> frozenset[DependencyToken]:
    """Return the exact laws and policies one node's own pins commit it to."""

    return frozenset(
        policy_token(binding.artifact_digest)
        for binding in iter_pin_bindings(node)
        if isinstance(binding, ArtifactPin)
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


def _template_alias(value: object) -> str | None:
    if isinstance(value, str) and value.startswith(_STEP_PREFIX):
        return value[len(_STEP_PREFIX) :].split(".")[0]
    return None


def _base_tokens(
    node: object,
    state: _RunState,
    template: object,
) -> frozenset[DependencyToken]:
    return (
        _node_policy_tokens(node)
        | state.control
        | state.alias_tokens(_referenced_aliases(template))
    )


def _transform_provenance(
    node: TransformNodeV3 | RepeatBodyNodeV3,
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
    slot_aliases = {slot: _template_alias(spec.get(slot)) for slot in _ITEM_SLOTS}
    items: list[frozenset[DependencyToken]] = []
    for refs in lineage:
        tokens: frozenset[DependencyToken] = frozenset()
        for slot, index in refs:
            alias = slot_aliases.get(slot)
            if alias is not None:
                tokens = tokens | state.item_tokens(alias, index)
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
    alias = _template_alias(template)
    if alias is None:
        return AliasProvenanceV1(whole=base)
    return AliasProvenanceV1(
        whole=base,
        items=tuple(state.item_tokens(alias, index) for index in range(len(projected))),
    )


def _terminal_item_templates(
    node: CaptureEgressNodeV3
    | InboxEgressNodeV3
    | ProposeChangeSetNodeV3
    | MandateSettlementNodeV3,
) -> object:
    if isinstance(node, ProposeChangeSetNodeV3):
        return list(node.candidate_templates)
    return node.input


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
    alias = _template_alias(template)
    if alias is None:
        fallback = shared | state.alias_tokens(_referenced_aliases(template))
        return values, tuple(fallback for _ in values)
    return values, tuple(shared | state.item_tokens(alias, index) for index in range(len(values)))


def _decision_digest(run_id: str, decision: AcquisitionInputDecisionV1) -> str:
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
        raise PlaybillExecutionError(f"runtime reference {reference!r} does not resolve")
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
            raise PlaybillExecutionError(f"runtime reference {value!r} names absent output")
        return _resolve_path(outputs[alias], parts[1:], reference=value)
    if value == "$item":
        if item is None:
            raise PlaybillExecutionError("$item is unavailable outside item transforms")
        return item
    if value.startswith("$item."):
        if item is None:
            raise PlaybillExecutionError("$item is unavailable outside item transforms")
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
            raise PlaybillExecutionError(
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
        raise PlaybillExecutionError("typed transform spec is absent")
    payload = spec.model_dump(mode="json", exclude={"tag"})
    if kind == "adapter":
        return normalize_canonical(payload["value"])
    return normalize_canonical(payload)


_TRANSFORM_INPUT_REFUSAL_CODES: dict[str, ProcedureNodeRefusalCodeV1] = {
    "adapter": "adapter_value_invalid",
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
    except PlaybillExecutionError as exc:
        raise _RunRefusal(
            "runtime_reference_unresolved",
            "A Procedure runtime reference did not resolve.",
            node_id=node_id,
        ) from exc
    except Exception as exc:
        code: ProcedureNodeRefusalCodeV1 = (
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
                raise PlaybillExecutionError("Contract budget validator returned an invalid result")
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
        code: ProcedureNodeRefusalCodeV1 = (
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
            f"The Procedure node {direction} contract refused its value.",
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
    except PlaybillExecutionError as exc:
        raise _RunRefusal(
            _TRANSFORM_INPUT_REFUSAL_CODES[kind],
            f"The {kind} transform input is invalid.",
            node_id=node_id,
        ) from exc


PROCEDURE_RESULT_MAX_BYTES = 1_048_576


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
        budget=ProcedureBudgetRefusalDetailV1(
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

    if kind == "adapter":
        return spec, None
    if list_boundary is None:
        max_items = None
    if not isinstance(spec, dict):
        raise PlaybillExecutionError(f"transform {kind!r} requires an object spec")
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
            raise PlaybillExecutionError("shape_items fields must be an object")
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
            raise PlaybillExecutionError("filter_items where must be an object")
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
            raise PlaybillExecutionError("dedupe_items keys must be a string list")
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
            raise PlaybillExecutionError("join_items keys and fields are malformed")
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
    raise PlaybillExecutionError(f"unsupported deterministic transform {kind!r}")


def _check_return_budget(
    value: CanonicalValue,
    *,
    max_items: int | None,
    node_id: str,
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
    if size > PROCEDURE_RESULT_MAX_BYTES:
        raise _budget_refusal(
            budget_kind="result_bytes",
            limit=PROCEDURE_RESULT_MAX_BYTES,
            observed=size,
            node_id=node_id,
        )


def _operand_value(
    operand: PredicateOperandV1,
    *,
    input_payload: CanonicalValue,
    outputs: dict[str, CanonicalValue],
    parameters: CanonicalValue,
) -> CanonicalValue:
    if operand.kind == "literal":
        return normalize_canonical(operand.value)
    if operand.kind == "parameter":
        if not isinstance(parameters, dict) or operand.parameter_name not in parameters:
            raise PlaybillExecutionError("guard parameter is absent")
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
    raise PlaybillExecutionError(f"guard operand {name!r} is absent")


def _compare(left: CanonicalValue, operator: str, right: CanonicalValue) -> bool:
    if operator == "eq":
        return left == right
    if operator == "ne":
        return left != right
    if isinstance(left, bool) or isinstance(right, bool):
        raise PlaybillExecutionError("ordered guard operands cannot be booleans")
    if isinstance(left, int) and isinstance(right, int):
        comparable_left: int | str = left
        comparable_right: int | str = right
    elif isinstance(left, str) and isinstance(right, str):
        comparable_left = left
        comparable_right = right
    else:
        raise PlaybillExecutionError("ordered guard operands must share an int or string type")
    if operator in {"gt", "after"}:
        return comparable_left > comparable_right  # type: ignore[operator]
    if operator in {"gte", "on_or_after"}:
        return comparable_left >= comparable_right  # type: ignore[operator]
    if operator in {"lt", "before"}:
        return comparable_left < comparable_right  # type: ignore[operator]
    if operator in {"lte", "on_or_before"}:
        return comparable_left <= comparable_right  # type: ignore[operator]
    raise PlaybillExecutionError(f"unknown guard operator {operator!r}")


def _evaluate_predicate(
    predicate: GuardPredicateV1,
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
    "AcceptedStateRunMaterialV1",
    "ContractValidatorProtocol",
    "ExhaustRunMaterialV1",
    "LandedCaptureRunMaterialV1",
    "PreparedProcedureRunV1",
    "ProcedureActivationAuthorityProtocol",
    "ProcedureClockProtocol",
    "ProcedureExecutor",
    "ProcedureNodePinSetV1",
    "ProcedureRunAdmissionV1",
    "ProcedureRunReceiptV1",
    "ProcedureRunRefusalV1",
    "ProcedureRunResultV1",
    "ProcedureRunStatusV1",
    "ProviderExecutorProtocol",
    "ProviderInvocationResultV1",
    "StateTapReaderProtocol",
    "SystemProcedureClock",
    "accepted_procedure_pin_set_digest",
    "procedure_node_pin_sets",
    "prepare_direct_procedure_run",
    "procedure_admission_digest",
    "procedure_pin_set_digest",
    "resolve_procedure_pin",
    "procedure_run_receipt_digest",
    "run_value_digest",
]
