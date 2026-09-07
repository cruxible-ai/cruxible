"""Served wire for Procedure measurement resolution and exact-grain readings.

A measurement is declared on the accepted Procedure and ACTIVATED by the
generation that accepted that exact revision. Evaluating it later reads real
evidence at an explicit observation coordinate and instant, records one
resolution per activation, and binds that resolution to the grain one real run
actually reached. Three coordinates are therefore always distinct here:

* the ACTIVATION coordinate, the generation that accepted the declaration;
* the RUN ADMISSION coordinate, the accepted state one run was bound to;
* the OBSERVATION coordinate and instant, where the evidence is evaluated.

None of these records is accepted state, and none grants authority: readings and
resolutions are operational exhaust until an existing governed promotion accepts
them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import ServedRepairV1, served_repair_for_refusal
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.temporal import ensure_utc, format_datetime

#: Closed served refusals for the measurement doors. Each names a runnable
#: repair in the shared repair catalog, so none joins the undeclared count.
ProcedureMeasurementRefusalCodeV1: TypeAlias = Literal[
    # The named measurement is not declared on the accepted Procedure revision.
    "measurement_not_declared",
    # The run named for a reading executed another Procedure revision.
    "measurement_run_mismatch",
    # The declared Claim statement or QueryDefinition is absent at the
    # observation coordinate.
    "measurement_subject_absent",
    # The declared statement digest or query pin differs from what the
    # observation coordinate holds; the declaration cannot be evaluated there.
    "measurement_subject_mismatch",
    # The declaration names an observation basis (execution option, instant)
    # the served evaluator cannot honour.
    "measurement_basis_unsupported",
    # A retried reading key arrived with a different semantic payload.
    "measurement_reading_conflict",
    # The contract partition moved under this evaluation (another writer).
    "measurement_resolution_conflict",
]

ProcedureMeasurementStatusV1: TypeAlias = Literal[
    # The observation instant precedes check_at; nothing was evaluated.
    "pending",
    # The window closed with no standing resolution; nothing was evaluated.
    "expired",
    # A non-overturned resolution stands (written now or found).
    "resolved",
]

ProcedureReadingStatusV1: TypeAlias = Literal[
    # No run was named; the resolution alone was produced or found.
    "not_requested",
    # A reading was appended for this run and grain.
    "recorded",
    # The same reading already stood for this key; the stored record is returned.
    "replayed",
    # The run has not finalized; retry once it has.
    "run_not_final",
    # The run finalized without reaching this grain (untaken arm, refused or
    # unfired node, unsuccessful unit); no reading credits it.
    "grain_not_occurred",
    # No resolution stands (pending or expired), so no reading can be graded.
    "no_resolution",
]

ProcedureMeasurementVerdictV1: TypeAlias = Literal["satisfied", "contradicted", "indeterminate"]


class _StrictReadingWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str | None) -> str | None:
    if value is not None:
        Sha256Value.from_tagged(value)
    return value


class ProcedureMeasurementEligibilityV1(_StrictReadingWireModel):
    """The window the producer honours, and where this evaluation stood in it.

    ``activated_at`` reads the signed acceptance instant of the declaring
    generation; ``check_at`` and ``expires_at`` are that instant plus the
    declaration's ``check_after`` and ``expires_after``. The producer evaluates
    only inside ``[check_at, expires_at)``: before it the measurement is
    pending, at or after ``expires_at`` it is expired, and neither writes a
    resolution. The frozen resolution law is narrower than this producer rule
    and is left exactly as it was.
    """

    tag: Literal["playbill-procedure-measurement-eligibility-v1"] = (
        "playbill-procedure-measurement-eligibility-v1"
    )
    activation_coordinate: AcceptedCoordinate
    activated_at: datetime
    check_at: datetime
    expires_at: datetime
    observation_coordinate: AcceptedCoordinate
    observation_time: datetime
    window: Literal["before_check", "open", "closed"]

    @field_validator("activated_at", "check_at", "expires_at", "observation_time")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer(
        "activated_at", "check_at", "expires_at", "observation_time", when_used="json"
    )
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureMeasurementResolutionSummaryV1(_StrictReadingWireModel):
    """The standing resolution for one activation, with its retrievable record."""

    tag: Literal["playbill-procedure-measurement-resolution-summary-v1"] = (
        "playbill-procedure-measurement-resolution-summary-v1"
    )
    resolution_id: str
    contract_id: str
    sequence: int = Field(ge=1)
    verdict: ProcedureMeasurementVerdictV1
    value: object | None = None
    note: str | None = None
    observed_at: datetime
    recorded_at: datetime
    evidence_refs: tuple[dict[str, object], ...] = ()
    journal_partition_id: str
    journal_record_digest: str
    resolution_digest: str
    written_now: bool

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> tuple[dict[str, object], ...]:
        normalized = normalize_canonical(list(value) if isinstance(value, tuple) else value)
        if not isinstance(normalized, list) or not all(
            isinstance(item, dict) for item in normalized
        ):
            raise ValueError("resolution evidence references must be canonical objects")
        return tuple(cast(dict[str, object], item) for item in normalized)

    @field_validator("journal_record_digest", "resolution_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        return cast(str, _digest(value))

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observed_at", "recorded_at", when_used="json")
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureReadingSummaryV1(_StrictReadingWireModel):
    """One retained exact-grain reading and the run it credits."""

    tag: Literal["playbill-procedure-reading-summary-v1"] = "playbill-procedure-reading-summary-v1"
    reading_id: str
    reading_digest: str
    journal_partition_id: str
    journal_record_digest: str
    subject_grain: Literal["procedure_unit", "node", "arm"]
    subject: SemanticAddress
    accepted_coordinate: AcceptedCoordinate
    definition_digest: str
    node_id: str | None = None
    from_node_id: str | None = None
    arm_label: Literal["on_true", "on_false"] | None = None
    grade: Literal["contract", "observation"]
    measurement_name: str | None = None
    contract_id: str | None = None
    resolution_id: str | None = None
    verdict: ProcedureMeasurementVerdictV1
    value: object | None = None
    run_id: str | None = None
    run_receipt_digest: str | None = None
    episode_ref: str | None = None
    evidence_refs: tuple[dict[str, object], ...] = ()
    claim_attestation_digests: tuple[str, ...] = ()
    observed_at: datetime
    recorded_at: datetime
    actor_id: str
    idempotency_key: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object | None) -> object | None:
        return None if value is None else normalize_canonical(value)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> tuple[dict[str, object], ...]:
        normalized = normalize_canonical(list(value) if isinstance(value, tuple) else value)
        if not isinstance(normalized, list) or not all(
            isinstance(item, dict) for item in normalized
        ):
            raise ValueError("reading evidence references must be canonical objects")
        return tuple(cast(dict[str, object], item) for item in normalized)

    @field_validator(
        "reading_digest",
        "journal_record_digest",
        "definition_digest",
        "run_receipt_digest",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        return _digest(value)

    @field_validator("observed_at", "recorded_at")
    @classmethod
    def _times(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observed_at", "recorded_at", when_used="json")
    def _serialize_times(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureMeasurementRowV1(_StrictReadingWireModel):
    """One declared measurement's outcome for this evaluation request."""

    tag: Literal["playbill-procedure-measurement-row-v1"] = "playbill-procedure-measurement-row-v1"
    measurement_name: str
    measurement_kind: Literal["accepted_query", "claim_statement", "claim_attestation"]
    contract_id: str
    activation_id: str
    subject_grain: Literal["procedure_unit", "node", "arm"]
    subject: SemanticAddress
    status: ProcedureMeasurementStatusV1
    eligibility: ProcedureMeasurementEligibilityV1
    resolution: ProcedureMeasurementResolutionSummaryV1 | None = None
    reading_status: ProcedureReadingStatusV1
    reading: ProcedureReadingSummaryV1 | None = None
    detail: str | None = None


class PlaybillProcedureMeasureRequestV1(_StrictReadingWireModel):
    """Evaluate the due measurements of one accepted Procedure, optionally for one run.

    ``evaluation_time`` is the explicit OBSERVATION INSTANT; ``at`` is the
    OBSERVATION COORDINATE. Both default to the daemon's current view when
    omitted. ``run_id`` names the real run whose grain a reading should
    credit; without it only the contract-level resolution is produced.
    """

    tag: Literal["playbill-procedure-measure-request-v1"] = "playbill-procedure-measure-request-v1"
    run_id: str | None = None
    measurement_names: tuple[str, ...] = ()
    evaluation_time: datetime | None = None
    at: AcceptedCoordinate | None = None

    @field_validator("measurement_names")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("measurement_names must be sorted and unique")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class PlaybillProcedureMeasureResultV1(_StrictReadingWireModel):
    tag: Literal["playbill-procedure-measure-result-v1"] = "playbill-procedure-measure-result-v1"
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    activation_coordinate: AcceptedCoordinate
    observation_coordinate: AcceptedCoordinate
    observation_time: datetime
    run_id: str | None = None
    run_admission_coordinate: AcceptedCoordinate | None = None
    rows: tuple[ProcedureMeasurementRowV1, ...]

    @field_validator("procedure_artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        return cast(str, _digest(value))

    @field_validator("observation_time")
    @classmethod
    def _observation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observation_time", when_used="json")
    def _serialize_observation_time(self, value: datetime) -> str | None:
        return format_datetime(value)


class ProcedureMeasurementContractStatusV1(_StrictReadingWireModel):
    """Read-only standing of one activation at the inspection instant."""

    tag: Literal["playbill-procedure-measurement-contract-status-v1"] = (
        "playbill-procedure-measurement-contract-status-v1"
    )
    measurement_name: str
    measurement_kind: Literal["accepted_query", "claim_statement", "claim_attestation"]
    contract_id: str
    activation_id: str
    subject_grain: Literal["procedure_unit", "node", "arm"]
    subject: SemanticAddress
    status: ProcedureMeasurementStatusV1
    eligibility: ProcedureMeasurementEligibilityV1
    resolution: ProcedureMeasurementResolutionSummaryV1 | None = None
    reading_count: int = Field(ge=0)


class PlaybillProcedureReadingsRequestV1(_StrictReadingWireModel):
    """Bounded, paginated inspection of retained readings. Never writes."""

    tag: Literal["playbill-procedure-readings-request-v1"] = (
        "playbill-procedure-readings-request-v1"
    )
    measurement_names: tuple[str, ...] = ()
    run_id: str | None = None
    subject_grain: Literal["procedure_unit", "node", "arm"] | None = None
    evaluation_time: datetime | None = None
    at: AcceptedCoordinate | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @field_validator("measurement_names")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("measurement_names must be sorted and unique")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _evaluation_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else ensure_utc(value)

    @field_serializer("evaluation_time", when_used="json")
    def _serialize_evaluation_time(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class PlaybillProcedureReadingsResultV1(_StrictReadingWireModel):
    tag: Literal["playbill-procedure-readings-result-v1"] = "playbill-procedure-readings-result-v1"
    procedure_identity: ArtifactIdentity
    procedure_artifact_digest: str
    activation_coordinate: AcceptedCoordinate
    observation_coordinate: AcceptedCoordinate
    observation_time: datetime
    contracts: tuple[ProcedureMeasurementContractStatusV1, ...]
    readings: tuple[ProcedureReadingSummaryV1, ...]
    truncated: bool = False
    cursor: str | None = None

    @field_validator("procedure_artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        return cast(str, _digest(value))

    @field_validator("observation_time")
    @classmethod
    def _observation_time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_serializer("observation_time", when_used="json")
    def _serialize_observation_time(self, value: datetime) -> str | None:
        return format_datetime(value)


def measurement_repair_for_refusal(code: ProcedureMeasurementRefusalCodeV1) -> ServedRepairV1:
    return served_repair_for_refusal(code)


__all__ = [
    "PlaybillProcedureMeasureRequestV1",
    "PlaybillProcedureMeasureResultV1",
    "PlaybillProcedureReadingsRequestV1",
    "PlaybillProcedureReadingsResultV1",
    "ProcedureMeasurementContractStatusV1",
    "ProcedureMeasurementEligibilityV1",
    "ProcedureMeasurementRefusalCodeV1",
    "ProcedureMeasurementResolutionSummaryV1",
    "ProcedureMeasurementRowV1",
    "ProcedureMeasurementStatusV1",
    "ProcedureMeasurementVerdictV1",
    "ProcedureReadingStatusV1",
    "ProcedureReadingSummaryV1",
    "measurement_repair_for_refusal",
]
