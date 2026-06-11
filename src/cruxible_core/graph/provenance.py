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

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.temporal import ensure_utc, format_datetime, utc_now


class RelationshipProvenance(BaseModel):
    """System-owned provenance for a relationship."""

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    source: str | None = None
    source_ref: str | None = None
    created_at: datetime | None = None
    last_modified_at: datetime | None = None
    last_modified_by: str | None = None
    # Write-time correlation, stamped at creation and never rewritten:
    # edges created before these fields existed keep them null.
    receipt_id: str | None = None
    resolution_id: str | None = None
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


def provenance_group_id(provenance: RelationshipProvenance) -> str | None:
    """Extract the candidate group id from group-backed provenance."""
    source_ref = provenance.source_ref
    if source_ref is None or not source_ref.startswith("group:"):
        return None
    return source_ref.removeprefix("group:")


__all__ = [
    "CANONICAL_SOURCE_REFS",
    "SOURCE_REF_ADD_RELATIONSHIP",
    "SOURCE_REF_BATCH_DIRECT_WRITE",
    "RelationshipProvenance",
    "dump_provenance",
    "load_provenance",
    "make_provenance",
    "provenance_group_id",
    "stamp_provenance_modified",
]
