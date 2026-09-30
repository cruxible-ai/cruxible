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
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimTypeMemberDescriptionV1,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.operational_reads import (
    PlaybillGetCaptureCardV1,
    PlaybillGetLineCardV1,
    PlaybillGetMandateCardV1,
    PlaybillGetProcedureRunCardV1,
    PlaybillGetResolutionContractCardV1,
    PlaybillLiveViewV1,
)

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
    "line",
    "capture",
    "resolution_contract",
    "mandate",
    "procedure_run",
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
#: A summary card shows at most this many characters of one string value.
GET_SUMMARY_TEXT_MAX_CHARS = 500
#: Revisions per ``get(detail="history")`` page, by default and at most.
GET_HISTORY_DEFAULT_LIMIT = 20
GET_HISTORY_MAX_LIMIT = 200

# A git oid or a prefix of one; the read resolves it (at least 12 hex, unique).
_GIT_OID = r"^[0-9a-f]{1,64}$"

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
    "line": ("summary", "history", "proof"),
    "capture": ("summary", "proof"),
    "resolution_contract": ("summary", "history", "proof"),
    "mandate": ("summary", "history", "proof"),
    "procedure_run": ("summary", "proof"),
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
    # ``detail="history"`` pages: revisions per page (default 20) and the
    # opaque ``next_cursor`` of the page before.
    limit: int | None = Field(default=None, ge=1, le=GET_HISTORY_MAX_LIMIT)
    cursor: str | None = Field(default=None, min_length=1)
    # Also answer the full four-digest accepted coordinate, which only
    # ``detail="proof"`` carries otherwise (the SDK pins reads with it).
    full_coordinate: bool = False

    @field_validator("at")
    @classmethod
    def _at(
        cls, value: PlaybillAcceptedCoordinate | str | None
    ) -> PlaybillAcceptedCoordinate | str | None:
        if isinstance(value, str) and not re.fullmatch(_GIT_OID, value):
            raise ValueError(
                "at must be an accepted coordinate, a lowercase hex git oid (a unique "
                "prefix of at least 12 characters), or a generation number (for example 42)"
            )
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
        if (self.limit is not None or self.cursor is not None) and self.detail != "history":
            raise ValueError('limit and cursor page detail="history" only')
        return self


class PlaybillGetTruncatedTextV1(_StrictGetModel):
    """A long string value cut short on a summary card.

    ``value`` is its first ``GET_SUMMARY_TEXT_MAX_CHARS`` characters and
    ``length`` its whole length; ``detail="evidence"`` or ``"proof"`` reads the
    whole value.
    """

    value: str
    truncated: Literal[True] = True
    length: int = Field(gt=GET_SUMMARY_TEXT_MAX_CHARS)


def summary_value(value: Any) -> Any:
    """A value as a summary card shows it: long strings cut, lists element-wise."""

    if isinstance(value, str) and len(value) > GET_SUMMARY_TEXT_MAX_CHARS:
        return PlaybillGetTruncatedTextV1(
            value=value[:GET_SUMMARY_TEXT_MAX_CHARS], length=len(value)
        )
    if isinstance(value, list | tuple):
        return [summary_value(item) for item in value]
    return value


class PlaybillExactContentRefV1(_StrictGetModel):
    """An exact-content Claim value shown by digest, because it cannot be shown as text.

    An exact-content value reads as its UTF-8 text wherever a value is shown.
    This marker stands in for the text when there is none to show: the bytes are
    not UTF-8 text (``binary``), or the store no longer holds them
    (``unavailable``). It never raises. Every caller who may read a Claim reads
    its exact-content value; nothing is withheld by permission.
    """

    exact_content: Literal["binary", "unavailable"]
    content_digest: str
    # The value's length in bytes, when it is known without reading the bytes
    # (the accepted span) or they were read.
    length: int | None = Field(default=None, ge=0, exclude_if=_omit_none)


class PlaybillGetCoordinateV1(_StrictGetModel):
    """Which accepted generation answered: the git oid's 12-hex prefix and its sequence."""

    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    generation: int = Field(ge=0)


class PlaybillGetContenderV1(_StrictGetModel):
    claim: str
    value: Any
    # An exact-content value's digest; its text is ``value``.
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    verdict: str


class PlaybillGetClaimCardV1(_StrictGetModel):
    claim: str
    subject: str
    predicate: str
    predicate_full: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    value: Any
    # An exact-content value's digest, the proof its text is ``value``.
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
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
    # The Claim behind the value; a list, aligned with ``value``, when the row
    # shows several (a many-valued predicate or a contested slot).
    claim: str | tuple[str, ...]
    value: Any
    # An exact-content row's digests, aligned with ``claim`` the same way.
    content_digest: str | tuple[str, ...] | None = Field(default=None, exclude_if=_omit_none)
    flags: tuple[PlaybillReadFlag, ...] = ()


class PlaybillGetSubjectCardV1(_StrictGetModel):
    subject: str
    kind: str
    lifecycle: str
    claims: tuple[PlaybillGetSubjectClaimV1, ...]
    incoming_count: int
    next: tuple[str, ...] = ()


class PlaybillGetEvidenceRuleV1(_StrictGetModel):
    """One evidence rule: which roles it admits evidence for, under which contracts."""

    rule_id: str
    roles: tuple[ClaimRole, ...]
    contracts: tuple[str, ...]
    admission: Literal["origin_only", "direct", "derivational"]


class PlaybillGetClaimTypeCardV1(_StrictGetModel):
    predicate: str
    subject_kinds: tuple[str, ...]
    object: str
    cardinality: str
    members: tuple[Any, ...] | None = Field(default=None, exclude_if=_omit_none)
    description: str | None = Field(default=None, exclude_if=_omit_none)
    # Each described enum member, as its literal value beside what it means.
    member_descriptions: tuple[ClaimTypeMemberDescriptionV1, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    roles: tuple[ClaimRole, ...]
    # The role a write takes when it names none.
    default_role: ClaimRole | None = Field(default=None, exclude_if=_omit_none)
    # What backs a Claim (``self`` before v7) and what a statement-changing
    # revision keeps (``accumulate`` before v7).
    evidence_requirement: EvidenceRequirement
    revision_evidence: RevisionEvidence
    # Accepted evidence, as CaptureContract names; never a digest where the
    # contract's identity resolves. ``unresolved:<digest prefix>`` otherwise.
    evidence: tuple[str, ...]
    evidence_rules: tuple[PlaybillGetEvidenceRuleV1, ...] = ()
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
    | PlaybillGetLineCardV1
    | PlaybillGetCaptureCardV1
    | PlaybillGetResolutionContractCardV1
    | PlaybillGetMandateCardV1
    | PlaybillGetProcedureRunCardV1
)


class PlaybillGetCaptureEvidenceV1(_StrictGetModel):
    capture: str = Field(
        description="Capture handle, CAP- plus the digest's first 12 hex; get and read_capture "
        "accept it."
    )
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
    # The Claim's whole value, never cut as a summary card cuts a long one.
    value: Any
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    captures: tuple[PlaybillGetCaptureEvidenceV1, ...]
    attestations: tuple[PlaybillGetAttestationEvidenceV1, ...]
    rationale: str | None = Field(default=None, exclude_if=_omit_none)


class PlaybillGetRevisionV1(_StrictGetModel):
    revision: int
    sequence: int
    # The accepted generation's git oid (12-hex prefix): pass it, or the
    # sequence, back as ``at`` to read at that revision.
    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    accepted: str
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    approved_by: tuple[str, ...] = ()
    lifecycle: str | None = Field(default=None, exclude_if=_omit_none)
    value: Any = Field(default=None, exclude_if=_omit_none)
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    digest: str = Field(description="Artifact digest prefix of this revision.")
    # Read a cut value in full at this revision's accepted generation.
    next: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class PlaybillGetHistoryV1(_StrictGetModel):
    """One page of revisions, newest first; ``revision`` counts from the oldest.

    Each revision's value follows the summary card rule: a string over 500
    characters is cut to ``{value, truncated: true, length}``.
    """

    revisions: tuple[PlaybillGetRevisionV1, ...]


class PlaybillGetBodyV1(_StrictGetModel):
    document: str
    media_type: str
    size: int
    # The bytes returned; absent for an empty Document, which has none.
    range: PlaybillByteRangeV1 | None = None
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
    # ``detail="history"`` paging; absent on every other detail.
    truncated: bool | None = Field(default=None, exclude_if=_omit_none)
    next_cursor: str | None = Field(default=None, exclude_if=_omit_none)
    coordinate: PlaybillGetCoordinateV1
    # The full accepted coordinate: under ``detail="proof"``, or when asked for.
    accepted_coordinate: PlaybillAcceptedCoordinate | None = Field(
        default=None, exclude_if=_omit_none
    )
    # Present when part of the answer is operational state, read live at the
    # current head (``live.as_of``) whatever ``coordinate`` the read named.
    live: PlaybillLiveViewV1 | None = Field(default=None, exclude_if=_omit_none)
    evaluation_time: datetime


__all__ = [
    "GET_BODY_DEFAULT_MAX_BYTES",
    "GET_BODY_RANGE_MAX_BYTES",
    "GET_DETAILS_BY_KIND",
    "GET_HISTORY_DEFAULT_LIMIT",
    "GET_HISTORY_MAX_LIMIT",
    "GET_SUMMARY_TEXT_MAX_CHARS",
    "PlaybillByteRangeV1",
    "PlaybillExactContentRefV1",
    "PlaybillGetAttestationEvidenceV1",
    "PlaybillGetBodyV1",
    "PlaybillGetCaptureContractCardV1",
    "PlaybillGetCaptureEvidenceV1",
    "PlaybillGetCardV1",
    "PlaybillGetClaimCardV1",
    "PlaybillGetClaimTypeCardV1",
    "PlaybillGetEvidenceRuleV1",
    "PlaybillGetContenderV1",
    "PlaybillGetCoordinateV1",
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
    "PlaybillGetTruncatedTextV1",
    "PlaybillReadFlag",
    "PlaybillReadSurface",
    "summary_value",
]
