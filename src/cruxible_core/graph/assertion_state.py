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
    ``live|superseded|retired``) declared by each concrete subclass with
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

    This state is carried by the typed :class:`~cruxible_core.graph.types.EntityMetadata`
    envelope (``EntityMetadata.lifecycle``), mirroring how
    :class:`RelationshipLifecycleState` rides inside ``RelationshipMetadata``. There
    is no free-form ``metadata['lifecycle']`` reserved-key convention -- entity
    lifecycle is a typed field, encoded/decoded only at the metadata boundary.

    ``orphaned`` is intentionally NOT a value here: an orphaned entity is a DERIVED
    evaluate/health finding (surfaced as ``integrity.orphan_entity_count``), not an
    authorable lifecycle state, so it is absent from the vocabulary.
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


__all__ = [
    "EntityLifecycleState",
    "EntityLifecycleStatus",
    "LifecycleState",
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipLifecycleStatus",
    "RelationshipReviewSource",
    "RelationshipReviewState",
    "RelationshipReviewStatus",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
]
