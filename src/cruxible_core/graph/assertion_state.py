"""Typed helpers for relationship assertion state.

Provenance explains where an edge came from. Assertion state explains how
Cruxible should treat the edge in live graph semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import ensure_utc, format_datetime, is_effective

RelationshipReviewStatus = Literal[
    "unreviewed",
    "pending",
    "approved",
    "rejected",
]

RelationshipReviewSource = Literal["system", "human", "agent", "group", "unknown"]
"""Who moved an edge's review state.

``unknown`` is the honest value for a review transition whose actor context did
not resolve. It exists because the alternative — the retired caller-declared
``source`` axis — defaulted such writes to ``human``, which is a claim the
instance had no evidence for.
"""

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

TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES: frozenset[str] = frozenset({"retracted", "superseded"})
"""Relationship statuses a free-form write may not set.

Terminating a claim is a governed judgement about its standing, not a property
edit. These live next to the status vocabularies (rather than in the service
layer) because the graph write chokepoint — the seam every free write shares —
must be able to consult them without importing ``service``.
"""

TERMINAL_ENTITY_LIFECYCLE_STATUSES: frozenset[str] = frozenset({"retired", "superseded"})
"""Entity statuses a free-form write may not set."""

WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES = "active, inactive"
"""Human-readable list of relationship statuses that stay freely writable."""

WRITABLE_ENTITY_LIFECYCLE_STATUSES = "live"
"""Human-readable list of entity statuses that stay freely writable."""

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


class SupersessionPointer(BaseModel):
    """Open, typed reference to the other side of a supersession link.

    Two shapes are named, one per subject kind:

    * an EDGE (claim) is referenced by its minted ``claim_id`` -- the whole
      point of edge identity is that a supersession pointer can name one
      specific claim rather than a tuple that may have several live edges;
    * an ENTITY is referenced by ``entity_type`` + ``entity_id``, its natural
      key (entities already have stable identity).

    The model is deliberately OPEN (``extra="allow"``) so a later pointer kind
    can be introduced without a migration of stored lifecycle payloads, and so
    already-persisted free-form pointers keep validating. What it refuses is the
    three shapes that can only be mistakes: an empty pointer, a half entity
    pair, and a pointer that names BOTH a claim and an entity.

    THE REFUSAL IS A WRITE-PATH REFUSAL. Constructing or validating a pointer
    directly -- what a lifecycle verb does when it writes one -- raises on those
    shapes. Reaching this model through a stored ``LifecycleState`` does NOT:
    that path runs on EVERY graph load, so a single stray persisted value would
    make the whole graph unloadable, turning a bad pointer into a bricked
    instance. ``LifecycleState`` therefore coerces an incoherent stored pointer
    to ``None`` (see ``_tolerate_incoherent_stored_pointer``): the damage stays
    the size of the one field.

    The dedicated receipted relationship/entity supersede verbs are the only
    writers of these pointers.
    """

    model_config = ConfigDict(extra="allow")

    claim_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None

    @model_validator(mode="after")
    def _validate_pointer_shape(self) -> SupersessionPointer:
        has_entity_type = bool(self.entity_type)
        has_entity_id = bool(self.entity_id)
        if has_entity_type != has_entity_id:
            raise ValueError(
                "supersession pointer entity reference requires both entity_type and entity_id"
            )
        if self.claim_id and has_entity_type:
            raise ValueError(
                "supersession pointer names either a claim_id or an entity, never both"
            )
        if not self.claim_id and not has_entity_type and not (self.model_extra or {}):
            raise ValueError(
                "supersession pointer must name a claim_id or an entity_type/entity_id pair"
            )
        return self


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
    supersedes: SupersessionPointer | None = None
    superseded_by: SupersessionPointer | None = None

    @field_validator("supersedes", "superseded_by", mode="before")
    @classmethod
    def _tolerate_incoherent_stored_pointer(cls, value: Any) -> Any:
        """Coerce an unusable STORED pointer to None instead of failing the load.

        This validator runs on every graph load, because lifecycle state is
        decoded out of persisted relationship/entity metadata. If it propagated
        ``SupersessionPointer``'s refusals, one stray persisted ``{}`` -- from a
        hand-edited image, a future writer's bug, a partially-written payload --
        would make the ENTIRE graph unloadable, with no read path left to
        diagnose or repair it from. A pointer that names nothing resolvable is
        worth exactly nothing, so dropping it costs nothing and keeps the blast
        radius at one field. The write path (constructing or validating a
        ``SupersessionPointer`` directly) still refuses those shapes loudly.
        """
        if value is None or isinstance(value, SupersessionPointer):
            return value
        if not isinstance(value, Mapping):
            return None
        try:
            return SupersessionPointer.model_validate(dict(value))
        except ValidationError:
            return None

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


class GroupApprovalDrift(BaseModel):
    """Divergence between the content a group approved and the edge's content now.

    A group approval accepts an edge's PROPERTIES, not merely its existence. For
    an ordinary (``direct``) relationship type a later direct write that changes
    those properties is legitimate — facts about the world change — but the
    divergence must not vanish silently, or a reviewer reading the edge still
    believes the group signed off on what it now says.

    ``approved_values`` holds the values as of the approval, and is carried
    forward across successive drifts on the same group: a second drift must not
    overwrite it with the first drift's values, or the record degrades into
    "what it said last time" instead of "what was approved".
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    group_id: str
    changed_properties: list[str] = Field(default_factory=list)
    approved_values: dict[str, Any] = Field(default_factory=dict)
    first_detected_at: datetime | None = None
    detected_at: datetime | None = None
    receipt_id: str | None = None
    actor_context: GovernedActorContext | None = None

    @field_validator("first_detected_at", "detected_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer("first_detected_at", "detected_at", when_used="json")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


class RelationshipAssertion(BaseModel):
    """Coupled review and lifecycle state for a relationship."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    review: RelationshipReviewState = Field(default_factory=RelationshipReviewState)
    lifecycle: RelationshipLifecycleState = Field(default_factory=RelationshipLifecycleState)
    group_override: bool = False
    # Deliberately NOT on the review or lifecycle axis: a legitimately-changed
    # fact must stay live and stay approved. Drift is a trust annotation that
    # rides alongside ``group_override`` (the other group-provenance flag) and
    # is returned by every ordinary relationship read via ``metadata.assertion``.
    # ``None`` is dropped by the ``exclude_none=True`` metadata encoder, so
    # un-drifted edges serialize exactly as before.
    group_approval_drift: GroupApprovalDrift | None = None


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


def relationship_is_live(assertion_or_metadata: Any = None) -> bool:
    """Return whether a relationship participates in live graph semantics."""
    assertion = relationship_assertion_from_metadata(assertion_or_metadata)
    if not relationship_lifecycle_is_active(assertion):
        return False

    return assertion.review.status not in {"pending", "rejected"}


__all__ = [
    "TERMINAL_ENTITY_LIFECYCLE_STATUSES",
    "TERMINAL_RELATIONSHIP_LIFECYCLE_STATUSES",
    "WRITABLE_ENTITY_LIFECYCLE_STATUSES",
    "WRITABLE_RELATIONSHIP_LIFECYCLE_STATUSES",
    "EntityLifecycleState",
    "EntityLifecycleStatus",
    "GroupApprovalDrift",
    "LifecycleState",
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipLifecycleStatus",
    "RelationshipReviewSource",
    "RelationshipReviewState",
    "RelationshipReviewStatus",
    "SupersessionPointer",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
]
