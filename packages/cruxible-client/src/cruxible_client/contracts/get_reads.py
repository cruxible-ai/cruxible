"""The ``get`` read: one governed thing by reference, values first.

A request names one reference in any form an agent sees (``CLM-…``, ``kind/id``,
a predicate, ``Document:<name>``, a proposal id, an artifact path, ...) and one
``detail`` level. The result leads with the values an agent asks for; digests
and the full accepted envelope sit behind ``detail="proof"``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts import PlaybillAcceptedCoordinate

PlaybillGetDetail = Literal["summary", "evidence", "why", "history", "proof", "body"]
PlaybillGetRefKind = Literal[
    "claim",
    "subject",
    "claim_type",
    "document",
    "procedure",
    "query",
    "capture_contract",
    "proposal",
]
# Verdict problems a row or card carries; derived from the verdict machinery,
# never re-adjudicated here.
PlaybillReadFlag = Literal["stale", "contested", "contradicted", "unsure_hold"]
# Which surface ``next`` suggestions are rendered for.
PlaybillReadSurface = Literal["mcp", "cli", "sdk"]

#: A Document body larger than this is never returned whole; pass a byte range.
GET_BODY_DEFAULT_MAX_BYTES = 64 * 1024
#: The widest byte range one ``get(detail="body")`` returns.
GET_BODY_RANGE_MAX_BYTES = 256 * 1024

_GIT_OID = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

# Which details apply to which kind of reference.
GET_DETAILS_BY_KIND: dict[str, tuple[str, ...]] = {
    "claim": ("summary", "evidence", "why", "history", "proof"),
    "subject": ("summary", "why", "history", "proof"),
    "claim_type": ("summary", "history", "proof"),
    "document": ("summary", "history", "proof", "body"),
    "procedure": ("summary", "history", "proof"),
    "query": ("summary", "history", "proof"),
    "capture_contract": ("summary", "history", "proof"),
    "proposal": ("summary", "proof"),
}


class _StrictGetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class PlaybillByteRangeV1(_StrictGetModel):
    """A half-open byte range ``[start, end)`` of a Document body."""

    start: int = Field(ge=0, description="First byte, zero-based.")
    end: int = Field(gt=0, description="One past the last byte.")

    @model_validator(mode="after")
    def _ordered(self) -> PlaybillByteRangeV1:
        if self.end <= self.start:
            raise ValueError("range end must be greater than start (for example 0:4096)")
        if self.end - self.start > GET_BODY_RANGE_MAX_BYTES:
            raise ValueError(
                f"range spans more than {GET_BODY_RANGE_MAX_BYTES} bytes; "
                f"read it in slices (for example 0:{GET_BODY_RANGE_MAX_BYTES})"
            )
        return self

    @classmethod
    def parse(cls, value: str) -> PlaybillByteRangeV1:
        """Read the CLI spelling ``start:end``."""

        start, separator, end = value.partition(":")
        if not separator or not start.isdigit() or not end.isdigit():
            raise ValueError("range must be start:end in bytes (for example 0:4096)")
        return cls(start=int(start), end=int(end))


class PlaybillGetRequestV1(_StrictGetModel):
    tag: Literal["playbill-get-request-v1"] = "playbill-get-request-v1"
    ref: str = Field(min_length=1, max_length=512)
    detail: PlaybillGetDetail = "summary"
    range: PlaybillByteRangeV1 | None = None
    at: PlaybillAcceptedCoordinate | str | None = None
    evaluation_time: datetime | None = None
    surface: PlaybillReadSurface = "mcp"

    @field_validator("at")
    @classmethod
    def _at(
        cls, value: PlaybillAcceptedCoordinate | str | None
    ) -> PlaybillAcceptedCoordinate | str | None:
        if isinstance(value, str) and not re.fullmatch(_GIT_OID, value):
            raise ValueError("at must be an accepted coordinate or a full lowercase git oid")
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("evaluation_time must be timezone-aware (for example ...Z)")
        return value

    @model_validator(mode="after")
    def _range_only_for_body(self) -> PlaybillGetRequestV1:
        if self.range is not None and self.detail != "body":
            raise ValueError('range applies only to detail="body"')
        return self


class PlaybillGetContenderV1(_StrictGetModel):
    claim: str
    value: Any
    verdict: str


class PlaybillGetClaimCardV1(_StrictGetModel):
    claim: str
    subject: str
    predicate: str
    predicate_full: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    value: Any
    verdict: str
    status: str
    revision: int
    accepted: str | None = Field(default=None, exclude_if=_omit_none)
    contenders: tuple[PlaybillGetContenderV1, ...] = ()
    flags: tuple[PlaybillReadFlag, ...] = ()
    next: tuple[str, ...] = ()


class PlaybillGetSubjectClaimV1(_StrictGetModel):
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    value: Any
    flags: tuple[PlaybillReadFlag, ...] = ()


class PlaybillGetSubjectCardV1(_StrictGetModel):
    subject: str
    kind: str
    lifecycle: str
    claims: tuple[PlaybillGetSubjectClaimV1, ...]
    incoming_count: int
    next: tuple[str, ...] = ()


class PlaybillGetClaimTypeCardV1(_StrictGetModel):
    predicate: str
    subject_kinds: tuple[str, ...]
    object: str
    cardinality: str
    members: tuple[Any, ...] | None = Field(default=None, exclude_if=_omit_none)
    description: str | None = Field(default=None, exclude_if=_omit_none)
    # Accepted evidence, as CaptureContract names; never a digest where the
    # contract's identity resolves. ``unresolved:<digest prefix>`` otherwise.
    evidence: tuple[str, ...]
    live_claims: int
    next: tuple[str, ...] = ()


class PlaybillGetDocumentCardV1(_StrictGetModel):
    document: str
    title: str
    document_kind: str
    media_type: str
    size: int | None
    revision: int
    next: tuple[str, ...] = ()


class PlaybillGetProcedureCardV1(_StrictGetModel):
    procedure: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    inputs: dict[str, Any]
    readiness: str
    required_slots: tuple[str, ...] = ()
    unsupported_nodes: int = 0
    next: tuple[str, ...] = ()


class PlaybillGetQueryParameterV1(_StrictGetModel):
    name: str
    type: str
    required: bool


class PlaybillGetQueryCardV1(_StrictGetModel):
    query: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    params: tuple[PlaybillGetQueryParameterV1, ...] = ()
    next: tuple[str, ...] = ()


class PlaybillGetCaptureContractCardV1(_StrictGetModel):
    contract: str
    version: int
    lifecycle: str
    captures: dict[str, Any]
    admitted_by: tuple[str, ...]
    next: tuple[str, ...] = ()


class PlaybillGetProposalChangeV1(_StrictGetModel):
    path: str
    change: str


class PlaybillGetProposalCardV1(_StrictGetModel):
    proposal: str
    status: str
    # Retained partial evidence: which records are missing, as the list says.
    incomplete: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    verdict: str | None = Field(default=None, exclude_if=_omit_none)
    reason: str | None = Field(default=None, exclude_if=_omit_none)
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    admitted_at: str | None = Field(default=None, exclude_if=_omit_none)
    rationale: str | None = Field(default=None, exclude_if=_omit_none)
    changes: tuple[PlaybillGetProposalChangeV1, ...] = ()
    next: tuple[str, ...] = ()


PlaybillGetCardV1 = (
    PlaybillGetClaimCardV1
    | PlaybillGetSubjectCardV1
    | PlaybillGetClaimTypeCardV1
    | PlaybillGetDocumentCardV1
    | PlaybillGetProcedureCardV1
    | PlaybillGetQueryCardV1
    | PlaybillGetCaptureContractCardV1
    | PlaybillGetProposalCardV1
)


class PlaybillGetCaptureEvidenceV1(_StrictGetModel):
    capture: str = Field(description="Capture digest prefix.")
    contract: str = Field(description="CaptureContract identity; never a digest.")
    version: int = Field(description="Accepted version of that contract the capture used.")
    source: str
    observed_at: datetime
    role: str
    admitted: bool


class PlaybillGetAttestationEvidenceV1(_StrictGetModel):
    stance: str
    principal: str
    at: datetime
    current: bool


class PlaybillGetEvidenceV1(_StrictGetModel):
    captures: tuple[PlaybillGetCaptureEvidenceV1, ...]
    attestations: tuple[PlaybillGetAttestationEvidenceV1, ...]
    rationale: str | None = Field(default=None, exclude_if=_omit_none)


class PlaybillGetRevisionV1(_StrictGetModel):
    revision: int
    sequence: int
    accepted: str
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    approved_by: tuple[str, ...] = ()
    lifecycle: str | None = Field(default=None, exclude_if=_omit_none)
    value: Any = Field(default=None, exclude_if=_omit_none)
    digest: str = Field(description="Artifact digest prefix of this revision.")


class PlaybillGetHistoryV1(_StrictGetModel):
    revisions: tuple[PlaybillGetRevisionV1, ...]


class PlaybillGetBodyV1(_StrictGetModel):
    document: str
    media_type: str
    size: int
    range: PlaybillByteRangeV1
    text: str | None = Field(default=None, exclude_if=_omit_none)
    content_base64: str | None = Field(default=None, exclude_if=_omit_none)


class PlaybillGetResultV1(_StrictGetModel):
    tag: Literal["playbill-get-result-v1"] = "playbill-get-result-v1"
    ref: str
    kind: PlaybillGetRefKind
    detail: PlaybillGetDetail
    card: PlaybillGetCardV1 | None = Field(default=None, exclude_if=_omit_none)
    evidence: PlaybillGetEvidenceV1 | None = Field(default=None, exclude_if=_omit_none)
    history: PlaybillGetHistoryV1 | None = Field(default=None, exclude_if=_omit_none)
    body: PlaybillGetBodyV1 | None = Field(default=None, exclude_if=_omit_none)
    # Today's explain output, unchanged.
    why: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    # Today's full accepted envelope, unchanged.
    proof: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: datetime


__all__ = [
    "GET_BODY_DEFAULT_MAX_BYTES",
    "GET_BODY_RANGE_MAX_BYTES",
    "GET_DETAILS_BY_KIND",
    "PlaybillByteRangeV1",
    "PlaybillGetAttestationEvidenceV1",
    "PlaybillGetBodyV1",
    "PlaybillGetCaptureContractCardV1",
    "PlaybillGetCaptureEvidenceV1",
    "PlaybillGetCardV1",
    "PlaybillGetClaimCardV1",
    "PlaybillGetClaimTypeCardV1",
    "PlaybillGetContenderV1",
    "PlaybillGetDetail",
    "PlaybillGetDocumentCardV1",
    "PlaybillGetEvidenceV1",
    "PlaybillGetHistoryV1",
    "PlaybillGetProcedureCardV1",
    "PlaybillGetProposalCardV1",
    "PlaybillGetProposalChangeV1",
    "PlaybillGetQueryCardV1",
    "PlaybillGetQueryParameterV1",
    "PlaybillGetRefKind",
    "PlaybillGetRequestV1",
    "PlaybillGetResultV1",
    "PlaybillGetRevisionV1",
    "PlaybillGetSubjectCardV1",
    "PlaybillGetSubjectClaimV1",
    "PlaybillReadFlag",
    "PlaybillReadSurface",
]
