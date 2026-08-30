"""Shared typed failure contracts for served Procedure runs."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
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
]
ProcedureOperationalFailureCodeV1: TypeAlias = Literal[
    "wall_clock_exhausted",
    "cas_unavailable_at_replay",
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
    budget_kind: Literal["max_items", "result_bytes"]
    limit: int = Field(ge=1)
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


ProcedureTerminalV1: TypeAlias = Annotated[
    ProcedureAdmissionRefusalV1
    | ProcedureNodeRefusalV1
    | ProcedureOperationalFailureV1
    | ProcedureInternalFailureV1,
    Field(discriminator="tag"),
]


__all__ = [
    "ProcedureAdmissionRefusalCodeV1",
    "ProcedureAdmissionRefusalV1",
    "ProcedureBudgetRefusalDetailV1",
    "ProcedureInternalFailureCodeV1",
    "ProcedureInternalFailureV1",
    "ProcedureJournalCoordinateV1",
    "ProcedureNodeRefusalCodeV1",
    "ProcedureNodeRefusalV1",
    "ProcedureOperationalFailureCodeV1",
    "ProcedureOperationalFailureV1",
    "ProcedureRunAttributionV1",
    "ProcedureRunReceiptV2",
    "ProcedureTerminalV1",
]
