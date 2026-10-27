"""The orient read: one bounded map of an instance's accepted state.

``orient`` answers "what is here and where do I start" in one call: the Subject
kinds with their counts and compact predicate descriptors, how many of each
artifact family exist, the named queries, who the caller is and whether it can
author, what needs attention, and runnable follow-up calls. ``orient(kind=K)``
widens one kind to every predicate in full, the predicates that point at it
(``incoming``, its reverse follows) and sample Subject IDs, and
``orient(section=S)`` pages one artifact family as compact rows, including the
provider interfaces a Procedure can call.

Values lead: a predicate's accepted evidence is shown as CaptureContract NAMES,
never digests, and absent optional fields are left out of the wire rather than
sent as nulls, so the default answer stays small on a real instance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimTypeContention,
    ClaimTypeMemberDescription,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.line_dispatch import LineEnablementStopReason
from cruxible_client.contracts.operational_reads import (
    LineEnablementState,
    LiveView,
    OrientCapture,
    OrientCaptureContract,
    OrientLine,
    OrientMandate,
    OrientPrediction,
    RunRow,
    RunStatus,
)
from cruxible_client.contracts.policy_rows import PolicyInForce
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_contracts import ProviderOperationContract
from cruxible_client.contracts.types import PrincipalRecord

OrientSection: TypeAlias = Literal[
    "documents",
    "procedures",
    "claim_types",
    "subject_kinds",
    "queries",
    "interfaces",
    "runs",
    "running",
    "lines",
    "captures",
    "capture_contracts",
    "predictions",
    "mandates",
    "principals",
    "policies",
    "generations",
    "graph",
    "pins",
    "activity",
]
#: The surface a caller renders ``next`` for: tool calls, commands, or SDK calls.
OrientSurface: TypeAlias = Literal["mcp", "cli", "sdk"]

ORIENT_DEFAULT_LIMIT = 50
ORIENT_MAX_LIMIT = 500
#: Sample Subject IDs an ``orient(kind=...)`` answer carries.
ORIENT_SAMPLE_SUBJECTS = 5
#: Named queries the default answer lists before pointing at the queries section.
ORIENT_DEFAULT_QUERIES = 10
#: Attention lines the default answer carries from the ``next`` queue.
ORIENT_ATTENTION_TOP = 3
#: How many of a kind's predicates the default map names by short name.
ORIENT_KIND_FIELDS = 5


def _is_none(value: object) -> bool:
    return value is None


def _is_empty(value: object) -> bool:
    return not value


class _StrictOrientModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OrientYou(_StrictOrientModel):
    """The caller as the daemon resolved it, and whether it can author."""

    actor: str | None
    principal: str | None = Field(default=None, exclude_if=_is_none)
    can_author: bool
    # Exactly the refusal whoami reports and authoring returns: code, detail, repair.
    authoring_refusal: AuthoringRefusal | None = Field(default=None, exclude_if=_is_none)


class OrientPredicate(_StrictOrientModel):
    """One predicate of a kind: compact by default, in full under ``orient(kind=...)``.

    ``name`` is the short name a query field uses against this kind; ``predicate``
    is the full ClaimType predicate. ``type`` is the value type: a JSON type,
    ``date``/``datetime``, ``enum`` (with ``members``), ``subject`` or
    ``subject:<kinds>`` for a Subject-valued predicate, or ``exact_content``.
    ``evidence`` names the CaptureContracts the ClaimType admits; a digest that
    names no accepted contract shows as ``unresolved:<short digest>``.
    ``None`` (omitted on the wire) inherits the kind's evidence set; an empty
    tuple (``[]`` on the wire) explicitly admits no contracts. Standalone
    descriptors always carry an explicit set.

    ``description`` is the ClaimType's (v7) description: its first sentence, at
    most 160 characters, in a compact descriptor; all of it in full.
    ``evidence_requirement`` appears only when it is not ``self`` (``none``: the
    Claim's own origin supports it; ``captured``: a Capture under a declared
    contract is required). A full descriptor also carries each described enum
    member, the role a write defaults to, and ``revision_evidence``: what a
    statement-changing revision keeps (``accumulate`` before v7).
    A full descriptor also names how the ClaimType settles rival Claims
    (``contention``), its format version (``format``: 7 for
    ``playbill-claim-type-v7``), its accepted ``revision`` and its ledger
    ``path``, which a kit's drifted paths name. Under ``orient(kind=K)`` a
    descriptor counts the kind's ``live_claims`` of the predicate and
    ``subjects_with_value``: its live Subjects with at least one, both read
    from counters Claim writes maintain.
    ``kit`` (claim_types section only) is the installed kit that owns the
    ClaimType: its receipt records the definition. Absent when no kit owns it.
    """

    name: str
    predicate: str
    cardinality: Literal["one", "many"]
    type: str
    members: tuple[str | int | float | bool | None, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    description: str | None = Field(default=None, exclude_if=_is_none)
    evidence: tuple[str, ...] | None = Field(default=None, exclude_if=_is_none)
    evidence_requirement: EvidenceRequirement | None = Field(default=None, exclude_if=_is_none)
    # Full descriptors (``orient(kind=...)`` and the claim_types section) only.
    subject_kinds: tuple[str, ...] | None = Field(default=None, exclude_if=_is_none)
    roles: tuple[str, ...] | None = Field(default=None, exclude_if=_is_none)
    default_role: ClaimRole | None = Field(default=None, exclude_if=_is_none)
    member_descriptions: tuple[ClaimTypeMemberDescription, ...] = Field(
        default=(), exclude_if=_is_empty
    )
    revision_evidence: RevisionEvidence | None = Field(default=None, exclude_if=_is_none)
    stale_after: str | None = Field(default=None, exclude_if=_is_none)
    contention: ClaimTypeContention | None = Field(default=None, exclude_if=_is_none)
    format: int | None = Field(default=None, ge=1, exclude_if=_is_none)
    revision: int | None = Field(default=None, ge=1, exclude_if=_is_none)
    path: str | None = Field(default=None, exclude_if=_is_none)
    live_claims: int | None = Field(default=None, ge=0, exclude_if=_is_none)
    subjects_with_value: int | None = Field(default=None, ge=0, exclude_if=_is_none)
    kit: str | None = Field(default=None, exclude_if=_is_none)


class OrientSubjectKind(_StrictOrientModel):
    """One live SubjectKind: how core names a new Subject of the kind.

    ``rule`` is ``keys`` (named by its key ClaimTypes, in key order, ``unique``
    or ``hint``) or ``minted`` (core mints the id). A kind with no live
    SubjectKind has no row: no new Subject of it can be created. ``kit`` is
    the installed kit that owns the SubjectKind, absent when no kit owns it.
    """

    kind: str
    rule: Literal["keys", "minted"]
    keys: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    uniqueness: Literal["unique", "hint"] | None = Field(default=None, exclude_if=_is_none)
    kit: str | None = Field(default=None, exclude_if=_is_none)


#: A kind no accepted SubjectKind declares: its Subjects are grandfathered.
UNDECLARED_KIND_IDENTITY = "undeclared"


class OrientKind(_StrictOrientModel):
    """One Subject kind: how many live Subjects it has and its predicates.

    The most common admitted CaptureContract set is named once as ``evidence``.
    Predicates inheriting it omit their evidence; exceptions carry their own
    set, including an explicit empty list when no contracts are admitted.
    """

    kind: str
    subjects: int = Field(ge=0)
    #: How core names Subjects of the kind (its SubjectKind): ``keys: <key
    #: ClaimTypes> (unique|hint)``, ``minted``, or ``undeclared`` -- no new Subject
    #: of an undeclared kind can be created until a SubjectKind is authored.
    identity: str = UNDECLARED_KIND_IDENTITY
    evidence: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    predicates: tuple[OrientPredicate, ...]


class OrientKindSummary(_StrictOrientModel):
    """One Subject kind in the default map: its live Subjects and first fields.

    ``fields`` names its first predicates by short name (byte order), at most
    ``ORIENT_KIND_FIELDS``; ``more_fields`` counts the rest. ``identity`` is
    as on ``OrientKind``. ``orient(kind=...)`` reads every predicate in full.
    """

    kind: str
    subjects: int = Field(ge=0)
    identity: str = UNDECLARED_KIND_IDENTITY
    fields: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    more_fields: int = Field(default=0, ge=0, exclude_if=lambda value: value == 0)


class OrientKindDetail(OrientKind):
    """One kind in full, with a few Subject IDs to read next.

    ``incoming`` names, in full, the Subject-valued predicates of other kinds
    whose values may name this kind's Subjects: the reverse follows a ``query``
    on this kind can take (``follow`` with ``direction: "reverse"``). It is
    omitted when nothing points at the kind.
    """

    incoming: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    sample_subject_ids: tuple[str, ...] = ()


class OrientClaimCounts(_StrictOrientModel):
    """Every accepted Claim by its status at the coordinate.

    ``accepted`` Claims answer their slot; ``conflicted`` ones are contenders
    resolution left unresolved; ``overturned`` ones lost their slot to an
    accepted rival; ``ineligible`` ones are not eligible to resolve under their
    own ClaimType (a verdict other than supported, or erased evidence read
    stale);
    ``retired`` ones were withdrawn. ``query(..., status=[...])`` lists the
    non-live ones.
    """

    accepted: int = Field(default=0, ge=0)
    conflicted: int = Field(default=0, ge=0)
    overturned: int = Field(default=0, ge=0)
    ineligible: int = Field(default=0, ge=0)
    retired: int = Field(default=0, ge=0)


class OrientArtifactCounts(_StrictOrientModel):
    """How many of each family exist; each operational family is its own section.

    ``lines``, ``capture_contracts``, ``resolution_contracts`` and ``mandates``
    count accepted artifacts, ``captures`` the Captures accepted Claims cite,
    and ``runs`` / ``running`` the Procedure runs admitted and still running.
    """

    claim_types: int = Field(ge=0)
    procedures: int = Field(ge=0)
    documents: int = Field(ge=0)
    queries: int = Field(ge=0)
    interfaces: int = Field(ge=0)
    lines: int = Field(default=0, ge=0)
    captures: int = Field(default=0, ge=0)
    capture_contracts: int = Field(default=0, ge=0)
    resolution_contracts: int = Field(default=0, ge=0)
    mandates: int = Field(default=0, ge=0)
    runs: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    claims: OrientClaimCounts | None = Field(default=None, exclude_if=_is_none)


class OrientQuery(_StrictOrientModel):
    """One named QueryDefinition; params read ``name: type`` with ``?`` when optional."""

    name: str
    description: str | None = Field(default=None, exclude_if=_is_none)
    params: tuple[str, ...] = ()


class OrientDocument(_StrictOrientModel):
    name: str
    title: str
    document_kind: str
    media_type: str


class OrientProcedure(_StrictOrientModel):
    name: str
    lifecycle: Literal["live", "retired"]
    #: ``direct``: procedure run; ``line``: only as a Line (its terminals act
    #: outward); ``unsupported``: no run path admits it.
    runnable: Literal["direct", "line", "unsupported"]


class OrientInterfaceProvider(_StrictOrientModel):
    """One live Provider implementing an interface, and the implementation it pins."""

    provider: str


class OrientInterface(_StrictOrientModel):
    """One live provider interface a Procedure node can call.

    ``input`` and ``output`` list the operation contract's fields as
    ``name: type`` (``?`` when optional); an acquisition interface's output is
    the named external-capture contract instead. ``effect`` is the interface's
    governed effect class: an ``external_mutation`` call needs an effect policy
    on its node. ``providers`` names the live Providers implementing it; one
    with none has nothing to run it yet. ``operation_contract`` describes the
    node's input and output. ``get("ProviderInterface:<name>")`` reads the
    whole accepted entry. Admission records the exact versions selected by name.
    """

    name: str
    description: str | None = Field(default=None, exclude_if=_is_none)
    input: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    effect: Literal["pure", "external_read", "external_mutation"]
    providers: tuple[OrientInterfaceProvider, ...] = ()
    operation_contract: ProviderOperationContract | None = Field(default=None, exclude_if=_is_none)


class OrientEnablements(_StrictOrientModel):
    """The instance's enabled Lines as its Line consumer reports them, no daemon scope needed.

    ``running`` enablements are admitting their own due work; ``stalled`` ones have
    due work older than the stall horizon; ``stopped`` ones stopped for any
    reason but a deliberate disable. ``needs_attention`` names up to three
    stalled or stopped Lines (``Line:<name> stopped (<reason>)``), each a
    ``get`` reference.
    """

    running: int = Field(default=0, ge=0)
    stalled: int = Field(default=0, ge=0)
    stopped: int = Field(default=0, ge=0)
    needs_attention: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)


class OrientAttention(_StrictOrientModel):
    """What the ``next`` queue holds, and anything else the instance needs."""

    next_items: int = Field(ge=0)
    open_proposals: int = Field(ge=0)
    top: tuple[str, ...] = ()
    notes: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    # Present when any Line was ever enabled on this instance.
    enablements: OrientEnablements | None = Field(default=None, exclude_if=_is_none)


class OrientFloor(_StrictOrientModel):
    """The workspace's greppable floor: the coordinate it is at, and how stale.

    Only a client that sees the workspace (CLI, MCP, SDK) fills this in, from
    the floor's own manifest; a daemon answer never carries it.
    ``generations_behind`` counts accepted generations from the floor to this
    answer's coordinate; ``None`` when the floor predates generation stamps.
    """

    at: str
    generations_behind: int | None = Field(ge=0)


class Head(_StrictOrientModel):
    """The accepted head (or the coordinate ``at`` names) and its generation.

    The cheapest read there is: no kind, artifact or attention fold, only the
    coordinate a caller pins its next reads to and the generation a floor's
    freshness is counted in. Internal callers use it in place of ``orient``.
    """

    tag: Literal["playbill-head-v1"] = "playbill-head-v1"
    instance: str
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)


#: The most characters of a generation's ``because`` an orient row carries.
ORIENT_BECAUSE_MAX_CHARS = 500


class OrientGeneration(_StrictOrientModel):
    """One accepted generation, newest first: what accepted it and how much it changed.

    ``changeset_digest`` names the change-set record; ``proposal`` is the
    proposal whose admission it accepted (``get <proposal>`` reads its card and
    changes), ``proposer`` the actor bound to it, and ``approvals`` the
    approving signers in record order. ``because`` is that admission's
    rationale, cut at 500 characters (``because_truncated``). ``members``
    counts the change set's members; ``since(generation=sequence-1,
    at=<this generation>)`` lists them. Genesis (sequence 0) carries only its
    sequence, git oid and acceptance time. Pass ``sequence`` or ``git_oid`` as
    ``at`` to read state as this generation left it.
    """

    sequence: int = Field(ge=0)
    git_oid: str
    accepted_at: datetime
    changeset_digest: str | None = Field(default=None, exclude_if=_is_none)
    proposal: str | None = Field(default=None, exclude_if=_is_none)
    proposer: str | None = Field(default=None, exclude_if=_is_none)
    approvals: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    because: str | None = Field(default=None, exclude_if=_is_none)
    because_truncated: bool = Field(default=False, exclude_if=lambda value: value is False)
    members: int = Field(default=0, ge=0)


class _OrientActivityRow(_StrictOrientModel):
    """What every activity row carries: when it happened, as its own record says.

    ``at`` is the instant the record behind the row carries, never the read's
    clock; each event's docstring says which. Rows are newest first by it,
    except that a generation is placed no later than the one accepted before
    it in sequence, whatever its own time says.
    """

    #: The record's own instant: a journal record's, or a generation's acceptance.
    at: datetime


class OrientActivityGenerationAccepted(_OrientActivityRow):
    """The ledger accepted a generation; the fields are an ``OrientGeneration``'s.

    ``ref`` (and ``proposal``) is the proposal whose admission accepted it, which
    ``get`` reads; genesis has neither. ``because`` is cut at 500 characters
    (``because_truncated``). ``at`` is its acceptance time (the accepted
    candidate's timestamp, or genesis's commit time); a candidate's timestamp
    need not follow sequence, so the row is placed no later than the
    generation accepted after it.
    """

    event: Literal["generation_accepted"] = "generation_accepted"
    ref: str | None = Field(default=None, exclude_if=_is_none)
    sequence: int = Field(ge=0)
    git_oid: str
    proposal: str | None = Field(default=None, exclude_if=_is_none)
    proposer: str | None = Field(default=None, exclude_if=_is_none)
    approvals: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    because: str | None = Field(default=None, exclude_if=_is_none)
    because_truncated: bool = Field(default=False, exclude_if=lambda value: value is False)
    members: int = Field(default=0, ge=0)


class OrientActivityOccurrenceDue(_OrientActivityRow):
    """A Trigger made one occurrence of a Line pending work, eligible from ``eligible_at``.

    ``trigger`` is the Trigger that derived it, absent for an explicit
    evaluation that names none. ``ref`` is the Line. ``at`` is when the
    evaluation that found it recorded it pending.
    """

    event: Literal["occurrence_due"] = "occurrence_due"
    ref: str
    line: str
    trigger: str | None = Field(default=None, exclude_if=_is_none)
    occurrence: str
    eligible_at: datetime


class OrientActivityOccurrenceLapsed(_OrientActivityRow):
    """A pending timed tick lapsed as a new enablement opened; a retry still runs it.

    ``at`` is when the enablement that lapsed it was recorded.
    """

    event: Literal["occurrence_lapsed"] = "occurrence_lapsed"
    ref: str
    line: str
    occurrence: str


class OrientActivityRunStarted(_OrientActivityRow):
    """A Procedure run was admitted; ``ref`` (``ProcedureRun:<run>``) reads it.

    ``at`` is the run's evaluation instant, the ``started_at`` the runs
    section shows. ``unavailable`` marks a run whose admission payload is
    lost: the run is still listed, without its Procedure or Line.
    """

    event: Literal["run_started"] = "run_started"
    ref: str
    run: str
    procedure: str | None = Field(default=None, exclude_if=_is_none)
    line: str | None = Field(default=None, exclude_if=_is_none)
    unavailable: bool = Field(default=False, exclude_if=lambda value: value is False)


class OrientActivityRunFinished(_OrientActivityRow):
    """A Procedure run finished with ``status``, in the runs section's vocabulary.

    ``at`` is the run's evaluation instant, not the wall time it finished:
    every record of a run carries that one instant (the executor's
    deterministic clock), so a finish lists beside its start, above it.
    ``unavailable`` marks a lost payload: a lost final payload reads as
    ``operational_failed``, as the runs section shows it.
    """

    event: Literal["run_finished"] = "run_finished"
    ref: str
    run: str
    procedure: str | None = Field(default=None, exclude_if=_is_none)
    line: str | None = Field(default=None, exclude_if=_is_none)
    status: RunStatus
    unavailable: bool = Field(default=False, exclude_if=lambda value: value is False)


class OrientActivityCaptureLanded(_OrientActivityRow):
    """A run landed a Capture: ``capture`` is its handle, ``ref`` its ``Capture:<digest>``.

    ``contract`` names the CaptureContract it was taken under and
    ``logical_source`` the external source it read, when it read one. ``at``
    is the landing run's evaluation instant, like its start and finish.
    ``unavailable`` marks a lost payload or an unreadable Capture: the landing
    is listed by its run alone.
    """

    event: Literal["capture_landed"] = "capture_landed"
    ref: str | None = Field(default=None, exclude_if=_is_none)
    capture: str | None = Field(default=None, exclude_if=_is_none)
    contract: str | None = Field(default=None, exclude_if=_is_none)
    logical_source: str | None = Field(default=None, exclude_if=_is_none)
    run: str
    line: str | None = Field(default=None, exclude_if=_is_none)
    unavailable: bool = Field(default=False, exclude_if=lambda value: value is False)


class OrientActivityInboxPosted(_OrientActivityRow):
    """A Line's run posted a new inbox item; ``ref`` is its id, which ``inbox get`` reads.

    Only a new item is listed: a run posting the same payload again counts a
    sighting on the item and is not new work. ``at`` is when the posting was
    recorded: a run posts at its own evaluation instant.
    """

    event: Literal["inbox_posted"] = "inbox_posted"
    ref: str
    line: str
    procedure: str
    run: str


class OrientActivityInboxDisposed(_OrientActivityRow):
    """An agent recorded a decision on an inbox item: acknowledged, dismissed or linked.

    ``at`` is when the decision was recorded.
    """

    event: Literal["inbox_disposed"] = "inbox_disposed"
    ref: str
    line: str
    disposition: Literal["acknowledged", "dismissed", "linked"]


class OrientActivityLineStateChanged(_OrientActivityRow):
    """A Line's enablement moved from ``previous`` to ``state``; ``ref`` is the Line.

    The states read as the ``lines`` section reads them: enabling makes a Line
    ``running``; a stop makes it ``stopped`` (``reason`` says why), or
    ``disabled`` when deliberately disabled or stopped with no reason (a
    segment ending before its successor opens). ``previous`` is absent the
    first time a Line is enabled. A daemon restart keeps a running Line
    running and is not listed; ``stalled`` is read from due work, never
    recorded, so it never appears here. ``at`` is when the enable or stop was
    recorded.
    """

    event: Literal["line_state_changed"] = "line_state_changed"
    ref: str
    line: str
    previous: LineEnablementState | None = Field(default=None, exclude_if=_is_none)
    state: LineEnablementState
    reason: LineEnablementStopReason | None = Field(default=None, exclude_if=_is_none)
    detail: str | None = Field(default=None, exclude_if=_is_none)


class OrientActivityCaptureErased(_OrientActivityRow):
    """A Capture's retained body was erased, by ``by``; ``ref`` is the Capture.

    ``at`` is when the erasure was recorded. ``unavailable`` marks a lost
    erasure payload: the erasure is listed without the Capture it names.
    """

    event: Literal["capture_erased"] = "capture_erased"
    ref: str | None = Field(default=None, exclude_if=_is_none)
    capture: str | None = Field(default=None, exclude_if=_is_none)
    contract: str | None = Field(default=None, exclude_if=_is_none)
    by: str | None = Field(default=None, exclude_if=_is_none)
    unavailable: bool = Field(default=False, exclude_if=lambda value: value is False)


class OrientActivityRecordUnavailable(_OrientActivityRow):
    """A Line dispatch or inbox record whose payload is lost: what it recorded is unknown.

    ``at`` is the lost record's own instant. A lost inbox payload also stops
    the inbox before the records after it (``cruxible.inbox.unavailable``),
    so none of those are listed until it is restored.
    """

    event: Literal["record_unavailable"] = "record_unavailable"
    journal: Literal["line_dispatch", "line_inbox"]


#: One row of the activity feed, by ``event``.
OrientActivity: TypeAlias = Annotated[
    OrientActivityGenerationAccepted
    | OrientActivityOccurrenceDue
    | OrientActivityOccurrenceLapsed
    | OrientActivityRunStarted
    | OrientActivityRunFinished
    | OrientActivityCaptureLanded
    | OrientActivityInboxPosted
    | OrientActivityInboxDisposed
    | OrientActivityLineStateChanged
    | OrientActivityCaptureErased
    | OrientActivityRecordUnavailable,
    Field(discriminator="event"),
]


class OrientGraphNode(_StrictOrientModel):
    """One live Subject: ``subject`` is its ``kind/id`` ref, which ``get`` reads."""

    subject: str
    kind: str


class OrientGraphEdge(_StrictOrientModel):
    """One live Claim whose object is a Subject: ``source`` --predicate--> ``target``.

    ``claim`` is the Claim's id (``get`` reads it); ``source`` and ``target``
    are ``kind/id`` refs. A relation is stored once, on its source.
    """

    claim: str
    source: str
    predicate: str
    qualifier: str | None = Field(default=None, exclude_if=_is_none)
    target: str


class OrientGraphTotals(_StrictOrientModel):
    """The whole graph's size at the coordinate, on every page.

    ``subjects`` is the live Subject count, read from the per-kind counters
    the projection maintains (never a recount).
    """

    subjects: int = Field(ge=0)


class OrientGraph(_StrictOrientModel):
    """One page of the Subject graph: live Subjects, then live Subject-valued Claims.

    A page holds up to ``limit`` rows: nodes first (by kind, then Subject),
    and once the nodes are done, edges (by target, then Claim). Follow
    ``next_cursor`` while ``truncated``; every page reads at the first page's
    coordinate.
    """

    nodes: tuple[OrientGraphNode, ...] = ()
    edges: tuple[OrientGraphEdge, ...] = ()
    totals: OrientGraphTotals


#: The definition kinds whose pins ``orient(section="pins")`` lists, in byte order.
ORIENT_PIN_SOURCE_KINDS: tuple[str, ...] = (
    "Blueprint",
    "CaptureContract",
    "ClaimType",
    "Line",
    "Procedure",
    "ProcedureMandate",
    "Provider",
    "ProviderInterface",
    "QueryDefinition",
    "ResolutionContract",
    "SourceAcquisitionPolicy",
    "SubjectKind",
    "Trigger",
)


class OrientPin(_StrictOrientModel):
    """One dependency of a live definition, identified by source, field and target.

    ``current`` says whether the reference resolves to a live target.
    Read in reverse, the rows name who
    uses a contract, a ClaimType or a Procedure: the Procedures that capture
    under a contract (role ``capture-contract``), the Lines on a Procedure,
    the mandates that settle a ClaimType.
    """

    source: str
    role: str
    target: str
    current: bool | None = Field(default=None, exclude_if=_is_none)


class OrientResult(_StrictOrientModel):
    """One orient answer; which parts are present depends on the request.

    - no ``kind``/``section``: ``you``, ``kinds`` (paged by ``limit``/``cursor``),
      ``artifacts``, ``queries`` and ``attention``;
    - ``kind``: ``kind_detail``;
    - ``section``: that section's rows (``documents``, ``procedures``,
      ``claim_types``, ``subject_kinds``, ``queries``, ``interfaces``, or an
      operational family:
      ``runs``, ``running``, ``lines``, ``captures``, ``capture_contracts``,
      ``predictions``, ``mandates``), the principal registry (``principals``),
      the governed policies in force (``policies``), the accepted generations
      newest first (``generations``), the Subject graph (``graph``), the
      pins between live definitions (``pins``) or what just happened, newest
      first (``activity``), paged.
      The default map counts the operational families under ``artifacts`` and
      never inlines their rows.

    ``next`` is rendered for the requesting surface.
    """

    tag: Literal["playbill-orient-v1"] = "playbill-orient-v1"
    instance: str
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    accepted_at: datetime
    evaluation_time: datetime
    mirror_url: str | None = Field(default=None, exclude_if=_is_none)
    floor: OrientFloor | None = Field(default=None, exclude_if=_is_none)
    you: OrientYou | None = Field(default=None, exclude_if=_is_none)
    kinds: tuple[OrientKindSummary, ...] | None = Field(default=None, exclude_if=_is_none)
    artifacts: OrientArtifactCounts | None = Field(default=None, exclude_if=_is_none)
    attention: OrientAttention | None = Field(default=None, exclude_if=_is_none)
    kind_detail: OrientKindDetail | None = Field(default=None, exclude_if=_is_none)
    section: OrientSection | None = Field(default=None, exclude_if=_is_none)
    documents: tuple[OrientDocument, ...] | None = Field(default=None, exclude_if=_is_none)
    procedures: tuple[OrientProcedure, ...] | None = Field(default=None, exclude_if=_is_none)
    claim_types: tuple[OrientPredicate, ...] | None = Field(default=None, exclude_if=_is_none)
    subject_kinds: tuple[OrientSubjectKind, ...] | None = Field(default=None, exclude_if=_is_none)
    queries: tuple[OrientQuery, ...] | None = Field(default=None, exclude_if=_is_none)
    interfaces: tuple[OrientInterface, ...] | None = Field(default=None, exclude_if=_is_none)
    # Procedure runs, newest admission first (section "runs"), or only the runs
    # still running (section "running"). Runs are operational state, read live.
    runs: tuple[RunRow, ...] | None = Field(default=None, exclude_if=_is_none)
    # Accepted Lines, with each arm's state and pending counts at the head.
    lines: tuple[OrientLine, ...] | None = Field(default=None, exclude_if=_is_none)
    # Captures accepted Claims cite, newest observation first (paged by key).
    captures: tuple[OrientCapture, ...] | None = Field(default=None, exclude_if=_is_none)
    capture_contracts: tuple[OrientCaptureContract, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    # Live ResolutionContracts with their bound windows counted by status.
    predictions: tuple[OrientPrediction, ...] | None = Field(default=None, exclude_if=_is_none)
    mandates: tuple[OrientMandate, ...] | None = Field(default=None, exclude_if=_is_none)
    # The principal registry at the coordinate: every public record, active or
    # revoked. ``get("Principal:<id>")`` reads one.
    principals: tuple[PrincipalRecord, ...] | None = Field(default=None, exclude_if=_is_none)
    # Every live governed policy, standalone or embedded in its declaring
    # artifact; ``get`` reads the declaring artifact (``ApprovalPolicy:instance``).
    policies: tuple[PolicyInForce, ...] | None = Field(default=None, exclude_if=_is_none)
    # Accepted generations newest first from the answer's own, paged by sequence
    # (section "generations"): the ledger's timeline.
    generations: tuple[OrientGeneration, ...] | None = Field(default=None, exclude_if=_is_none)
    # Live Subjects and the live Claims between them (section "graph"), keyset-paged.
    graph: OrientGraph | None = Field(default=None, exclude_if=_is_none)
    # The required pins of live definitions (section "pins"), by source then
    # ordinal, keyset-paged; never a Claim's, a Subject's or an attestation's.
    pins: tuple[OrientPin, ...] | None = Field(default=None, exclude_if=_is_none)
    # What just happened, newest first (section "activity"): accepted generations,
    # Line occurrences and enablement changes, run starts and finishes, Captures
    # landed and erased, inbox postings and decisions. Read live, keyset-paged.
    activity: tuple[OrientActivity, ...] | None = Field(default=None, exclude_if=_is_none)
    truncated: bool = False
    next_cursor: str | None = Field(default=None, exclude_if=_is_none)
    # Present when part of the answer is operational state -- runs, Line enablements and
    # pending counts, prediction windows -- read live at the current head
    # (``live.as_of``) whatever ``coordinate`` the read named.
    live: LiveView | None = Field(default=None, exclude_if=_is_none)
    next: tuple[str, ...] = ()


__all__ = [
    "ORIENT_ATTENTION_TOP",
    "ORIENT_BECAUSE_MAX_CHARS",
    "ORIENT_DEFAULT_LIMIT",
    "ORIENT_DEFAULT_QUERIES",
    "ORIENT_KIND_FIELDS",
    "ORIENT_MAX_LIMIT",
    "ORIENT_PIN_SOURCE_KINDS",
    "ORIENT_SAMPLE_SUBJECTS",
    "UNDECLARED_KIND_IDENTITY",
    "Head",
    "OrientActivity",
    "OrientActivityCaptureErased",
    "OrientActivityCaptureLanded",
    "OrientActivityGenerationAccepted",
    "OrientActivityInboxDisposed",
    "OrientActivityInboxPosted",
    "OrientActivityLineStateChanged",
    "OrientActivityOccurrenceDue",
    "OrientActivityOccurrenceLapsed",
    "OrientActivityRecordUnavailable",
    "OrientActivityRunFinished",
    "OrientActivityRunStarted",
    "OrientEnablements",
    "OrientArtifactCounts",
    "OrientAttention",
    "OrientClaimCounts",
    "OrientDocument",
    "OrientFloor",
    "OrientGeneration",
    "OrientGraph",
    "OrientGraphEdge",
    "OrientGraphNode",
    "OrientGraphTotals",
    "OrientPin",
    "OrientInterfaceProvider",
    "OrientInterface",
    "OrientKindDetail",
    "OrientKind",
    "OrientKindSummary",
    "OrientPredicate",
    "OrientProcedure",
    "OrientQuery",
    "OrientResult",
    "OrientSection",
    "OrientSubjectKind",
    "OrientSurface",
    "OrientYou",
]
