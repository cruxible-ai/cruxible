"""Typed, immutable provenance of values written by the Claims terminal."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value


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


class ClaimEvidenceReceipt(_Receipt):
    law: Literal["claim-evidence-v2"] = "claim-evidence-v2"
    node_id: str
    template: int = Field(ge=0)
    procedure_digest: str
    _procedure_digest = field_validator("procedure_digest")(_digest)
    value: EvidenceBasis


FindingKind = Literal[
    "extract_refused",
    "duplicate_key",
    "incomplete_source",
    "held_disagrees",
    "slot_contested",
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
    key: dict[str, JsonValue] | None = None
    record_locators: tuple[str, ...] = Field(default=(), max_length=10)
    record_count: int = Field(default=0, ge=0)
    held_value: JsonValue = None
    incoming_value: JsonValue = None
