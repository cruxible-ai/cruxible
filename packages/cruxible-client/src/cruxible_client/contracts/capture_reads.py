"""Bounded reads of retained Capture evidence; references grant no authority."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


#: The permission a restricted CaptureKind's bytes are read under (C14).
BODY_READ_RESTRICTED_PERMISSION = "cruxible_body_read_restricted"


class BodyRestricted(BaseModel):
    """Bytes of restricted CaptureKinds were withheld from this caller (C14).

    A restricted kind's bytes, its envelopes' source and selector metadata, the
    spans cited from them, and values extracted from them into run outputs and
    inbox items are read only with ``cruxible_body_read_restricted``. The
    withheld Captures are named by digest, each with its kind; accepted Claim
    values are never withheld.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["cruxible-body-restricted-v1"] = "cruxible-body-restricted-v1"
    kind: Literal["body_restricted"] = "body_restricted"
    #: The withheld Captures; empty only with ``provenance_unavailable``.
    capture_digests: tuple[str, ...] = ()
    #: The restricted CaptureKinds, by qualified identity, sorted; a kind that
    #: no accepted version resolves is withheld as restricted and not named.
    capture_kinds: tuple[str, ...] = ()
    #: A run whose record of the Captures it consumed is unreadable is withheld
    #: whole (fail closed) and says so here.
    provenance_unavailable: bool = Field(default=False, exclude_if=lambda value: not value)
    required_permission: Literal["cruxible_body_read_restricted"] = "cruxible_body_read_restricted"
    required_tier: Literal["governed_write"] = "governed_write"

    @field_validator("capture_digests", "capture_kinds")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("withheld Captures and kinds are sorted and unique")
        return value

    @model_validator(mode="after")
    def _names_what(self) -> BodyRestricted:
        if not self.capture_digests and not self.provenance_unavailable:
            raise ValueError("a body_restricted notice names a Capture or unavailable provenance")
        return self


class CaptureRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-capture-read-v1"] = "playbill-capture-read-v1"
    capture_digest: str
    coordinate: AcceptedCoordinate
    #: ``restricted``: the inventory view of a restricted CaptureKind's Capture
    #: for a caller without ``cruxible_body_read_restricted``; ``notice`` says so.
    status: Literal["verified", "unavailable", "restricted"]
    #: Why it is unavailable: ``capture_unavailable`` (the envelope is not held),
    #: ``contract_not_at_coordinate``, ``body_unavailable`` (the body is not held),
    #: or ``body_erased`` (its body was erased; ``erasure`` says by whom and why);
    #: ``body_restricted`` when it is withheld.
    reason: str | None = None
    erasure: CaptureErasureRecord | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    notice: BodyRestricted | None = Field(default=None, exclude_if=lambda value: value is None)
    envelope: CaptureEnvelopeV1 | CaptureEnvelope | None = None
    contract_address: str | None = None
    epistemic_grade: Literal["observed", "derived", "predicted"] | None = None
    citation_role: Literal["evidence", "copy"] | None = None
    material: SourceDereferenceResult | None = None
