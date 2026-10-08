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

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimTypeMemberDescription,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.operational_reads import (
    GetCaptureCard,
    GetLineCard,
    GetMandateCard,
    GetProcedureRunCard,
    GetResolutionContractCard,
    LiveView,
)
from cruxible_client.contracts.read_values import ShownValue
from cruxible_client.contracts.repairs import ServedRepair

GetDetail = Literal["summary", "evidence", "why", "history", "proof", "body"]
GetRefKind = Literal[
    "claim",
    "subject",
    "claim_type",
    "document",
    "procedure",
    "blueprint",
    "query",
    "capture_contract",
    "trigger",
    "proposal",
    "line",
    "capture",
    "resolution_contract",
    "mandate",
    "procedure_run",
    "principal",
    "approval_policy",
    "procedure_runtime_policy",
    "source_acquisition_policy",
    "provider_interface",
]
# Verdict problems a row or card carries; derived from the verdict machinery,
# never re-adjudicated here.
ReadFlag = Literal["stale", "contested", "contradicted", "uncovered", "unsure_hold"]
# Which surface ``next`` suggestions are rendered for.
ReadSurface = Literal["mcp", "cli", "sdk"]

#: A Document body larger than this is never returned whole; pass a byte range.
GET_BODY_DEFAULT_MAX_BYTES = 64 * 1024
#: The widest byte range one ``get(detail="body")`` returns.
GET_BODY_RANGE_MAX_BYTES = 256 * 1024
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
    "document": ("summary", "why", "history", "proof", "body"),
    "procedure": ("summary", "history", "proof"),
    "blueprint": ("summary", "history", "proof"),
    "query": ("summary", "history", "proof"),
    "capture_contract": ("summary", "history", "proof"),
    "trigger": ("summary", "history", "proof"),
    "proposal": ("summary", "proof"),
    "line": ("summary", "history", "proof"),
    "capture": ("summary", "proof"),
    "resolution_contract": ("summary", "history", "proof"),
    "mandate": ("summary", "history", "proof"),
    "procedure_run": ("summary", "proof"),
    "principal": ("summary", "proof"),
    "approval_policy": ("summary", "history", "proof"),
    "procedure_runtime_policy": ("summary", "history", "proof"),
    "source_acquisition_policy": ("summary", "history", "proof"),
    "provider_interface": ("summary", "history", "proof"),
}


class _StrictGetModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class ByteRange(_StrictGetModel):
    """A half-open byte range ``[start, end)`` of a Document body."""

    start: int = Field(ge=0, description="First byte, zero-based.")
    end: int = Field(gt=0, description="One past the last byte.")

    @model_validator(mode="after")
    def _ordered(self) -> ByteRange:
        if self.end <= self.start:
            raise ValueError("range end must be greater than start (for example 0:4096)")
        if self.end - self.start > GET_BODY_RANGE_MAX_BYTES:
            raise ValueError(
                f"range spans more than {GET_BODY_RANGE_MAX_BYTES} bytes; "
                f"read it in slices (for example 0:{GET_BODY_RANGE_MAX_BYTES})"
            )
        return self

    @classmethod
    def parse(cls, value: str) -> ByteRange:
        """Read the CLI spelling ``start:end``."""

        start, separator, end = value.partition(":")
        if not separator or not start.isdigit() or not end.isdigit():
            raise ValueError("range must be start:end in bytes (for example 0:4096)")
        return cls(start=int(start), end=int(end))


class GetRequest(_StrictGetModel):
    tag: Literal["playbill-get-request-v1"] = "playbill-get-request-v1"
    ref: str = Field(min_length=1, max_length=512)
    detail: GetDetail = "summary"
    range: ByteRange | None = None
    at: AcceptedCoordinate | str | None = None
    evaluation_time: datetime | None = None
    surface: ReadSurface = "mcp"
    # ``detail="history"`` pages: revisions per page (default 20) and the
    # opaque ``next_cursor`` of the page before.
    limit: int | None = Field(default=None, ge=1, le=GET_HISTORY_MAX_LIMIT)
    cursor: str | None = Field(default=None, min_length=1)
    # Also answer the full four-digest accepted coordinate, which only
    # ``detail="proof"`` carries otherwise (the SDK pins reads with it).
    full_coordinate: bool = False

    @field_validator("at")
    @classmethod
    def _at(cls, value: AcceptedCoordinate | str | None) -> AcceptedCoordinate | str | None:
        if isinstance(value, str) and not re.fullmatch(_GIT_OID, value):
            raise ValueError(
                "at must be an accepted coordinate, a lowercase hex git oid (a unique "
                "prefix of at least 12 characters), or a generation number (for example 42; "
                "an all-digit value of 11 or fewer characters is always a generation)"
            )
        return value

    @field_validator("evaluation_time")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("evaluation_time must be timezone-aware (for example ...Z)")
        return value

    @model_validator(mode="after")
    def _range_only_for_body(self) -> GetRequest:
        if self.range is not None and self.detail != "body":
            raise ValueError('range applies only to detail="body"')
        if (self.limit is not None or self.cursor is not None) and self.detail != "history":
            raise ValueError('limit and cursor page detail="history" only')
        return self


class GetCoordinate(_StrictGetModel):
    """Which accepted generation answered: the git oid's 12-hex prefix and its sequence."""

    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    generation: int = Field(ge=0)


class GetContender(_StrictGetModel):
    claim: str
    value: ShownValue
    # An exact-content value's digest; its text is ``value``.
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    verdict: str


class GetClaimCard(_StrictGetModel):
    claim: str
    subject: str
    predicate: str
    predicate_full: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    # The value as a summary shows it: a long string is a TruncatedText preview.
    value: ShownValue
    # An exact-content value's digest, the proof its text is ``value``.
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    verdict: str
    status: str
    revision: int
    accepted: str | None = Field(default=None, exclude_if=_omit_none)
    contenders: tuple[GetContender, ...] = ()
    flags: tuple[ReadFlag, ...] = ()
    next: tuple[str, ...] = ()


class GetSubjectClaim(_StrictGetModel):
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    # The Claim behind the value; a list, aligned with ``value``, when the row
    # shows several (a many-valued predicate or a contested slot).
    claim: str | tuple[str, ...]
    value: ShownValue
    # An exact-content row's digests, aligned with ``claim`` the same way.
    content_digest: str | tuple[str, ...] | None = Field(default=None, exclude_if=_omit_none)
    flags: tuple[ReadFlag, ...] = ()


class GetSubjectCard(_StrictGetModel):
    subject: str
    kind: str
    lifecycle: str
    claims: tuple[GetSubjectClaim, ...]
    incoming_count: int
    next: tuple[str, ...] = ()


class GetEvidenceRule(_StrictGetModel):
    """One evidence rule: which roles it admits evidence for, under which contracts."""

    rule_id: str
    roles: tuple[ClaimRole, ...]
    contracts: tuple[str, ...]
    admission: Literal["origin_only", "direct", "derivational"]


class GetClaimTypeCard(_StrictGetModel):
    predicate: str
    subject_kinds: tuple[str, ...]
    object: str
    cardinality: str
    members: tuple[Any, ...] | None = Field(default=None, exclude_if=_omit_none)
    description: str | None = Field(default=None, exclude_if=_omit_none)
    # Each described enum member, as its literal value beside what it means.
    member_descriptions: tuple[ClaimTypeMemberDescription, ...] = Field(
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
    evidence_rules: tuple[GetEvidenceRule, ...] = ()
    live_claims: int
    next: tuple[str, ...] = ()


class GetDocumentCard(_StrictGetModel):
    document: str
    title: str
    document_kind: str
    media_type: str
    size: int | None
    revision: int
    next: tuple[str, ...] = ()


class GetProcedureTrackRecord(_StrictGetModel):
    """One accepted promotion of a Procedure's run exhaust, and what it computed.

    A promotion pins a contiguous range of run records and a reducer; its
    acceptance makes the reducer's output over exactly those records part of
    accepted state. Records nobody promoted are never counted here.
    """

    promotion: str = Field(description="The ExhaustPromotion's name.")
    first_sequence: int = Field(description="The first run record the promotion covers.")
    last_sequence: int = Field(description="The last run record the promotion covers.")
    output: Any = Field(description="The reducer's output over those records, as accepted.")
    output_digest: str
    promotion_digest: str


class GetProcedureNode(_StrictGetModel):
    """One node the direct run lane does not execute, and where it can run."""

    node_id: str
    kind: str
    runs_on: Literal["line", "nowhere"]


class GetProcedureCard(_StrictGetModel):
    procedure: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    #: A retired Procedure stays readable; nothing runs or measures it.
    lifecycle: Literal["live", "retired"] = "live"
    inputs: dict[str, Any]
    #: ``direct``: ``procedure run``; ``line``: only as a Line (its terminals act
    #: outward under the Line's authority); ``unsupported``: no run path admits it.
    runnable: Literal["direct", "line", "unsupported"]
    #: The nodes behind a ``line`` or ``unsupported`` answer.
    unsupported_nodes: tuple[GetProcedureNode, ...] = ()
    #: Accepted promotions of this Procedure's runs, by promotion name.
    track_record: tuple[GetProcedureTrackRecord, ...] = ()
    next: tuple[str, ...] = ()


class GetQueryParameter(_StrictGetModel):
    name: str
    type: str
    required: bool


class GetQueryCard(_StrictGetModel):
    query: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    params: tuple[GetQueryParameter, ...] = ()
    next: tuple[str, ...] = ()


class GetCaptureContractCard(_StrictGetModel):
    contract: str
    version: int
    lifecycle: str
    captures: dict[str, Any]
    admitted_by: tuple[str, ...]
    next: tuple[str, ...] = ()


class GetBlueprintSlot(_StrictGetModel):
    """One open slot: the interface it needs and the accepted Providers that fit it."""

    slot: str
    #: ``ProviderInterface:<name>``
    interface: str
    #: Live Providers implementing that interface exactly once; any one can be bound.
    fits: tuple[str, ...] = ()


class GetBlueprintCard(_StrictGetModel):
    """A Procedure skeleton and its instantiation preview: each slot and what fits it."""

    blueprint: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    lifecycle: str
    inputs: dict[str, Any]
    slots: tuple[GetBlueprintSlot, ...]
    next: tuple[str, ...] = ()


class GetTriggerCard(_StrictGetModel):
    """One Trigger: when it fires and what it sets off."""

    trigger: str
    lifecycle: str
    schedule: dict[str, Any]
    #: ``Line:<name>`` or the internal action it fires.
    target: str
    next: tuple[str, ...] = ()


class GetProposalChange(_StrictGetModel):
    path: str
    change: str


class GetProposalRefusal(_StrictGetModel):
    """One refusal diagnostic of a refused proposal, with its served repair."""

    code: str
    message: str
    repair: ServedRepair


class GetProposalCard(_StrictGetModel):
    proposal: str
    status: str
    # Retained partial evidence: which records are missing, as the list says.
    incomplete: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    verdict: str | None = Field(default=None, exclude_if=_omit_none)
    reason: str | None = Field(default=None, exclude_if=_omit_none)
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    admitted_at: str | None = Field(default=None, exclude_if=_omit_none)
    rationale: str | None = Field(default=None, exclude_if=_omit_none)
    changes: tuple[GetProposalChange, ...] = ()
    #: Why a refused proposal was refused: every stored diagnostic, in order.
    refusal: tuple[GetProposalRefusal, ...] = Field(default=(), exclude_if=lambda value: not value)
    next: tuple[str, ...] = ()


class GetPrincipalCard(_StrictGetModel):
    """One registered principal: who it is, what kind, and whether it is active."""

    principal: str
    kind: str
    status: Literal["active", "revoked"]
    algorithm: str
    public_key: str
    next: tuple[str, ...] = ()


class GetApprovalPolicyCard(_StrictGetModel):
    """The instance's approval policy: whether a proposer may approve its own change."""

    policy: str
    mode: Literal["self_approval_allowed", "independent_approval_required"]
    next: tuple[str, ...] = ()


class GetProcedureRuntimePolicyCard(_StrictGetModel):
    """The instance's Procedure runtime ceilings: how much one run may produce and retry."""

    policy: str
    provider_output_bytes_cap: int
    result_bytes_cap: int | None = Field(default=None, exclude_if=_omit_none)
    repeat_attempts_cap: int | None = Field(default=None, exclude_if=_omit_none)
    next: tuple[str, ...] = ()


class GetAcquisitionInput(_StrictGetModel):
    """How one Procedure input is acquired under a source acquisition policy."""

    input: str
    requirement: Literal["required", "optional", "conservative_default"]
    #: The oldest acquisition admitted, as ``<seconds>s``; absent means any age.
    max_age: str | None = Field(default=None, exclude_if=_omit_none)


class GetSourceAcquisitionPolicyCard(_StrictGetModel):
    """One source acquisition policy: how each input is acquired and kept coherent."""

    policy: str
    lifecycle: str
    coherence: Literal["independent", "bounded_window", "declared_snapshot_group"]
    inputs: tuple[GetAcquisitionInput, ...] = ()
    next: tuple[str, ...] = ()


class GetProviderInterfaceProvider(_StrictGetModel):
    provider: str
    implementation_digest: str


class GetProviderInterfaceCard(_StrictGetModel):
    """One live provider interface: its contract fields, effect and implementations.

    ``detail="proof"`` answers the accepted inventory entry a Procedure node
    pins (``entry``): artifact, interface and classifier digests, the operation
    contract and every implementation.
    """

    interface: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    input: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    effect: Literal["none", "external_read", "external_mutation"]
    providers: tuple[GetProviderInterfaceProvider, ...] = ()
    interface_digest: str
    next: tuple[str, ...] = ()


GetCard = (
    GetClaimCard
    | GetSubjectCard
    | GetClaimTypeCard
    | GetDocumentCard
    | GetProcedureCard
    | GetBlueprintCard
    | GetQueryCard
    | GetCaptureContractCard
    | GetTriggerCard
    | GetProposalCard
    | GetLineCard
    | GetCaptureCard
    | GetResolutionContractCard
    | GetMandateCard
    | GetProcedureRunCard
    | GetPrincipalCard
    | GetApprovalPolicyCard
    | GetProcedureRuntimePolicyCard
    | GetSourceAcquisitionPolicyCard
    | GetProviderInterfaceCard
)


class GetCaptureEvidence(_StrictGetModel):
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


class GetAttestationEvidence(_StrictGetModel):
    stance: str
    principal: str
    at: datetime
    current: bool


class GetEvidence(_StrictGetModel):
    # The Claim's whole value, never cut as a summary card cuts a long one.
    value: Any
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    captures: tuple[GetCaptureEvidence, ...]
    attestations: tuple[GetAttestationEvidence, ...]
    rationale: str | None = Field(default=None, exclude_if=_omit_none)


class GetRevision(_StrictGetModel):
    revision: int
    sequence: int
    # The accepted generation's git oid (12-hex prefix): pass it, or the
    # sequence, back as ``at`` to read at that revision.
    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    accepted: str
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    approved_by: tuple[str, ...] = ()
    lifecycle: str | None = Field(default=None, exclude_if=_omit_none)
    value: ShownValue = Field(default=None, exclude_if=_omit_none)
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    digest: str = Field(description="Artifact digest prefix of this revision.")
    # Read a cut value in full at this revision's accepted generation.
    next: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class GetHistory(_StrictGetModel):
    """One page of revisions, newest first; ``revision`` counts from the oldest.

    Each revision's value follows the summary card rule: a string over 500
    characters is a ``TruncatedText`` preview, read whole through ``next``.
    """

    revisions: tuple[GetRevision, ...]


class GetBody(_StrictGetModel):
    """A byte range of one Document body; ``body_digest`` names the whole body."""

    document: str
    media_type: str
    size: int
    body_digest: str
    # The bytes returned; absent for an empty Document, which has none.
    range: ByteRange | None = None
    text: str | None = Field(default=None, exclude_if=_omit_none)
    content_base64: str | None = Field(default=None, exclude_if=_omit_none)


class GetResult(_StrictGetModel):
    tag: Literal["playbill-get-result-v1"] = "playbill-get-result-v1"
    ref: str
    kind: GetRefKind
    detail: GetDetail
    card: GetCard | None = Field(default=None, exclude_if=_omit_none)
    evidence: GetEvidence | None = Field(default=None, exclude_if=_omit_none)
    history: GetHistory | None = Field(default=None, exclude_if=_omit_none)
    body: GetBody | None = Field(default=None, exclude_if=_omit_none)
    # Today's explain output, unchanged.
    why: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    # Today's full accepted envelope, unchanged.
    proof: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    # ``detail="history"`` paging; absent on every other detail.
    truncated: bool | None = Field(default=None, exclude_if=_omit_none)
    next_cursor: str | None = Field(default=None, exclude_if=_omit_none)
    coordinate: GetCoordinate
    # The full accepted coordinate: under ``detail="proof"``, or when asked for.
    accepted_coordinate: AcceptedCoordinate | None = Field(default=None, exclude_if=_omit_none)
    # Present when part of the answer is operational state, read live at the
    # current head (``live.as_of``) whatever ``coordinate`` the read named.
    live: LiveView | None = Field(default=None, exclude_if=_omit_none)
    evaluation_time: datetime


#: The most references one internal batch read resolves.
GET_BATCH_MAX_REFS = 64


class GetBatchRequest(_StrictGetModel):
    """Several references read at one coordinate and one detail: an SDK-internal route.

    Agents call ``get`` once per reference; the SDK reads a whole vocabulary
    (every ClaimType envelope, for ``world()``) in a few round trips with this.
    Every result answers the coordinate the first one resolved.
    """

    tag: Literal["playbill-get-batch-request-v1"] = "playbill-get-batch-request-v1"
    refs: tuple[str, ...] = Field(min_length=1, max_length=GET_BATCH_MAX_REFS)
    detail: Literal["summary", "proof"] = "proof"
    at: AcceptedCoordinate | str | None = None
    evaluation_time: datetime | None = None
    surface: ReadSurface = "sdk"


class GetBatchResult(_StrictGetModel):
    tag: Literal["playbill-get-batch-result-v1"] = "playbill-get-batch-result-v1"
    coordinate: AcceptedCoordinate
    results: tuple[GetResult, ...]


__all__ = [
    "GET_BATCH_MAX_REFS",
    "GET_BODY_DEFAULT_MAX_BYTES",
    "GET_BODY_RANGE_MAX_BYTES",
    "GET_DETAILS_BY_KIND",
    "GET_HISTORY_DEFAULT_LIMIT",
    "GET_HISTORY_MAX_LIMIT",
    "ByteRange",
    "GetAcquisitionInput",
    "GetApprovalPolicyCard",
    "GetAttestationEvidence",
    "GetBatchRequest",
    "GetBatchResult",
    "GetBody",
    "GetCaptureContractCard",
    "GetTriggerCard",
    "GetCaptureEvidence",
    "GetCard",
    "GetClaimCard",
    "GetClaimTypeCard",
    "GetEvidenceRule",
    "GetContender",
    "GetCoordinate",
    "GetDetail",
    "GetDocumentCard",
    "GetEvidence",
    "GetHistory",
    "GetPrincipalCard",
    "GetBlueprintCard",
    "GetBlueprintSlot",
    "GetProcedureCard",
    "GetProcedureNode",
    "GetProcedureRuntimePolicyCard",
    "GetProcedureTrackRecord",
    "GetProviderInterfaceCard",
    "GetProviderInterfaceProvider",
    "GetProposalCard",
    "GetProposalRefusal",
    "GetProposalChange",
    "GetQueryCard",
    "GetQueryParameter",
    "GetRefKind",
    "GetRequest",
    "GetResult",
    "GetRevision",
    "GetSourceAcquisitionPolicyCard",
    "GetSubjectCard",
    "GetSubjectClaim",
    "ReadFlag",
    "ReadSurface",
]
