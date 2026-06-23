"""Typed helpers for relationship assertion state.

Provenance explains where an edge came from. Assertion state explains how
Cruxible should treat the edge in live graph semantics.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import ensure_utc, format_datetime, is_effective

RelationshipReviewStatus = Literal[
    "unreviewed",
    "pending",
    "approved",
    "rejected",
]

RelationshipReviewSource = Literal["system", "human", "agent", "group"]

RelationshipLifecycleStatus = Literal[
    "active",
    "inactive",
    "superseded",
    "retracted",
]

EntityLifecycleStatus = Literal[
    "live",
    "superseded",
    "retired",
    "orphaned",
]

# Per-kind status vocabularies stay DISTINCT (relationship vs entity); only the
# surrounding lifecycle structure is shared. ``StatusT`` is the per-kind status
# Literal a concrete lifecycle narrows the shared base to.
StatusT = TypeVar("StatusT")


class RelationshipReviewState(BaseModel):
    """Review/adjudication state for a relationship assertion."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    status: RelationshipReviewStatus = "unreviewed"
    source: RelationshipReviewSource = "system"
    updated_at: datetime | None = None
    updated_by: str | None = None
    actor_context: GovernedActorContext | None = None

    @field_validator("updated_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer("updated_at", when_used="json")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class LifecycleState(BaseModel, Generic[StatusT]):
    """Shared lifecycle/actuality structure for entities and relationships.

    The lifecycle axis is the same shape on both kinds: a per-kind ``status`` plus
    a ``reason``, an effective window, a shared ``closed_at``/``closed_by`` audit
    pair, and supersession links. Only ``status`` differs by kind -- it is a
    per-kind :class:`~typing.Literal` (relationships use
    ``active|inactive|superseded|retracted``; entities use
    ``live|superseded|retired|orphaned``) declared by each concrete subclass with
    its own default. The two status vocabularies are intentionally NOT unified.

    ``status`` is declared FIRST so the serialized JSON of every concrete
    lifecycle leads with ``status`` and is followed by the shared fields in a
    fixed order. ``RelationshipLifecycleState``'s serialized shape is pinned by
    the contract snapshot and KEV goldens, so this order MUST NOT change.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    # Concrete subclasses re-declare ``status`` with their per-kind Literal and
    # default. Declaring it here (first) fixes its position at the head of the
    # serialized field order for every subclass.
    status: StatusT
    reason: str | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None
    supersedes: dict[str, Any] | None = None
    superseded_by: dict[str, Any] | None = None

    @field_validator("effective_from", "effective_until", "closed_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer(
        "effective_from",
        "effective_until",
        "closed_at",
        when_used="json",
    )
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class RelationshipLifecycleState(LifecycleState[RelationshipLifecycleStatus]):
    """Lifecycle/actuality state for a relationship assertion.

    Narrows the shared :class:`LifecycleState` to the relationship status
    vocabulary. The serialized shape (``status`` first, then ``reason``,
    ``effective_from``, ``effective_until``, ``closed_at``, ``closed_by``,
    ``supersedes``, ``superseded_by``) is byte-identical to the pre-shared-base
    model and is pinned by the contract snapshot + KEV goldens.
    """

    status: RelationshipLifecycleStatus = "active"


class EntityLifecycleState(LifecycleState[EntityLifecycleStatus]):
    """Lifecycle/actuality state for an entity instance.

    Narrows the shared :class:`LifecycleState` to the entity status vocabulary.
    An entity is a referent, not an assertion: its existence is not "approved", so
    there is no review axis. ``status`` is distinct from any domain ``status``
    property (which models progress, e.g. planned/active/closed). The canonical
    soft-delete / retirement of an entity lives here as ``status != "live"``,
    gated out of live reads. The audit timestamp pair is the shared
    ``closed_at``/``closed_by`` (there is no entity-only ``retired_at``).
    """

    status: EntityLifecycleStatus = "live"


class RelationshipAssertion(BaseModel):
    """Coupled review and lifecycle state for a relationship."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    review: RelationshipReviewState = Field(default_factory=RelationshipReviewState)
    lifecycle: RelationshipLifecycleState = Field(default_factory=RelationshipLifecycleState)
    group_override: bool = False


def relationship_assertion_from_metadata(value: Any) -> RelationshipAssertion:
    """Load relationship assertion state from metadata-like input."""
    if value is None:
        return RelationshipAssertion()
    if isinstance(value, RelationshipAssertion):
        return value
    assertion = getattr(value, "assertion", None)
    if isinstance(assertion, RelationshipAssertion):
        return assertion
    if isinstance(value, dict):
        if not value:
            return RelationshipAssertion()
        if "assertion" in value:
            return RelationshipAssertion.model_validate(value.get("assertion") or {})
        if "review" in value or "lifecycle" in value:
            return RelationshipAssertion.model_validate(value)
    raise TypeError("relationship liveness requires a RelationshipAssertion or metadata")


def relationship_lifecycle_is_active(assertion_or_metadata: Any = None) -> bool:
    """Return whether relationship lifecycle permits current participation."""
    assertion = relationship_assertion_from_metadata(assertion_or_metadata)
    if assertion.lifecycle.status != "active":
        return False

    if not is_effective(
        effective_from=assertion.lifecycle.effective_from,
        effective_until=assertion.lifecycle.effective_until,
    ):
        return False
    return True


def relationship_is_live(
    assertion_or_metadata: Any = None,
    *,
    require_approved: bool = False,
) -> bool:
    """Return whether a relationship participates in live graph semantics."""
    assertion = relationship_assertion_from_metadata(assertion_or_metadata)
    if not relationship_lifecycle_is_active(assertion):
        return False

    if assertion.review.status in {"pending", "rejected"}:
        return False
    if require_approved and assertion.review.status != "approved":
        return False
    return True


# Entity metadata has no typed wrapper analogous to ``RelationshipMetadata``
# (it is a free-form ``dict``). The typed :class:`EntityLifecycleState` is stored
# under this reserved key and is the ONLY structured, validated slice of entity
# metadata. Read paths decode it via :func:`entity_lifecycle_from_metadata`; write
# paths construct + validate the typed model and embed it via
# :func:`entity_lifecycle_into_metadata` -- never by hand-authoring the dict.
ENTITY_LIFECYCLE_METADATA_KEY = "lifecycle"


def entity_lifecycle_from_metadata(value: Any) -> EntityLifecycleState:
    """Load entity lifecycle state from entity-metadata-like input.

    Entity metadata is a free-form ``dict``; the typed lifecycle state (if
    present) lives under :data:`ENTITY_LIFECYCLE_METADATA_KEY` and is always
    validated back into an :class:`EntityLifecycleState`. A missing/empty/partial
    shape decodes to the default ``live`` state so every read path treats
    undecorated entities as live without per-call dict spelunking.
    """
    if value is None:
        return EntityLifecycleState()
    if isinstance(value, EntityLifecycleState):
        return value
    if isinstance(value, dict):
        lifecycle = value.get(ENTITY_LIFECYCLE_METADATA_KEY)
        if lifecycle is None:
            return EntityLifecycleState()
        if isinstance(lifecycle, EntityLifecycleState):
            return lifecycle
        if isinstance(lifecycle, dict):
            return EntityLifecycleState.model_validate(lifecycle)
        raise TypeError("entity lifecycle metadata must be a mapping")
    raise TypeError("entity lifecycle requires an EntityLifecycleState or metadata dict")


def entity_lifecycle_into_metadata(
    lifecycle: EntityLifecycleState,
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Embed a validated :class:`EntityLifecycleState` into an entity-metadata dict.

    Returns a shallow copy of ``base`` (or a fresh dict) with the typed lifecycle
    serialized under :data:`ENTITY_LIFECYCLE_METADATA_KEY`. This is the single
    encode path for entity lifecycle: callers build the typed model (validating
    ``status`` against the entity ``Literal``) and hand it here, so storage always
    round-trips a validated lifecycle rather than a hand-authored blob.
    """
    metadata = dict(base or {})
    metadata[ENTITY_LIFECYCLE_METADATA_KEY] = lifecycle.model_dump(mode="json")
    return metadata


def build_entity_lifecycle_metadata(
    *,
    status: EntityLifecycleStatus,
    reason: str | None = None,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct + validate a typed entity lifecycle and embed it in metadata.

    Convenience wrapper over :func:`entity_lifecycle_into_metadata` for the common
    ``status`` (+ optional ``reason``) write. ``status`` is validated against the
    entity :class:`EntityLifecycleStatus` Literal by pydantic at construction.
    """
    lifecycle = EntityLifecycleState(status=status, reason=reason)
    return entity_lifecycle_into_metadata(lifecycle, base=base)


def entity_lifecycle_status(metadata: Any) -> EntityLifecycleStatus:
    """Return the typed lifecycle status for entity metadata.

    Single source of truth for entity-lifecycle inspection. Read surfaces must
    use this rather than reaching into ``metadata['lifecycle']['status']`` so a
    malformed/partial metadata shape is decoded consistently.
    """
    return entity_lifecycle_from_metadata(metadata).status


def entity_is_live(metadata: Any = None) -> bool:
    """Return whether an entity participates in live graph semantics.

    An entity is live when its lifecycle status is ``live`` and (if bounded) it is
    currently within its effective window.
    """
    lifecycle = entity_lifecycle_from_metadata(metadata)
    if lifecycle.status != "live":
        return False
    if not is_effective(
        effective_from=lifecycle.effective_from,
        effective_until=lifecycle.effective_until,
    ):
        return False
    return True


__all__ = [
    "ENTITY_LIFECYCLE_METADATA_KEY",
    "EntityLifecycleState",
    "EntityLifecycleStatus",
    "LifecycleState",
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipLifecycleStatus",
    "RelationshipReviewSource",
    "RelationshipReviewState",
    "RelationshipReviewStatus",
    "build_entity_lifecycle_metadata",
    "entity_is_live",
    "entity_lifecycle_from_metadata",
    "entity_lifecycle_into_metadata",
    "entity_lifecycle_status",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
]
