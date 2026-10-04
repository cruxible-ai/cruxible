"""Bounded reads of retained Capture evidence; references grant no authority."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import CasDigest
from cruxible_client.contracts.captures import CaptureEnvelope, CaptureEnvelopeV1
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.source_references import SourceDereferenceResult

#: A Capture named by the handle a card prints (``CAP-`` plus 12+ hex) or a
#: digest prefix of 12+ hex; the daemon resolves it as the write verbs do: among
#: Captures accepted Claims cite and retained ones that verify.
_CAPTURE_PREFIX = re.compile(r"^(?:CAP-|sha256:)[0-9a-f]{12,63}$")


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
        if _CAPTURE_PREFIX.fullmatch(value):
            return value
        CasDigest.from_tagged(value)
        return value


class CaptureRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-capture-read-v1"] = "playbill-capture-read-v1"
    capture_digest: str
    coordinate: AcceptedCoordinate
    status: Literal["verified", "unavailable"]
    reason: str | None = None
    envelope: CaptureEnvelopeV1 | CaptureEnvelope | None = None
    contract_address: str | None = None
    epistemic_grade: Literal["observed", "derived", "predicted"] | None = None
    citation_role: Literal["evidence", "copy"] | None = None
    material: SourceDereferenceResult | None = None
