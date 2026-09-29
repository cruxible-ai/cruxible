"""The orient read: one bounded map of an instance's accepted state.

``orient`` answers "what is here and where do I start" in one call: the Subject
kinds with their counts and compact predicate descriptors, how many of each
artifact family exist, the named queries, who the caller is and whether it can
author, what needs attention, and runnable follow-up calls. ``orient(kind=K)``
widens one kind to every predicate in full plus sample Subject IDs, and
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

from cruxible_client.contracts.projection import AcceptedCoordinate

PlaybillOrientSection: TypeAlias = Literal[
    "documents", "procedures", "claim_types", "queries", "interfaces"
]
#: The surface a caller renders ``next`` for: tool calls, commands, or SDK calls.
PlaybillOrientSurface: TypeAlias = Literal["mcp", "cli", "sdk"]

PLAYBILL_ORIENT_DEFAULT_LIMIT = 50
PLAYBILL_ORIENT_MAX_LIMIT = 500
#: Sample Subject IDs an ``orient(kind=...)`` answer carries.
PLAYBILL_ORIENT_SAMPLE_SUBJECTS = 5
#: Named queries the default answer lists before pointing at the queries section.
PLAYBILL_ORIENT_DEFAULT_QUERIES = 10
#: Attention lines the default answer carries from the ``next`` queue.
PLAYBILL_ORIENT_ATTENTION_TOP = 3


def _is_none(value: object) -> bool:
    return value is None


def _is_empty(value: object) -> bool:
    return not value


class _StrictOrientModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlaybillOrientYouV1(_StrictOrientModel):
    """The caller as the daemon resolved it, and whether it can author."""

    actor: str | None
    principal: str | None = Field(default=None, exclude_if=_is_none)
    can_author: bool
    reason: str | None = Field(default=None, exclude_if=_is_none)


class PlaybillOrientPredicateV1(_StrictOrientModel):
    """One predicate of a kind: compact by default, in full under ``orient(kind=...)``.

    ``name`` is the short name a query field uses against this kind; ``predicate``
    is the full ClaimType predicate. ``type`` is the value type: a JSON type,
    ``date``/``datetime``, ``enum`` (with ``members``), ``subject`` or
    ``subject:<kinds>`` for a Subject-valued predicate, or ``exact_content``.
    ``evidence`` names the CaptureContracts the ClaimType admits; a digest that
    names no accepted contract shows as ``unresolved:<short digest>``.
    """

    name: str
    predicate: str
    cardinality: Literal["one", "many"]
    type: str
    members: tuple[str | int | float | bool | None, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    description: str | None = Field(default=None, exclude_if=_is_none)
    evidence: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    # Full descriptors (``orient(kind=...)`` and the claim_types section) only.
    subject_kinds: tuple[str, ...] | None = Field(default=None, exclude_if=_is_none)
    roles: tuple[str, ...] | None = Field(default=None, exclude_if=_is_none)
    stale_after: str | None = Field(default=None, exclude_if=_is_none)
    live_claims: int | None = Field(default=None, ge=0, exclude_if=_is_none)


class PlaybillOrientKindV1(_StrictOrientModel):
    """One Subject kind: how many live Subjects it has and its predicates.

    When every predicate of the kind admits the same CaptureContracts, the
    compact map names them once here as ``evidence`` and the predicates leave
    theirs out; otherwise each predicate carries its own.
    """

    kind: str
    subjects: int = Field(ge=0)
    evidence: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)
    predicates: tuple[PlaybillOrientPredicateV1, ...]


class PlaybillOrientKindDetailV1(PlaybillOrientKindV1):
    """One kind in full, with a few Subject IDs to read next."""

    sample_subject_ids: tuple[str, ...] = ()


class PlaybillOrientArtifactCountsV1(_StrictOrientModel):
    claim_types: int = Field(ge=0)
    procedures: int = Field(ge=0)
    documents: int = Field(ge=0)
    queries: int = Field(ge=0)
    interfaces: int = Field(ge=0)


class PlaybillOrientQueryV1(_StrictOrientModel):
    """One named QueryDefinition; params read ``name: type`` with ``?`` when optional."""

    name: str
    description: str | None = Field(default=None, exclude_if=_is_none)
    params: tuple[str, ...] = ()


class PlaybillOrientDocumentV1(_StrictOrientModel):
    name: str
    title: str
    document_kind: str
    media_type: str


class PlaybillOrientProcedureV1(_StrictOrientModel):
    name: str
    lifecycle: Literal["live", "retired"]
    runnable: Literal["directly_runnable", "binding_required"]


class PlaybillOrientInterfaceV1(_StrictOrientModel):
    """One live provider interface a Procedure node can call.

    ``input`` and ``output`` list the operation contract's fields as
    ``name: type`` (``?`` when optional); an acquisition interface's output is
    the named external-capture contract instead. ``effect`` is the interface's
    governed effect class: an ``external_mutation`` call needs an effect policy
    on its node. ``providers`` are the live Providers implementing it; one with
    none has nothing to run it yet.
    """

    name: str
    description: str | None = Field(default=None, exclude_if=_is_none)
    input: tuple[str, ...] = ()
    output: tuple[str, ...] = ()
    effect: Literal["none", "external_read", "external_mutation"]
    providers: tuple[str, ...] = ()


class PlaybillOrientAttentionV1(_StrictOrientModel):
    """What the ``next`` queue holds, and anything else the instance needs."""

    next_items: int = Field(ge=0)
    open_proposals: int = Field(ge=0)
    top: tuple[str, ...] = ()
    notes: tuple[str, ...] = Field(default=(), exclude_if=_is_empty)


class PlaybillOrientResultV1(_StrictOrientModel):
    """One orient answer; which parts are present depends on the request.

    - no ``kind``/``section``: ``you``, ``kinds`` (paged by ``limit``/``cursor``),
      ``artifacts``, ``queries`` and ``attention``;
    - ``kind``: ``kind_detail``;
    - ``section``: that section's rows (``documents``, ``procedures``,
      ``claim_types``, ``queries`` or ``interfaces``), paged.

    ``next`` is rendered for the requesting surface.
    """

    tag: Literal["playbill-orient-v1"] = "playbill-orient-v1"
    instance: str
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    accepted_at: datetime
    evaluation_time: datetime
    mirror_url: str | None = Field(default=None, exclude_if=_is_none)
    you: PlaybillOrientYouV1 | None = Field(default=None, exclude_if=_is_none)
    kinds: tuple[PlaybillOrientKindV1, ...] | None = Field(default=None, exclude_if=_is_none)
    artifacts: PlaybillOrientArtifactCountsV1 | None = Field(default=None, exclude_if=_is_none)
    attention: PlaybillOrientAttentionV1 | None = Field(default=None, exclude_if=_is_none)
    kind_detail: PlaybillOrientKindDetailV1 | None = Field(default=None, exclude_if=_is_none)
    section: PlaybillOrientSection | None = Field(default=None, exclude_if=_is_none)
    documents: tuple[PlaybillOrientDocumentV1, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    procedures: tuple[PlaybillOrientProcedureV1, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    claim_types: tuple[PlaybillOrientPredicateV1, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    queries: tuple[PlaybillOrientQueryV1, ...] | None = Field(default=None, exclude_if=_is_none)
    interfaces: tuple[PlaybillOrientInterfaceV1, ...] | None = Field(
        default=None, exclude_if=_is_none
    )
    truncated: bool = False
    next_cursor: str | None = Field(default=None, exclude_if=_is_none)
    next: tuple[str, ...] = ()


__all__ = [
    "PLAYBILL_ORIENT_ATTENTION_TOP",
    "PLAYBILL_ORIENT_DEFAULT_LIMIT",
    "PLAYBILL_ORIENT_DEFAULT_QUERIES",
    "PLAYBILL_ORIENT_MAX_LIMIT",
    "PLAYBILL_ORIENT_SAMPLE_SUBJECTS",
    "PlaybillOrientArtifactCountsV1",
    "PlaybillOrientAttentionV1",
    "PlaybillOrientDocumentV1",
    "PlaybillOrientInterfaceV1",
    "PlaybillOrientKindDetailV1",
    "PlaybillOrientKindV1",
    "PlaybillOrientPredicateV1",
    "PlaybillOrientProcedureV1",
    "PlaybillOrientQueryV1",
    "PlaybillOrientResultV1",
    "PlaybillOrientSection",
    "PlaybillOrientSurface",
    "PlaybillOrientYouV1",
]
