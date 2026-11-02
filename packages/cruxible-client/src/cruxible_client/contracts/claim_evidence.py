"""Typed, immutable provenance of values written by the Claims terminal."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.projection import AcceptedCoordinate


def _digest(value: str) -> str:
    Sha256Value.from_tagged(value)
    return value


class _Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractedBasis(_Receipt):
    basis: Literal["extracted"] = "extracted"
    capture_digest: str
    extract_spec_digest: str
    extractor_version: str
    record_locator: str
    field_path: str
    record_key: dict[str, JsonValue] = Field(default_factory=dict)
    transform_node_ids: tuple[str, ...] = ()

    @field_validator("capture_digest", "extract_spec_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class CaptureBasis(_Receipt):
    basis: Literal["capture"] = "capture"
    capture_digest: str
    _digest = field_validator("capture_digest")(_digest)


class DerivedBasis(_Receipt):
    basis: Literal["derived"] = "derived"
    inputs: tuple[ArtifactPin, ...] = ()
    extractions: tuple[ExtractedBasis, ...] = ()
    transform_node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _sources(self) -> DerivedBasis:
        if not self.inputs and not self.extractions:
            raise ValueError("derived values require exact Claim inputs or extraction receipts")
        return self


class MeasuredBasis(_Receipt):
    basis: Literal["measured"] = "measured"
    capture_digest: str
    operation: str
    build: ArtifactPin
    enumerates: str | None = None
    declared_fields: dict[str, str] = Field(default_factory=dict)
    field_path: str | None = None
    transform_node_ids: tuple[str, ...] = ()
    _digest = field_validator("capture_digest")(_digest)


EvidenceBasis = Annotated[
    ExtractedBasis | CaptureBasis | DerivedBasis | MeasuredBasis, Field(discriminator="basis")
]


class CompleteWhenResult(_Receipt):
    count_at: str
    expected: int | None
    observed: int = Field(ge=0)
    held: bool

    @model_validator(mode="after")
    def _counts(self) -> CompleteWhenResult:
        if self.held and self.expected != self.observed:
            raise ValueError("complete_when result does not match the enumerated count")
        return self


class AbsenceWitness(_Receipt):
    run_id: str
    procedure_digest: str
    node_id: str
    capture: str
    extract_spec: str
    extractor: str
    logical_source: str
    #: Present only when the extract declares ``complete_when``: optional extra
    #: proof. Completeness itself is the absence of observed incompleteness.
    complete_when: CompleteWhenResult | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    enumerated_key_digest: str
    expect_coordinate: AcceptedCoordinate

    _digests = field_validator(
        "capture", "extract_spec", "enumerated_key_digest", "procedure_digest"
    )(_digest)

    @model_validator(mode="after")
    def _complete(self) -> AbsenceWitness:
        if self.complete_when is not None and not self.complete_when.held:
            raise ValueError("absence witness requires a complete enumeration")
        return self


class ClaimEvidenceReceipt(_Receipt):
    law: Literal["claim-evidence-v2"] = "claim-evidence-v2"
    node_id: str
    template: int = Field(ge=0)
    procedure_digest: str
    _procedure_digest = field_validator("procedure_digest")(_digest)
    value: EvidenceBasis
    absence: AbsenceWitness | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _absence_capture(self) -> ClaimEvidenceReceipt:
        if self.absence is not None and (
            not isinstance(self.value, CaptureBasis)
            or self.value.capture_digest != self.absence.capture
        ):
            raise ValueError("absence receipt must bind the witness's exact Capture")
        return self


FindingKind = Literal[
    "extract_refused",
    "duplicate_key",
    "incomplete_source",
    "held_disagrees",
    "slot_contested",
    "claim_not_in_source",
]


class ClaimsDiagnostic(_Receipt):
    kind: FindingKind
    target: str
    field: str | None = None
    problem: str | None = None
    lexeme: str | None = Field(default=None, max_length=512)
    input: str | None = None
    claim: str | None = None
    role: str | None = None
    subject: str | None = Field(default=None, exclude_if=lambda value: value is None)
    claim_type: str | None = Field(default=None, exclude_if=lambda value: value is None)
    qualifier: JsonValue = Field(default=None, exclude_if=lambda value: value is None)
    logical_source: str | None = Field(default=None, exclude_if=lambda value: value is None)
    capture: str | None = Field(default=None, exclude_if=lambda value: value is None)
    extract_spec: str | None = Field(default=None, exclude_if=lambda value: value is None)
    reason: Literal["record_absent", "field_absent"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    source_incomplete: bool = Field(default=False, exclude_if=lambda value: not value)
    key: dict[str, JsonValue] | None = None
    record_locators: tuple[str, ...] = Field(default=(), max_length=10)
    record_count: int = Field(default=0, ge=0)
    held_value: JsonValue = None
    incoming_value: JsonValue = None

    @model_validator(mode="after")
    def _absence_finding(self) -> ClaimsDiagnostic:
        if self.kind == "claim_not_in_source" and any(
            value is None
            for value in (
                self.claim,
                self.subject,
                self.claim_type,
                self.logical_source,
                self.capture,
                self.extract_spec,
                self.reason,
            )
        ):
            raise ValueError(
                "claim_not_in_source requires a Claim and its complete absence attribution"
            )
        return self


CompletenessReasonKind = Literal[
    "incomplete_input",
    "count_mismatch",
    "invalid_record",
    "duplicate_key",
    "invalid_field",
    "unknown_predicate",
    "may_truncate",
    "control_incomplete",
    "upstream_incomplete",
]


class CompletenessReason(_Receipt):
    node: str
    kind: CompletenessReasonKind
    inputs: tuple[str, ...] = Field(default=(), max_length=20)


class RunCompleteness(_Receipt):
    complete: Literal[False] = False
    reasons: tuple[CompletenessReason, ...] = Field(max_length=20)
    downgraded: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
