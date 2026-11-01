"""Typed, immutable provenance of values written by the Claims terminal."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    inputs: tuple[ArtifactPin, ...] = Field(min_length=1)
    transform_node_ids: tuple[str, ...] = ()


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
    value: EvidenceBasis


class ClaimsDiagnostic(_Receipt):
    kind: Literal["extract_refused", "incomplete_source", "held_by_precedence", "slot_contested"]
    target: str
    field: str | None = None
    problem: str | None = None
    lexeme: str | None = Field(default=None, max_length=512)
    input: str | None = None
    claim: str | None = None
    role: str | None = None


class ExtractRefusedInboxItem(_Receipt):
    """The core diagnostic inbox Contract; other outcomes stay in the run report."""

    tag: Literal["cruxible-extract-refused-inbox-v1"] = "cruxible-extract-refused-inbox-v1"
    kind: Literal["extract_refused"] = "extract_refused"
    target: str
    field: str
    problem: str
    lexeme: str | None = Field(default=None, max_length=512)
