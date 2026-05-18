"""Runtime graph types for entity instances and relationship instances.

These are the runtime objects stored in the EntityGraph, distinct from
the schema types (PropertySchema, EntityTypeSchema, etc.) which define
the config structure.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from cruxible_core.graph.assertion_state import (
    ASSERTION_PROPERTY,
    LEGACY_REVIEW_STATUS_PROPERTY,
    RelationshipAssertionState,
    RelationshipLifecycleState,
    RelationshipReviewState,
    dump_assertion_state,
    load_assertion_state,
    relationship_is_live,
    review_state_to_legacy_review_status,
)
from cruxible_core.graph.provenance import (
    PROVENANCE_PROPERTY,
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    make_provenance,
    provenance_group_id,
    stamp_provenance_modified,
)
from cruxible_core.graph.system_metadata import (
    RelationshipSystemMetadata,
    load_relationship_system_metadata,
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

SYSTEM_OWNED_PROPERTIES: frozenset[str] = frozenset(
    {PROVENANCE_PROPERTY, ASSERTION_PROPERTY, LEGACY_REVIEW_STATUS_PROPERTY}
)
"""Graph property keys written by Cruxible system paths, not user/domain writes."""

USER_STRIPPED_PROPERTIES: frozenset[str] = SYSTEM_OWNED_PROPERTIES
"""System-owned keys stripped from user/domain write payloads."""


__all__ = [
    "ASSERTION_PROPERTY",
    "EntityInstance",
    "LEGACY_REVIEW_STATUS_PROPERTY",
    "PROVENANCE_PROPERTY",
    "RelationshipInstance",
    "RelationshipAssertionState",
    "RelationshipLifecycleState",
    "RelationshipProvenance",
    "RelationshipReviewState",
    "RelationshipSystemMetadata",
    "REJECTED_STATUSES",
    "SYSTEM_OWNED_PROPERTIES",
    "USER_STRIPPED_PROPERTIES",
    "dump_assertion_state",
    "dump_provenance",
    "load_assertion_state",
    "load_relationship_system_metadata",
    "load_provenance",
    "make_node_id",
    "make_provenance",
    "provenance_group_id",
    "relationship_is_live",
    "review_state_to_legacy_review_status",
    "split_node_id",
    "stamp_provenance_modified",
]
