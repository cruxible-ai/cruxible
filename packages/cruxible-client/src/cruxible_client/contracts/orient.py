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
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimTypeMemberDescription,
    EvidenceRequirement,
    RevisionEvidence,
)
from cruxible_client.contracts.operational_reads import (
    LiveView,
    OrientCapture,
    OrientCaptureContract,
    OrientLine,
    OrientMandate,
    OrientPrediction,
    RunRow,
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
    live_claims: int | None = Field(default=None, ge=0, exclude_if=_is_none)


class OrientKind(_StrictOrientModel):
    """One Subject kind: how many live Subjects it has and its predicates.

    The most common admitted CaptureContract set is named once as ``evidence``.
    Predicates inheriting it omit their evidence; exceptions carry their own
    set, including an explicit empty list when no contracts are admitted.
    """

    kind: str
    subjects: int = Field(ge=0)
    evidence: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    predicates: tuple[OrientPredicate, ...]


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
    accepted rival; ``refused`` ones failed their own ClaimType's admission;
    ``retired`` ones were withdrawn. ``query(..., status=[...])`` lists the
    non-live ones.
    """

    accepted: int = Field(default=0, ge=0)
    conflicted: int = Field(default=0, ge=0)
    overturned: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0)
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
    implementation_digest: str


class OrientInterface(_StrictOrientModel):
    """One live provider interface a Procedure node can call.

    ``input`` and ``output`` list the operation contract's fields as
    ``name: type`` (``?`` when optional); an acquisition interface's output is
    the named external-capture contract instead. ``effect`` is the interface's
    governed effect class: an ``external_mutation`` call needs an effect policy
    on its node. ``providers`` are the live Providers implementing it, each
    with the implementation digest a Procedure node pins; one with none has
    nothing to run it yet. ``interface_digest`` and ``operation_contract`` are
    what a node pins the interface by; ``get("ProviderInterface:<name>")``
    reads the whole accepted entry.
    """

    name: str
    description: str | None = Field(default=None, exclude_if=_is_none)
    input: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    effect: Literal["none", "external_read", "external_mutation"]
    providers: tuple[OrientInterfaceProvider, ...] = ()
    interface_digest: str
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


class OrientResult(_StrictOrientModel):
    """One orient answer; which parts are present depends on the request.

    - no ``kind``/``section``: ``you``, ``kinds`` (paged by ``limit``/``cursor``),
      ``artifacts``, ``queries`` and ``attention``;
    - ``kind``: ``kind_detail``;
    - ``section``: that section's rows (``documents``, ``procedures``,
      ``claim_types``, ``queries``, ``interfaces``, or an operational family:
      ``runs``, ``running``, ``lines``, ``captures``, ``capture_contracts``,
      ``predictions``, ``mandates``), the principal registry (``principals``)
      or the governed policies in force (``policies``), paged. The default map counts the
      operational families under ``artifacts`` and never inlines their rows.

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
    kinds: tuple[OrientKind, ...] | None = Field(default=None, exclude_if=_is_none)
    artifacts: OrientArtifactCounts | None = Field(default=None, exclude_if=_is_none)
    attention: OrientAttention | None = Field(default=None, exclude_if=_is_none)
    kind_detail: OrientKindDetail | None = Field(default=None, exclude_if=_is_none)
    section: OrientSection | None = Field(default=None, exclude_if=_is_none)
    documents: tuple[OrientDocument, ...] | None = Field(default=None, exclude_if=_is_none)
    procedures: tuple[OrientProcedure, ...] | None = Field(default=None, exclude_if=_is_none)
    claim_types: tuple[OrientPredicate, ...] | None = Field(default=None, exclude_if=_is_none)
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
    truncated: bool = False
    next_cursor: str | None = Field(default=None, exclude_if=_is_none)
    # Present when part of the answer is operational state -- runs, Line enablements and
    # pending counts, prediction windows -- read live at the current head
    # (``live.as_of``) whatever ``coordinate`` the read named.
    live: LiveView | None = Field(default=None, exclude_if=_is_none)
    next: tuple[str, ...] = ()


__all__ = [
    "ORIENT_ATTENTION_TOP",
    "ORIENT_DEFAULT_LIMIT",
    "ORIENT_DEFAULT_QUERIES",
    "ORIENT_MAX_LIMIT",
    "ORIENT_SAMPLE_SUBJECTS",
    "Head",
    "OrientEnablements",
    "OrientArtifactCounts",
    "OrientAttention",
    "OrientClaimCounts",
    "OrientDocument",
    "OrientFloor",
    "OrientInterfaceProvider",
    "OrientInterface",
    "OrientKindDetail",
    "OrientKind",
    "OrientPredicate",
    "OrientProcedure",
    "OrientQuery",
    "OrientResult",
    "OrientSection",
    "OrientSurface",
    "OrientYou",
]
