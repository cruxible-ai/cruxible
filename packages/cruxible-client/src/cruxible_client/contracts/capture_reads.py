"""Bounded reads of retained Capture evidence; references grant no authority."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import CasDigest
from cruxible_client.contracts.captures import CaptureEnvelopeV1, CaptureEnvelopeV2
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.source_references import SourceDereferenceResultV1


class CaptureReadRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capture_digest: str
    at: AcceptedCoordinate | None = None
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=0)

    @field_validator("capture_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value


class CaptureReadV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-capture-read-v1"] = "playbill-capture-read-v1"
    capture_digest: str
    coordinate: AcceptedCoordinate
    status: Literal["verified", "unavailable"]
    reason: str | None = None
    envelope: CaptureEnvelopeV1 | CaptureEnvelopeV2 | None = None
    contract_address: str | None = None
    epistemic_grade: Literal["observed", "derived", "predicted"] | None = None
    citation_role: Literal["evidence", "copy"] | None = None
    material: SourceDereferenceResultV1 | None = None
