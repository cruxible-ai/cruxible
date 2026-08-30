"""Shared typed failure contracts for served Procedure runs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc

ProcedureAdmissionRefusalCodeV1: TypeAlias = Literal[
    "binding_required",
    "unsupported_node",
    "effectful_unsupported",
    "not_current",
    "artifact_binding_mismatch",
    "pin_binding_mismatch",
    "input_material_mismatch",
    "state_tap_refused",
    "replay_material_mismatch",
]
ProcedureNodeRefusalCodeV1: TypeAlias = Literal[
    "guard_refused",
    "repeat_exhausted",
    "budget_exhausted",
    "runtime_reference_unresolved",
    "contract_input_refused",
    "contract_output_refused",
    "adapter_value_invalid",
    "shape_items_input_invalid",
    "filter_items_input_invalid",
    "dedupe_items_input_invalid",
    "join_items_left_input_invalid",
    "join_items_right_input_invalid",
    "aggregate_items_input_invalid",
    "result_not_canonical",
    "line_binding_required",
    "source_acquisition_unavailable",
    "source_material_unavailable",
    "terminal_not_available",
    "terminal_egress_unverified",
    "provider_unavailable",
    "playbill.acquisition.unavailable",
    "playbill.acquisition.stale",
    "playbill.acquisition.oversized",
    "playbill.acquisition.refused",
    "effect_grant_unrecognized",
    "effect_dispatch_requires_actor",
    "effect_dispatch_requires_authenticated_actor",
    "terminal_rung_capped_by_procedure_terminal_capability",
    "terminal_rung_capped_by_line_requested_rung",
    "terminal_rung_capped_by_propagated_sensitivity",
    "terminal_rung_capped_by_mandate_grant",
    "terminal_rung_capped_by_calibration",
]
ProcedureOperationalFailureCodeV1: TypeAlias = Literal[
    "wall_clock_exhausted",
    "cas_unavailable_at_replay",
    "replay_material_mismatch",
    "journal_append_failed",
    "journal_read_failed",
    "journal_conflict",
    "run_recovery_required",
]
ProcedureInternalFailureCodeV1: TypeAlias = Literal[
    "unexpected_exception",
    "journal_integrity_error",
    "run_record_invalid",
    "compiler_invariant_broken",
]


class _StrictResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str | None) -> str | None:
    if value is None:
        return None
    Sha256Value.from_tagged(value)
    return value


class ProcedureJournalCoordinateV1(_StrictResultModel):
    tag: Literal["playbill-procedure-journal-coordinate-v1"] = (
        "playbill-procedure-journal-coordinate-v1"
    )
    stream_instance_id: str
    journal_family: str
    stream_id: str
    partition_id: str
    sequence: int = Field(ge=1)
    record_digest: str

    _record_digest = field_validator("record_digest")(_digest)


class ProcedureBudgetRefusalDetailV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-refusal-detail-v1"] = (
        "playbill-procedure-budget-refusal-detail-v1"
    )
    budget_kind: Literal[
        "max_items",
        "result_bytes",
        "wall_clock",
        "max_provider_calls",
        "max_capture_bytes",
    ]
    limit: int = Field(ge=0)
    observed: int = Field(ge=1)


class ProcedureAdmissionRefusalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-admission-refusal-v1"] = (
        "playbill-procedure-admission-refusal-v1"
    )
    classification: Literal["admission_refusal"] = "admission_refusal"
    code: ProcedureAdmissionRefusalCodeV1
    message: str
    details: object = Field(default_factory=dict)
    retryable: bool = False

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureNodeRefusalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-node-refusal-v1"] = "playbill-procedure-node-refusal-v1"
    classification: Literal["node_refusal"] = "node_refusal"
    code: ProcedureNodeRefusalCodeV1
    message: str
    node_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    detail_code: str | None = None
    details: object = Field(default_factory=dict)
    budget: ProcedureBudgetRefusalDetailV1 | None = None
    retryable: bool = False

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)

    @model_validator(mode="after")
    def _typed_detail(self) -> "ProcedureNodeRefusalV1":
        if self.code == "guard_refused" and self.detail_code is None:
            raise ValueError("guard refusal requires the Procedure-authored detail code")
        if self.code == "budget_exhausted" and self.budget is None:
            raise ValueError("budget refusal requires typed budget detail")
        if self.code != "guard_refused" and self.detail_code is not None:
            raise ValueError("only a guard refusal carries a Procedure-authored detail code")
        if self.code != "budget_exhausted" and self.budget is not None:
            raise ValueError("only a budget refusal carries typed budget detail")
        return self


class ProcedureOperationalFailureV1(_StrictResultModel):
    tag: Literal["playbill-procedure-operational-failure-v1"] = (
        "playbill-procedure-operational-failure-v1"
    )
    classification: Literal["operational_failure"] = "operational_failure"
    code: ProcedureOperationalFailureCodeV1
    message: str
    last_node_id: str | None = None
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    details: object = Field(default_factory=dict)
    retryable: bool = True

    @field_validator("details", mode="before")
    @classmethod
    def _details(cls, value: object) -> object:
        return normalize_canonical(value)


class ProcedureInternalFailureV1(_StrictResultModel):
    tag: Literal["playbill-procedure-internal-failure-v1"] = (
        "playbill-procedure-internal-failure-v1"
    )
    classification: Literal["internal_failure"] = "internal_failure"
    code: ProcedureInternalFailureCodeV1
    message: str
    correlation_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None


class ProcedureRunAttributionV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-attribution-v1"] = "playbill-procedure-run-attribution-v1"
    actor_type: str
    actor_id: str
    org_id: str
    operation_id: str
    request_id: str | None = None
    recorded_time: datetime

    @field_validator("recorded_time")
    @classmethod
    def _recorded_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProcedurePendingSuccessorV1(_StrictResultModel):
    tag: Literal["playbill-procedure-pending-successor-v1"] = (
        "playbill-procedure-pending-successor-v1"
    )
    proposal_id: str
    pending_successor_digest: str


class ProcedureRunReceiptV2(_StrictResultModel):
    tag: Literal["playbill-procedure-run-receipt-v2"] = "playbill-procedure-run-receipt-v2"
    run_id: str
    admission_binding_digest: str
    semantic_replay_key_digest: str
    semantic_result_digest: str | None
    bound_coordinate: AcceptedCoordinate
    head_at_admission: AcceptedCoordinate
    lane: Literal["current", "replay"]
    evaluation_time: datetime
    validated_pins: tuple[ArtifactPin, ...]
    admitted_inputs: tuple[dict[str, object], ...]
    attribution: ProcedureRunAttributionV1
    stream_instance_id: str
    journal_family: str
    stream_id: str
    partition_id: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    record_digests: tuple[str, ...]
    chain_head_digest: str

    _digests = field_validator(
        "admission_binding_digest",
        "semantic_replay_key_digest",
        "semantic_result_digest",
        "chain_head_digest",
    )(_digest)

    @field_validator("record_digests")
    @classmethod
    def _record_digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for digest in value:
            _digest(digest)
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProcedureBudgetExceededDetailV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-exceeded-detail-v1"] = (
        "playbill-procedure-budget-exceeded-detail-v1"
    )
    dimension: Literal["max_items"] = "max_items"
    limit: int = Field(ge=1)
    observed: int = Field(ge=1)
    boundary: str
    field_path: str


class ProcedureBudgetExhaustedV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-exhausted-v1"] = (
        "playbill-procedure-budget-exhausted-v1"
    )
    classification: Literal["budget_exhausted"] = "budget_exhausted"
    code: Literal["budget_max_items_exceeded"] = "budget_max_items_exceeded"
    message: Literal["A Procedure collection exceeded its declared item bound."] = (
        "A Procedure collection exceeded its declared item bound."
    )
    node_id: str
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None
    details: ProcedureBudgetExceededDetailV1
    retryable: Literal[False] = False


class ProcedureHaltTerminalV1(_StrictResultModel):
    tag: Literal["playbill-procedure-halt-terminal-v1"] = "playbill-procedure-halt-terminal-v1"
    classification: Literal["halted"] = "halted"
    node_id: str
    reason: str | None = None
    journal_coordinate: ProcedureJournalCoordinateV1 | None = None


ProcedureTerminalV1: TypeAlias = Annotated[
    ProcedureAdmissionRefusalV1
    | ProcedureNodeRefusalV1
    | ProcedureOperationalFailureV1
    | ProcedureInternalFailureV1
    | ProcedureBudgetExhaustedV1
    | ProcedureHaltTerminalV1,
    Field(discriminator="tag"),
]


class ProcedureBudgetBoundaryObservationV1(_StrictResultModel):
    tag: Literal["playbill-procedure-budget-boundary-observation-v1"] = (
        "playbill-procedure-budget-boundary-observation-v1"
    )
    high_water: int = Field(ge=0)
    boundary: str | None = None
    field_path: str | None = None

    @model_validator(mode="after")
    def _zero_location(self) -> "ProcedureBudgetBoundaryObservationV1":
        if self.high_water == 0 and (self.boundary is not None or self.field_path is not None):
            raise ValueError("a zero boundary observation has no location")
        if self.high_water > 0 and (self.boundary is None or self.field_path is None):
            raise ValueError("a nonzero boundary observation requires its location")
        return self


class ProcedureRunBudgetDeclaredV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-declared-v1"] = (
        "playbill-procedure-run-budget-declared-v1"
    )
    budget: ProcedureBudgetV3
    hard_caps: ProcedureHardCapsV3
    result_bytes_cap: Literal[1_048_576] = 1_048_576


class ProcedureRunBudgetObservedV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-observed-v1"] = (
        "playbill-procedure-run-budget-observed-v1"
    )
    max_items: ProcedureBudgetBoundaryObservationV1
    result_bytes: ProcedureBudgetBoundaryObservationV1
    provider_calls: int = Field(ge=0)
    capture_bytes: int = Field(ge=0)
    wall_clock_microseconds: int = Field(ge=0)


class ProcedureRunBudgetV1(_StrictResultModel):
    tag: Literal["playbill-procedure-run-budget-v1"] = "playbill-procedure-run-budget-v1"
    declared: ProcedureRunBudgetDeclaredV1
    observed: ProcedureRunBudgetObservedV1


class ProcedureRunReceiptV3(ProcedureRunReceiptV2):
    tag: Literal["playbill-procedure-run-receipt-v3"] = "playbill-procedure-run-receipt-v3"  # type: ignore[assignment]
    status: Literal[
        "succeeded",
        "node_refused",
        "operational_failed",
        "internal_failed",
        "halted",
    ]
    terminal: ProcedureTerminalV1 | None
    budget: ProcedureRunBudgetV1


__all__ = [
    "ProcedureAdmissionRefusalCodeV1",
    "ProcedureAdmissionRefusalV1",
    "ProcedureBudgetBoundaryObservationV1",
    "ProcedureBudgetExceededDetailV1",
    "ProcedureBudgetExhaustedV1",
    "ProcedureBudgetRefusalDetailV1",
    "ProcedureHaltTerminalV1",
    "ProcedureInternalFailureCodeV1",
    "ProcedureInternalFailureV1",
    "ProcedureJournalCoordinateV1",
    "ProcedureNodeRefusalCodeV1",
    "ProcedureNodeRefusalV1",
    "ProcedureOperationalFailureCodeV1",
    "ProcedureOperationalFailureV1",
    "ProcedurePendingSuccessorV1",
    "ProcedureRunAttributionV1",
    "ProcedureRunBudgetDeclaredV1",
    "ProcedureRunBudgetObservedV1",
    "ProcedureRunBudgetV1",
    "ProcedureRunReceiptV2",
    "ProcedureRunReceiptV3",
    "ProcedureTerminalV1",
]
