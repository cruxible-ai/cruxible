"""Typed graph-v3 declarations for future ResolutionContract activation.

A declaration is accepted Procedure intent, not operational exhaust.  It fixes
the exact question, expected answer, subject grain, and acceptance-relative
time window before any observation exists.  PC-E1 may derive activations from
these records; it must never infer them from nodes or ``annotations``.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
)
from cruxible_core.playbill.captures import CanonicalDurationV1
from cruxible_core.playbill.errors import CanonicalEncodingError
from cruxible_core.playbill.semantic import SemanticAddress

_MEASUREMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_ARTIFACT_KIND_RE = re.compile(r"^[A-Z][A-Za-z0-9_.-]{0,63}$")

ProcedureMeasurementSubjectGrainV1 = Literal["procedure_unit", "node", "arm"]
StringT = TypeVar("StringT", bound=str)


class _StrictMeasurementModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_name(value: str, *, label: str) -> str:
    if not _MEASUREMENT_NAME_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    return value


def _sorted_unique(
    value: tuple[StringT, ...],
    *,
    label: str,
    nonempty: bool = True,
) -> tuple[StringT, ...]:
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return value


def _canonical_object(value: object, *, label: str) -> dict[str, CanonicalValue]:
    try:
        normalized = normalize_canonical(value)
    except CanonicalEncodingError as exc:
        raise ValueError(f"{label} is not canonical: {exc}") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} must be a canonical object")
    return normalized


def _canonical_ratio(value: object, *, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or tuple(value) != ("$decimal",):
        raise ValueError(f"{label} must be a canonical $decimal wrapper")
    spelling = value["$decimal"]
    if not isinstance(spelling, str):
        raise ValueError(f"{label} decimal must be text")
    try:
        decimal = Decimal(spelling)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not a decimal") from exc
    if not decimal.is_finite() or decimal < 0 or decimal > 1:
        raise ValueError(f"{label} must be in [0,1]")
    canonical = format(decimal, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    if canonical in {"", "-0"}:
        canonical = "0"
    if spelling != canonical:
        raise ValueError(f"{label} decimal spelling is not canonical")
    return {"$decimal": spelling}


class ProcedureMeasurementExpectationV1(_StrictMeasurementModel):
    """Closed point-in-time count and property-equality expectation."""

    tag: Literal["playbill-procedure-measurement-expectation-v1"] = (
        "playbill-procedure-measurement-expectation-v1"
    )
    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    condition: object | None = None
    condition_scope: Literal["any", "all"] = "all"

    @field_validator("condition", mode="before")
    @classmethod
    def _condition(cls, value: object | None) -> object | None:
        if value is None:
            return None
        normalized = _canonical_object(value, label="measurement condition")
        if not normalized:
            raise ValueError("measurement condition must not be empty")
        return normalized

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureMeasurementExpectationV1":
        if self.min_count is None and self.max_count is None and self.condition is None:
            raise ValueError("measurement expectation must constrain count or condition")
        if (
            self.min_count is not None
            and self.max_count is not None
            and self.min_count > self.max_count
        ):
            raise ValueError("measurement min_count must be <= max_count")
        if self.condition is None and self.condition_scope != "all":
            raise ValueError("condition_scope must be 'all' when condition is absent")
        if self.condition is not None and self.condition_scope == "all":
            if self.min_count is None or self.min_count < 1:
                raise ValueError(
                    "an all-scoped condition requires min_count >= 1 to avoid vacuous satisfaction"
                )
        return self


class AcceptedQueryProcedureMeasurementV1(_StrictMeasurementModel):
    """Evaluate one exact accepted QueryDefinition with fixed options."""

    tag: Literal["playbill-procedure-accepted-query-measurement-v1"] = (
        "playbill-procedure-accepted-query-measurement-v1"
    )
    kind: Literal["accepted_query"] = "accepted_query"
    query: ArtifactPin
    parameters: object = Field(default_factory=dict)
    execution_options: object = Field(default_factory=dict)
    expect: ProcedureMeasurementExpectationV1

    @field_validator("parameters", "execution_options", mode="before")
    @classmethod
    def _objects(cls, value: object, info: object) -> object:
        return _canonical_object(
            value,
            label=f"accepted-query {getattr(info, 'field_name', 'value')}",
        )

    @model_validator(mode="after")
    def _exact_query(self) -> "AcceptedQueryProcedureMeasurementV1":
        if self.query.role != "query" or self.query.target.kind != "QueryDefinition":
            raise ValueError(
                "accepted-query measurement requires an exact role='query' "
                "kind='QueryDefinition' pin"
            )
        return self


class ClaimAttestationProcedureMeasurementV1(_StrictMeasurementModel):
    """Count exact-subject ClaimAttestations with declared stances."""

    tag: Literal["playbill-procedure-claim-attestation-measurement-v1"] = (
        "playbill-procedure-claim-attestation-measurement-v1"
    )
    kind: Literal["claim_attestation"] = "claim_attestation"
    claim_statement: SemanticAddress
    claim_statement_digest: str
    stances: tuple[Literal["support", "contradict", "unsure"], ...]
    expect: ProcedureMeasurementExpectationV1

    @field_validator("claim_statement_digest")
    @classmethod
    def _statement_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("stances")
    @classmethod
    def _stances(
        cls,
        value: tuple[Literal["support", "contradict", "unsure"], ...],
    ) -> tuple[Literal["support", "contradict", "unsure"], ...]:
        return _sorted_unique(value, label="ClaimAttestation measurement stances")

    @model_validator(mode="after")
    def _statement_subject(self) -> "ClaimAttestationProcedureMeasurementV1":
        if self.claim_statement.selector.scheme != "claim-statement-v1":
            raise ValueError("ClaimAttestation measurement requires a Claim statement address")
        return self


class ClaimStatementProcedureMeasurementV1(_StrictMeasurementModel):
    """Test one exact accepted Claim statement's evidence-relative verdict."""

    tag: Literal["playbill-procedure-claim-statement-measurement-v1"] = (
        "playbill-procedure-claim-statement-measurement-v1"
    )
    kind: Literal["claim_statement"] = "claim_statement"
    claim_statement: SemanticAddress
    claim_statement_digest: str
    acceptable_verdicts: tuple[
        Literal["supported", "uncovered", "stale", "contradicted", "unresolved"],
        ...,
    ]

    @field_validator("claim_statement_digest")
    @classmethod
    def _statement_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("acceptable_verdicts")
    @classmethod
    def _verdicts(
        cls,
        value: tuple[
            Literal["supported", "uncovered", "stale", "contradicted", "unresolved"],
            ...,
        ],
    ) -> tuple[
        Literal["supported", "uncovered", "stale", "contradicted", "unresolved"],
        ...,
    ]:
        return _sorted_unique(value, label="Claim statement acceptable verdicts")

    @model_validator(mode="after")
    def _statement_subject(self) -> "ClaimStatementProcedureMeasurementV1":
        if self.claim_statement.selector.scheme != "claim-statement-v1":
            raise ValueError("Claim statement measurement requires a Claim statement address")
        return self


ProcedureMeasurementV1 = Annotated[
    AcceptedQueryProcedureMeasurementV1
    | ClaimAttestationProcedureMeasurementV1
    | ClaimStatementProcedureMeasurementV1,
    Field(discriminator="kind"),
]


class ProcedureMeasurementSituationShapeV1(_StrictMeasurementModel):
    """Coarse reproducible context, excluding task IDs and answer content."""

    tag: Literal["playbill-procedure-measurement-situation-shape-v1"] = (
        "playbill-procedure-measurement-situation-shape-v1"
    )
    subject_kinds: tuple[str, ...] = ()
    task_category: str | None = None
    tags: tuple[str, ...] = ()

    @field_validator("subject_kinds")
    @classmethod
    def _subject_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not _ARTIFACT_KIND_RE.fullmatch(item):
                raise ValueError("situation subject_kinds must be canonical artifact kinds")
        return _sorted_unique(value, label="situation subject_kinds", nonempty=False)

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _canonical_name(item, label="situation tag")
        return _sorted_unique(value, label="situation tags", nonempty=False)

    @field_validator("task_category")
    @classmethod
    def _task_category(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_name(value, label="task_category")


class ProcedureMeasurementReviewTriggerV1(_StrictMeasurementModel):
    """A deterministic threshold that can later request calibration review."""

    tag: Literal["playbill-procedure-measurement-review-trigger-v1"] = (
        "playbill-procedure-measurement-review-trigger-v1"
    )
    name: str
    metric: Literal["contradicted_rate", "satisfied_rate", "arm_contrast"]
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte"]
    threshold: object
    min_readings: int = Field(ge=1)
    window: CanonicalDurationV1 | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, label="measurement review trigger name")

    @field_validator("threshold", mode="before")
    @classmethod
    def _threshold(cls, value: object) -> object:
        return _canonical_ratio(value, label="measurement review threshold")

    @model_validator(mode="after")
    def _window(self) -> "ProcedureMeasurementReviewTriggerV1":
        if self.window is not None and self.window.microseconds == 0:
            raise ValueError("measurement review window must be nonzero")
        return self


class ProcedureMeasurementDeclarationV1(_StrictMeasurementModel):
    """One digest-covered declaration from which PC-E1 may derive activation."""

    tag: Literal["playbill-procedure-measurement-declaration-v1"] = (
        "playbill-procedure-measurement-declaration-v1"
    )
    name: str
    subject_grain: ProcedureMeasurementSubjectGrainV1
    node_id: str | None = None
    from_node_id: str | None = None
    arm_label: Literal["on_true", "on_false"] | None = None
    measurement: ProcedureMeasurementV1
    check_after: CanonicalDurationV1
    expires_after: CanonicalDurationV1
    situation_shape: ProcedureMeasurementSituationShapeV1 | None = None
    review_when: tuple[ProcedureMeasurementReviewTriggerV1, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _refuse_self_measurement(cls, value: object) -> object:
        if isinstance(value, dict):
            measurement = value.get("measurement")
            if isinstance(measurement, dict) and measurement.get("kind") in {
                "procedure",
                "procedure_reading",
            }:
                raise ValueError("M5: a Procedure measurement cannot use its own later reading")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _canonical_name(value, label="Procedure measurement name")

    @field_validator("review_when")
    @classmethod
    def _review_when(
        cls,
        value: tuple[ProcedureMeasurementReviewTriggerV1, ...],
    ) -> tuple[ProcedureMeasurementReviewTriggerV1, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("measurement review triggers must be sorted and unique by name")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ProcedureMeasurementDeclarationV1":
        if self.subject_grain == "procedure_unit":
            if any(
                value is not None for value in (self.node_id, self.from_node_id, self.arm_label)
            ):
                raise ValueError(
                    "procedure_unit measurements require node and arm coordinates null"
                )
        elif self.subject_grain == "node":
            if self.node_id is None:
                raise ValueError("M1: node measurement requires node_id")
            if self.from_node_id is not None or self.arm_label is not None:
                raise ValueError("node measurement requires arm coordinates null")
        else:
            if self.node_id is None:
                raise ValueError("M1: arm measurement requires target node_id")
            if self.from_node_id is None or self.arm_label is None:
                raise ValueError("M2: arm measurement requires from_node_id and arm_label")
        if self.expires_after.microseconds == 0:
            raise ValueError("measurement expires_after must be nonzero")
        if self.check_after.microseconds >= self.expires_after.microseconds:
            raise ValueError("measurement check_after must be less than expires_after")
        if any(item.metric == "arm_contrast" for item in self.review_when):
            if self.subject_grain != "arm":
                raise ValueError("M4: arm_contrast requires subject_grain='arm'")
        return self


__all__ = [
    "AcceptedQueryProcedureMeasurementV1",
    "ClaimAttestationProcedureMeasurementV1",
    "ClaimStatementProcedureMeasurementV1",
    "ProcedureMeasurementDeclarationV1",
    "ProcedureMeasurementExpectationV1",
    "ProcedureMeasurementReviewTriggerV1",
    "ProcedureMeasurementSituationShapeV1",
    "ProcedureMeasurementSubjectGrainV1",
    "ProcedureMeasurementV1",
]
