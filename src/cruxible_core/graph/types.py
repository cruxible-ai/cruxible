"""Runtime graph types for entity instances and relationship instances.

These are the runtime objects stored in the EntityGraph, distinct from
the schema types (PropertySchema, EntityTypeSchema, etc.) which define
the config structure.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipLifecycleState,
    RelationshipReviewState,
    dump_assertion,
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    make_provenance,
    provenance_group_id,
    stamp_provenance_modified,
)


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
    metadata: dict[str, Any] = Field(default_factory=dict)

    def node_id(self) -> str:
        """Return the unique node ID for this entity."""
        return make_node_id(self.entity_type, self.entity_id)


class RelationshipMetadata(BaseModel):
    """Cruxible-owned metadata for a relationship instance."""

    provenance: RelationshipProvenance | None = None
    assertion: RelationshipAssertion = Field(default_factory=RelationshipAssertion)


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
    metadata: RelationshipMetadata = Field(default_factory=RelationshipMetadata)

    def from_node_id(self) -> str:
        """Return the source node ID."""
        return make_node_id(self.from_type, self.from_id)

    def to_node_id(self) -> str:
        """Return the target node ID."""
        return make_node_id(self.to_type, self.to_id)


__all__ = [
    "EntityInstance",
    "RelationshipInstance",
    "RelationshipAssertion",
    "RelationshipLifecycleState",
    "RelationshipMetadata",
    "RelationshipProvenance",
    "RelationshipReviewState",
    "dump_assertion",
    "dump_provenance",
    "load_provenance",
    "make_node_id",
    "make_provenance",
    "provenance_group_id",
    "relationship_assertion_from_metadata",
    "relationship_is_live",
    "relationship_lifecycle_is_active",
    "split_node_id",
    "stamp_provenance_modified",
]
