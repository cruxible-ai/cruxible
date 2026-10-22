"""Bounded reads of retained Capture evidence; references grant no authority."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.capture_erasure import CaptureErasureRecord, validate_capture_ref
from cruxible_client.contracts.captures import CaptureEnvelope, CaptureEnvelopeV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.source_references import SourceDereferenceResult


class CaptureReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    capture_digest: str = Field(
        description=(
            "The Capture's full digest (sha256:<64 hex>), or the CAP-<12+ hex> handle a get "
            "card prints, or a sha256:<12+ hex> prefix; a prefix must name one accepted Capture."
        )
    )
    at: AcceptedCoordinate | None = None
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=0)

    @field_validator("capture_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        return validate_capture_ref(value)


class CaptureRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-capture-read-v1"] = "playbill-capture-read-v1"
    capture_digest: str
    coordinate: AcceptedCoordinate
    status: Literal["verified", "unavailable"]
    #: Why it is unavailable: ``capture_unavailable`` (the envelope is not held),
    #: ``contract_not_at_coordinate``, ``body_unavailable`` (the body is not held),
    #: or ``body_erased`` (its body was erased; ``erasure`` says by whom and why).
    reason: str | None = None
    erasure: CaptureErasureRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    envelope: CaptureEnvelopeV1 | CaptureEnvelope | None = None
    contract_address: str | None = None
    epistemic_grade: Literal["observed", "derived", "predicted"] | None = None
    citation_role: Literal["evidence", "copy"] | None = None
    material: SourceDereferenceResult | None = None
