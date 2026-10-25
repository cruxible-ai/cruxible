"""The ``get`` read: one governed thing by reference, values first.

A request names one reference in any form an agent sees (``CLM-…``, ``kind/id``,
a predicate, ``Document:<name>``, a proposal id, an artifact path, ...) and one
``detail`` level. The result leads with the values an agent asks for; digests
and the full accepted envelope sit behind ``detail="proof"``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimTypeContention,
    ClaimTypeMemberDescription,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.claims import ClaimRetirementReason
from cruxible_client.contracts.operational_reads import (
    GetCaptureCard,
    GetCaptureErasure,
    GetLineCard,
    GetMandateCard,
    GetProcedureRunCard,
    GetResolutionContractCard,
    LiveView,
    RunRow,
)
from cruxible_client.contracts.procedures.dispositions import ProcedureMeasurementReview
from cruxible_client.contracts.read_values import ClaimLine as ClaimLine
from cruxible_client.contracts.read_values import ShownValue
from cruxible_client.contracts.repairs import ServedRepair

GetDetail = Literal["summary", "evidence", "why", "history", "incoming", "runs", "proof", "body"]
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
    "subject_kind",
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
    "provider",
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
#: Rows per paged ``get`` detail (``history``: revisions or a Subject's field
#: events; ``incoming``: the Claims pointing at a Subject; ``runs``: a Line's
#: runs), by default and at most.
GET_HISTORY_DEFAULT_LIMIT = 20
GET_HISTORY_MAX_LIMIT = 200
#: The ``get`` details that page with ``limit`` and ``cursor``.
GET_PAGED_DETAILS: tuple[str, ...] = ("history", "incoming", "runs")

# A git oid or a prefix of one; the read resolves it (at least 12 hex, unique).
_GIT_OID = r"^[0-9a-f]{1,64}$"

# Which details apply to which kind of reference.
GET_DETAILS_BY_KIND: dict[str, tuple[str, ...]] = {
    "claim": ("summary", "evidence", "why", "history", "proof"),
    "subject": ("summary", "why", "history", "incoming", "proof"),
    "claim_type": ("summary", "history", "proof"),
    "document": ("summary", "why", "history", "proof", "body"),
    "procedure": ("summary", "history", "proof"),
    "blueprint": ("summary", "history", "proof"),
    "query": ("summary", "history", "proof"),
    "capture_contract": ("summary", "history", "proof"),
    "trigger": ("summary", "history", "proof"),
    "subject_kind": ("summary", "history", "proof"),
    "proposal": ("summary", "proof"),
    "line": ("summary", "history", "runs", "proof"),
    "capture": ("summary", "proof"),
    "resolution_contract": ("summary", "history", "proof"),
    "mandate": ("summary", "history", "proof"),
    "procedure_run": ("summary", "proof"),
    "principal": ("summary", "proof"),
    "approval_policy": ("summary", "history", "proof"),
    "procedure_runtime_policy": ("summary", "history", "proof"),
    "source_acquisition_policy": ("summary", "history", "proof"),
    "provider_interface": ("summary", "history", "proof"),
    "provider": ("summary", "history", "proof"),
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
    # ``detail="history"``, ``"incoming"`` and ``"runs"`` page: rows per page
    # (default 20) and the opaque ``next_cursor`` of the page before.
    limit: int | None = Field(default=None, ge=1, le=GET_HISTORY_MAX_LIMIT)
    cursor: str | None = Field(default=None, min_length=1)
    # ``detail="history"`` on a Subject: only this field's events.
    field: str | None = Field(default=None, min_length=1, max_length=512)
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
        if (
            self.limit is not None or self.cursor is not None
        ) and self.detail not in GET_PAGED_DETAILS:
            raise ValueError('limit and cursor page detail="history", "incoming" or "runs" only')
        if self.field is not None and self.detail != "history":
            raise ValueError('field narrows detail="history" on a Subject only')
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


class GetNone(_StrictGetModel):
    """One none in a field: a live inverse Claim saying the field holds nothing.

    ``of`` is the unit it covers: absent for a one-valued field's whole slot,
    else the value (a Subject as kind/id) or key value the field does not hold.
    """

    claim: str
    of: ShownValue = Field(default=None, exclude_if=_omit_none)
    verdict: str
    flags: tuple[ReadFlag, ...] = Field(default=(), exclude_if=lambda value: not value)


class GetSubjectClaim(_StrictGetModel):
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    # The Claim behind the value; a list, aligned with ``value``, when the row
    # shows several (a many-valued predicate or a contested slot). A field that
    # holds only nones shows no value: ``claim`` and ``value`` are empty lists.
    claim: str | tuple[str, ...]
    value: ShownValue
    # An exact-content row's digests, aligned with ``claim`` the same way.
    content_digest: str | tuple[str, ...] | None = Field(default=None, exclude_if=_omit_none)
    flags: tuple[ReadFlag, ...] = ()
    # The field's live nones (ClaimType v9 inverses): never values. A field
    # with no value and no current none is unknown.
    nones: tuple[GetNone, ...] = Field(default=(), exclude_if=lambda value: not value)


class GetIncomingClaim(_StrictGetModel):
    """One live Claim whose Subject-valued object is the Subject read.

    ``subject`` is the asserting Subject (``kind/id``): a relation is stored
    once, on it, so this is the edge read from its object's side.
    """

    claim: str
    subject: str
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)


class GetIncoming(_StrictGetModel):
    """One page of the live Claims pointing at a Subject, in Claim id order.

    Read through the object index one page at a time: a page costs its rows,
    never the Subject's whole incoming set. ``get`` each Claim (or batch them)
    for its verdict and value.
    """

    kind: Literal["incoming"] = "incoming"
    claims: tuple[GetIncomingClaim, ...]


class GetLineRuns(_StrictGetModel):
    """One page of a Line's Procedure runs, newest admission first.

    Read through the run journal's per-Line partition index one page at a
    time, keyed by each admission's immutable position: a run admitted or
    finished between pages is never repeated or skipped, and a page costs its
    rows, never the Line's whole run history. Runs are operational state, read
    live at the current head. ``get ProcedureRun:<run>`` reads one.
    """

    kind: Literal["line_runs"] = "line_runs"
    line: str
    runs: tuple[RunRow, ...]


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
    # How rival Claims of one slot settle: the resolution selector, what a
    # conflict resolves to, and the admission policy's requirement counts.
    contention: ClaimTypeContention
    # The format version (7 for ``playbill-claim-type-v7``), the accepted
    # revision and the ledger path (which a kit's drifted paths name).
    format: int = Field(ge=1)
    revision: int = Field(ge=1)
    path: str
    # Live Claims of the predicate, and the Subjects holding at least one, over
    # every kind: read from counters Claim writes maintain, never a recount.
    live_claims: int
    subjects_with_value: int = Field(ge=0)
    # The installed kit that owns the ClaimType (its receipt records it); absent
    # when no kit owns it.
    kit: str | None = Field(default=None, exclude_if=_omit_none)
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
    measurement_reviews: tuple[ProcedureMeasurementReview, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
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


class GetSubjectKindCard(_StrictGetModel):
    """One SubjectKind: how core names and identifies Subjects of the kind."""

    #: ``SubjectKind:<kind>``
    subject_kind: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    lifecycle: str
    #: ``keys`` (named by its key ClaimTypes) or ``minted`` (core mints ids).
    rule: Literal["keys", "minted"]
    #: The key ClaimTypes in key order; empty for a minted kind.
    keys: tuple[str, ...] = ()
    uniqueness: Literal["unique", "hint"] | None = Field(default=None, exclude_if=_omit_none)
    #: How many Subjects of the kind are live at this coordinate.
    live_subjects: int
    #: The installed kit that owns the SubjectKind; absent when no kit owns it.
    kit: str | None = Field(default=None, exclude_if=_omit_none)
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
    """One source policy: age defaults and alias overrides, or frozen v1 details."""

    policy: str
    lifecycle: str
    max_age: str | None = Field(default=None, exclude_if=_omit_none)
    overrides: dict[str, str | None] = Field(default_factory=dict)
    coherence: Literal["independent", "bounded_window", "declared_snapshot_group"] | None = Field(
        default=None, exclude_if=_omit_none
    )
    inputs: tuple[GetAcquisitionInput, ...] = Field(default=(), exclude_if=lambda value: not value)
    next: tuple[str, ...] = ()


class GetProviderCard(_StrictGetModel):
    """One accepted Provider build, using the names shown by provider list."""

    provider: str
    build: str
    package: str | None = None
    version: str | None = None
    state: Literal["live", "retired"]
    interfaces: tuple[str, ...] = ()
    next: tuple[str, ...] = ()


class GetProviderInterfaceProvider(_StrictGetModel):
    provider: str


class GetProviderInterfaceCard(_StrictGetModel):
    """One live provider interface: its contract fields, effect and implementations.

    ``detail="proof"`` answers the accepted inventory entry a Procedure node
    pins (``entry``): the artifact and interface digests, the operation contract
    and every implementation.
    """

    interface: str
    description: str | None = Field(default=None, exclude_if=_omit_none)
    input: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    effect: Literal["pure", "external_read", "external_mutation"]
    providers: tuple[GetProviderInterfaceProvider, ...] = ()
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
    | GetSubjectKindCard
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
    | GetProviderCard
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
    origin: Literal["independent", "self_source", "legacy"] = Field(
        description="Whose bytes the citation reads: an independent source's, the Claim's "
        "own authored words (self_source), or a legacy backing's."
    )
    status: Literal["admitted", "not_admitted", "not_evidence"] = Field(
        description="admitted: the ClaimType's rule admits it and the verdict reads it; "
        "not_admitted: offered as evidence, refused by the rule; not_evidence: a copy "
        "the rule does not admit, which the verdict never reads."
    )
    erased: GetCaptureErasure | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="Present when the Capture's retained body was erased: its bytes are gone, "
        "so the verdict reads this evidence as no longer replayable.",
    )


#: Why an attestation does or does not count toward the Claim's verdict.
AttestationCountReason: TypeAlias = Literal[
    "cites_admitted_capture",
    "consequence_policy",
    "cites_no_capture",
    "capture_not_admitted",
    "not_accepted",
    "not_current",
]


class GetAttestationEvidence(_StrictGetModel):
    """One attestation of this exact Claim version, and whether it counts.

    An accepted attestation artifact, or an event ``claim attest`` recorded.
    The verdict reads accepted ones: a current one counts when it cites
    Captures the Claim admits. Either counts when the ClaimType's consequence
    policy has a rule for its stance. A recorded event is otherwise shown and
    resurfaced in ``next``, never read by the verdict (``not_accepted``).
    """

    stance: str
    principal: str
    at: datetime
    current: bool
    record: Literal["accepted", "event"]
    basis: Literal["examined_existing", "new_capture", "legacy"]
    counts: bool
    counts_reason: AttestationCountReason
    event: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The attestation door event's digest, for a recorded event.",
    )
    rule: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The consequence-policy rule it counts under.",
    )


class GetEvidence(_StrictGetModel):
    # The Claim's whole value, never cut as a summary card cuts a long one.
    value: Any
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    captures: tuple[GetCaptureEvidence, ...]
    attestations: tuple[GetAttestationEvidence, ...]
    rationale: str | None = Field(default=None, exclude_if=_omit_none)


#: A change-set reason longer than this is cut, as a summary value is; the
#: proposal card (``get <proposal>``) reads its whole rationale.
GET_REASON_MAX_CHARS = 500


class GetProposedReason(_StrictGetModel):
    """Why this revision was proposed, in its own Procedure item's words.

    Read from the proposal that accepted the revision: the item's own
    ``rationale``, and the run and terminal that proposed it. Provenance, not
    evidence.
    """

    kind: Literal["procedure_item"] = "procedure_item"
    reason: str
    truncated: bool = Field(default=False, exclude_if=lambda value: value is False)
    proposal: str
    procedure: str
    run: str
    terminal: str


class GetChangeSetReason(_StrictGetModel):
    """Why this revision was proposed: the rationale of the change set that accepted it.

    A write's ``because`` (with any per-retire note), or an authored change
    set's rationale, read from the exact admission the accepted generation
    published. Provenance, not evidence. Over ``GET_REASON_MAX_CHARS`` it is
    cut (``truncated``); ``get <proposal>`` reads it whole.
    """

    kind: Literal["change_set"] = "change_set"
    text: str
    truncated: bool = Field(default=False, exclude_if=lambda value: value is False)
    proposal: str


GetRevisionReason = Annotated[GetChangeSetReason | GetProposedReason, Field(discriminator="kind")]
"""Why one accepted revision was proposed: its Procedure item's reason, else its change set's."""


RevisionChange = Literal["stated", "changed", "reobserved", "rebound", "retired"]
"""What one Claim revision did: the first of its lineage, a new value, the same
value on new backing, the same statement with a drifted citation replaced by
the text it rests on now (``rebound``), or its retirement."""


class GetRevision(_StrictGetModel):
    """One accepted revision of the thing ``get`` names, and why it was proposed.

    A Claim's revision says what it did (``change``) and, when retired, why it
    ended (``retirement``); ``reason`` is the shared resolver's: the Procedure
    item's own reason for it, else its change set's rationale, else absent.
    """

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
    change: RevisionChange | None = Field(default=None, exclude_if=_omit_none)
    reason: GetRevisionReason | None = Field(default=None, exclude_if=_omit_none)
    retirement: ClaimRetirementReason | None = Field(default=None, exclude_if=_omit_none)
    # Read a cut value in full at this revision's accepted generation.
    next: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class GetHistory(_StrictGetModel):
    """One page of revisions, newest first; ``revision`` counts from the oldest.

    Each revision's value follows the summary card rule: a string over 500
    characters is a ``TruncatedText`` preview whose ``read_whole`` reads this
    revision's whole value at its own generation.
    """

    kind: Literal["revisions"] = "revisions"
    revisions: tuple[GetRevision, ...]
    measurement_reviews: tuple[ProcedureMeasurementReview, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )


class _GetFieldEvent(_StrictGetModel):
    """One change to one field of a Subject: one revision of one of its Claims."""

    #: The field's short name, as ``get`` and ``orient`` show it.
    field: str
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_omit_none)
    claim: str
    sequence: int
    # The accepted generation's git oid (12-hex prefix): pass it, or the
    # sequence, back as ``at`` to read the Subject as this event left it.
    git_oid: str = Field(pattern=r"^[0-9a-f]{12}$")
    accepted: str
    actor: str | None = Field(default=None, exclude_if=_omit_none)
    approved_by: tuple[str, ...] = ()
    reason: GetRevisionReason | None = Field(default=None, exclude_if=_omit_none)
    value: ShownValue = Field(default=None, exclude_if=_omit_none)
    content_digest: str | None = Field(default=None, exclude_if=_omit_none)
    # Read a cut value in full at this event's accepted generation.
    next: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class GetFieldStated(_GetFieldEvent):
    """A new Claim lineage: the field's first value, one more of a many-valued field,
    or (``contended``) a competing value beside a live one of a single-value field."""

    change: Literal["stated"] = "stated"
    contended: bool = Field(default=False, exclude_if=lambda value: value is False)


class GetFieldChanged(_GetFieldEvent):
    """A revision with a new value; ``before`` is the value it replaced."""

    change: Literal["changed"] = "changed"
    before: ShownValue = None


class GetFieldReobserved(_GetFieldEvent):
    """A revision with the same value on new backing (evidence refreshed)."""

    change: Literal["reobserved"] = "reobserved"


class GetFieldRebound(_GetFieldEvent):
    """A re-bind: the same statement, with drifted citations replaced by new ones.

    ``replaced`` are the Captures (``CAP-<12 hex>`` handles) whose citations the
    revision dropped; ``by`` the Captures it cited in their place. The Claim ID
    and lineage stay; ``reason`` is the write's ``because``.
    """

    change: Literal["rebound"] = "rebound"
    replaced: tuple[str, ...]
    by: tuple[str, ...]


class GetFieldRetired(_GetFieldEvent):
    """The Claim's retirement: the field no longer holds ``value``."""

    change: Literal["retired"] = "retired"
    retirement: ClaimRetirementReason | None = Field(default=None, exclude_if=_omit_none)


GetFieldEvent = Annotated[
    GetFieldStated | GetFieldChanged | GetFieldReobserved | GetFieldRebound | GetFieldRetired,
    Field(discriminator="change"),
]


class GetSubjectHistory(_StrictGetModel):
    """A Subject's history: its shell's revisions, and one page of its field events.

    ``revisions`` are the Subject shell's own (created, pins, retired), all of
    them. ``events`` are its fields' changes, newest first, one page: each
    revision of each Claim about the Subject, with what it did and why. Group
    them by ``field`` for each field's value timeline.
    """

    kind: Literal["subject"] = "subject"
    revisions: tuple[GetRevision, ...]
    events: tuple[GetFieldEvent, ...]


GetHistoryAny = Annotated[GetHistory | GetSubjectHistory, Field(discriminator="kind")]


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
    history: GetHistoryAny | None = Field(default=None, exclude_if=_omit_none)
    incoming: GetIncoming | None = Field(default=None, exclude_if=_omit_none)
    runs: GetLineRuns | None = Field(default=None, exclude_if=_omit_none)
    body: GetBody | None = Field(default=None, exclude_if=_omit_none)
    # Today's explain output, unchanged.
    why: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    # Under ``detail="why"``: the Procedure item's own reason for the accepted
    # revision of this Claim or Subject, when a Procedure proposed it.
    reason: GetProposedReason | None = Field(default=None, exclude_if=_omit_none)
    # Today's full accepted envelope, unchanged.
    proof: dict[str, Any] | None = Field(default=None, exclude_if=_omit_none)
    # Paging of ``detail="history"``, ``"incoming"`` and ``"runs"``; absent on every other detail.
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
    (every ClaimType envelope, for ``world()``) in a few round trips with this,
    and resolves individual World Subjects without population-wide suggestions.
    Every result answers the coordinate the first one resolved.
    """

    tag: Literal["playbill-get-batch-request-v1"] = "playbill-get-batch-request-v1"
    refs: tuple[str, ...] = Field(min_length=1, max_length=GET_BATCH_MAX_REFS)
    detail: Literal["summary", "proof"] = "proof"
    at: AcceptedCoordinate | str | None = None
    evaluation_time: datetime | None = None
    surface: ReadSurface = "sdk"
    subject_suggestions: bool = Field(
        default=True,
        description=(
            "SDK-internal: suggest nearest Subject names when resolution fails. "
            "World sets false to bound absent Subject lookups to the identity "
            "indexes. Other kinds retain their ordinary suggestions."
        ),
    )


class GetBatchResult(_StrictGetModel):
    tag: Literal["playbill-get-batch-result-v1"] = "playbill-get-batch-result-v1"
    coordinate: AcceptedCoordinate
    results: tuple[GetResult, ...]


__all__ = [
    "GET_BATCH_MAX_REFS",
    "GET_BODY_DEFAULT_MAX_BYTES",
    "GET_BODY_RANGE_MAX_BYTES",
    "GET_REASON_MAX_CHARS",
    "GET_DETAILS_BY_KIND",
    "GET_HISTORY_DEFAULT_LIMIT",
    "GET_HISTORY_MAX_LIMIT",
    "GET_PAGED_DETAILS",
    "AttestationCountReason",
    "ByteRange",
    "ClaimLine",
    "GetAcquisitionInput",
    "GetApprovalPolicyCard",
    "GetAttestationEvidence",
    "GetBatchRequest",
    "GetBatchResult",
    "GetBody",
    "GetCaptureContractCard",
    "GetTriggerCard",
    "GetSubjectKindCard",
    "GetCaptureErasure",
    "GetCaptureEvidence",
    "GetCard",
    "GetClaimCard",
    "GetClaimTypeCard",
    "GetEvidenceRule",
    "GetContender",
    "GetCoordinate",
    "GetDetail",
    "GetChangeSetReason",
    "GetProposedReason",
    "GetRevisionReason",
    "GetDocumentCard",
    "GetEvidence",
    "GetFieldChanged",
    "GetFieldEvent",
    "GetFieldRebound",
    "GetFieldReobserved",
    "GetFieldRetired",
    "GetFieldStated",
    "GetHistory",
    "GetHistoryAny",
    "GetIncoming",
    "GetIncomingClaim",
    "GetLineRuns",
    "GetPrincipalCard",
    "GetBlueprintCard",
    "GetBlueprintSlot",
    "GetProcedureCard",
    "GetProcedureNode",
    "GetProcedureRuntimePolicyCard",
    "GetProcedureTrackRecord",
    "GetProviderInterfaceCard",
    "GetProviderCard",
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
    "GetNone",
    "GetSubjectClaim",
    "GetSubjectHistory",
    "ReadFlag",
    "ReadSurface",
    "RevisionChange",
]
