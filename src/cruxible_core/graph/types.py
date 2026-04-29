"""Runtime graph types for entity instances and relationship instances.

These are the runtime objects stored in the EntityGraph, distinct from
the schema types (PropertySchema, EntityTypeSchema, etc.) which define
the config structure.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


def make_node_id(entity_type: str, entity_id: str) -> str:
    """Build the canonical node ID for a (type, id) pair."""
    return f"{entity_type}:{entity_id}"


def split_node_id(node_id: str) -> tuple[str, str]:
    """Split a canonical node ID back into (entity_type, entity_id).

    Inverse of ``make_node_id``.  Handles entity IDs that contain colons.
    """
    entity_type, sep, entity_id = node_id.partition(":")
    if not sep:
        raise ValueError(f"Invalid node_id: {node_id!r}")
    return entity_type, entity_id


class EntityInstance(BaseModel):
    """A single entity instance in the graph."""

    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)

    def node_id(self) -> str:
        """Return the unique node ID for this entity."""
        return make_node_id(self.entity_type, self.entity_id)


class RelationshipInstance(BaseModel):
    """A single relationship instance in the graph.

    Also used as the target reference in feedback records. The
    ``relationship_type`` field accepts ``"relationship"`` during
    validation so that legacy feedback JSON round-trips correctly.
    """

    model_config = ConfigDict(populate_by_name=True)

    relationship_type: str = Field(
        validation_alias=AliasChoices("relationship_type", "relationship"),
    )
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    def from_node_id(self) -> str:
        """Return the source node ID."""
        return make_node_id(self.from_type, self.from_id)

    def to_node_id(self) -> str:
        """Return the target node ID."""
        return make_node_id(self.to_type, self.to_id)


REJECTED_STATUSES: frozenset[str] = frozenset({"human_rejected", "agent_rejected"})
"""Edge review_status values that indicate rejection."""

SYSTEM_OWNED_PROPERTIES: frozenset[str] = frozenset({"_provenance", "review_status"})
"""Graph property keys written by Cruxible system paths, not user/domain writes."""

USER_STRIPPED_PROPERTIES: frozenset[str] = SYSTEM_OWNED_PROPERTIES
"""System-owned keys stripped from user/domain write payloads."""


def make_provenance(source: str, source_ref: str) -> dict[str, str]:
    """Create a provenance metadata dict for edge creation."""
    return {
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_ref": source_ref,
    }
