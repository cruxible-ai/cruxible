"""Typed helpers for system-owned relationship provenance."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_serializer,
    field_validator,
)

from cruxible_client.contracts.temporal import ensure_utc, format_datetime, utc_now
from cruxible_core.playbill.actor_context import GovernedActorContext


class RelationshipProvenance(BaseModel):
    """System-owned provenance for a relationship."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    source: str | None = None
    source_ref: str | None = None
    created_at: datetime | None = None
    last_modified_at: datetime | None = None
    last_modified_by: str | None = None
    # Write-time correlation: the id of the receipt/resolution that authored this
    # edge in THIS instance's receipt store. Stamped at creation and never
    # rewritten on the live-write path. Two cases produce a null receipt_id, and
    # both are accepted, intended, immutable history -- NOT a bug to backfill:
    #   1. Legacy edges (created before per-edge receipts existed, pre-2026-06-11)
    #      carry null receipt_id because no receipt was ever written for them.
    #   2. Clone/snapshot/state-pull edges have their receipt_id cleared on
    #      materialization (the receipt lives only in the source instance and is
    #      not shipped in the graph+config+lock bundle), with `clone_origin`
    #      stamped so the edge is honestly labeled as clone-origin rather than
    #      pointing at a phantom receipt. See ``relabel_provenance_for_clone``.
    # The invariant: a non-null receipt_id ALWAYS resolves to a receipt present
    # in this instance's store; a null receipt_id is accepted history.
    receipt_id: str | None = None
    resolution_id: str | None = None
    # Set when this edge was materialized from a snapshot/clone/state-pull bundle
    # (which carries no receipts), recording the origin and the now-dangling
    # source receipt_id that was cleared. Null on natively-written edges.
    clone_origin: str | None = None
    # Set only when an edge that carried NO provenance was touched by an
    # update/feedback write. ``source``/``source_ref`` then read
    # ``unknown_backfilled`` rather than the toucher's channel, and these two
    # fields record what is actually known: which channel performed the backfill
    # and when. Null on every edge whose origin is genuinely known.
    touched_by: str | None = None
    backfilled_at: str | None = None
    created_actor_context: GovernedActorContext | None = None
    last_modified_actor_context: GovernedActorContext | None = None

    @field_validator("created_at", "last_modified_at")
    @classmethod
    def _normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        return ensure_utc(value) if value is not None else None

    @field_serializer("created_at", "last_modified_at", when_used="json")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        return format_datetime(value)


# Canonical source_ref operation vocabulary: snake_case operation names,
# never surface spellings (source carries the channel). Frozen at 0.2.
SOURCE_REF_ADD_RELATIONSHIP = "add_relationship"
SOURCE_REF_BATCH_DIRECT_WRITE = "batch_direct_write"
CANONICAL_SOURCE_REFS = frozenset({SOURCE_REF_ADD_RELATIONSHIP, SOURCE_REF_BATCH_DIRECT_WRITE})

# Clone-origin marker stamped on edges materialized from a snapshot/clone or a
# state-pull bundle. The bundle is graph+config+lock with NO receipts, so any
# receipt_id those edges carried points at a receipt in the SOURCE instance that
# is absent here. We clear the dangling receipt_id and record this origin.
CLONE_ORIGIN_UPSTREAM_SNAPSHOT = "upstream-snapshot"

# Backfill marker for an edge that was touched while carrying NO provenance.
# The touch cannot know where the edge came from, so the origin is recorded as
# unknown-and-backfilled rather than borrowing the toucher's channel. The
# touching channel is preserved separately (``touched_by``) so the backfill is
# attributable without being mistaken for authorship.
SOURCE_UNKNOWN_BACKFILLED = "unknown_backfilled"
SOURCE_REF_UNKNOWN_BACKFILLED = "unknown_backfilled"


def make_provenance(
    source: str,
    source_ref: str,
    *,
    receipt_id: str | None = None,
    resolution_id: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> RelationshipProvenance:
    """Create complete provenance for a newly written relationship."""
    return RelationshipProvenance(
        source=source,
        source_ref=source_ref,
        created_at=utc_now(),
        receipt_id=receipt_id,
        resolution_id=resolution_id,
        created_actor_context=actor_context,
    )


def load_provenance(value: Any) -> RelationshipProvenance | None:
    """Parse stored relationship provenance, returning None when unusable."""
    if isinstance(value, RelationshipProvenance):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return RelationshipProvenance.model_validate(value)
    except ValidationError:
        return None


def dump_provenance(provenance: RelationshipProvenance) -> dict[str, Any]:
    """Return the JSON-ready relationship provenance shape."""
    return provenance.model_dump(mode="json", exclude_none=True)


def stamp_provenance_modified(
    provenance: RelationshipProvenance,
    actor: str,
    *,
    actor_context: GovernedActorContext | None = None,
) -> RelationshipProvenance:
    """Return provenance with modification actor and timestamp updated."""
    return provenance.model_copy(
        update={
            "last_modified_at": utc_now(),
            "last_modified_by": actor,
            "last_modified_actor_context": actor_context,
        }
    )


def backfill_provenance_on_touch(
    provenance: RelationshipProvenance | None,
    actor: str,
    *,
    actor_context: GovernedActorContext | None = None,
) -> RelationshipProvenance:
    """Stamp provenance for an update/feedback touch, backfilling when it is null.

    Edges written before provenance was tracked (or written without it) carry a null
    provenance that update/feedback paths historically left null forever. When an edge
    is touched we either stamp the existing provenance's modification fields, or — if it
    has none — backfill a marker provenance so the touch makes the edge auditable.

    The backfilled ``source`` is deliberately :data:`SOURCE_UNKNOWN_BACKFILLED`, NOT
    the touching channel. The toucher did not create this edge; stamping its own
    channel as the origin asserts a provenance the edge never had, and turns "we do
    not know where this came from" into a confident (and false) claim that it came
    from whoever last happened to write to it. What we DO know is recorded: which
    channel performed the backfill (``touched_by``/``touched_by_ref``), when, and
    under which actor context.
    """
    if provenance is not None:
        return stamp_provenance_modified(provenance, actor, actor_context=actor_context)
    now = utc_now()
    return RelationshipProvenance(
        source=SOURCE_UNKNOWN_BACKFILLED,
        source_ref=SOURCE_REF_UNKNOWN_BACKFILLED,
        last_modified_at=now,
        last_modified_by=actor,
        last_modified_actor_context=actor_context,
        touched_by=actor,
        backfilled_at=format_datetime(now),
    )


def relabel_provenance_for_clone(
    provenance: RelationshipProvenance | None,
    *,
    origin: str = CLONE_ORIGIN_UPSTREAM_SNAPSHOT,
) -> RelationshipProvenance | None:
    """Clear a clone's dangling receipt correlation and stamp the clone origin.

    A snapshot/clone/state-pull bundle is graph+config+lock with no receipts, so
    any ``receipt_id``/``resolution_id`` carried by a cloned edge points at an
    artifact that exists only in the source instance -- a dangling pointer. On
    materialization we null those correlation ids and record ``clone_origin`` (and
    the source receipt_id that was cleared, for traceability) so the edge is
    honestly labeled as clone-origin. The authoring ``source``/``source_ref`` and
    timestamps are preserved as real history.

    Returns the provenance unchanged when it is already clean (null receipt_id and
    null resolution_id), so re-materializing clone-origin or legacy edges is a
    no-op and we never fabricate provenance for edges that never had a receipt.
    """
    if provenance is None:
        return None
    if provenance.receipt_id is None and provenance.resolution_id is None:
        return provenance
    update: dict[str, Any] = {
        "receipt_id": None,
        "resolution_id": None,
        "clone_origin": origin,
    }
    if provenance.receipt_id is not None:
        # Preserve the dangling source receipt_id for traceability rather than
        # discarding it: the edge honestly records where it came from.
        update["cloned_receipt_id"] = provenance.receipt_id
    return provenance.model_copy(update=update)


def provenance_group_id(provenance: RelationshipProvenance) -> str | None:
    """Extract the candidate group id from group-backed provenance."""
    source_ref = provenance.source_ref
    if source_ref is None or not source_ref.startswith("group:"):
        return None
    return source_ref.removeprefix("group:")


__all__ = [
    "CANONICAL_SOURCE_REFS",
    "CLONE_ORIGIN_UPSTREAM_SNAPSHOT",
    "SOURCE_REF_ADD_RELATIONSHIP",
    "SOURCE_REF_BATCH_DIRECT_WRITE",
    "SOURCE_REF_UNKNOWN_BACKFILLED",
    "SOURCE_UNKNOWN_BACKFILLED",
    "RelationshipProvenance",
    "backfill_provenance_on_touch",
    "dump_provenance",
    "load_provenance",
    "make_provenance",
    "provenance_group_id",
    "relabel_provenance_for_clone",
    "stamp_provenance_modified",
]
