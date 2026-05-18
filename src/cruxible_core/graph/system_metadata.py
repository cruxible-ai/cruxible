"""In-code convenience model for sibling relationship system metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from cruxible_core.graph.assertion_state import (
    RelationshipAssertionState,
    load_assertion_state,
)
from cruxible_core.graph.provenance import (
    PROVENANCE_PROPERTY,
    RelationshipProvenance,
    load_provenance,
)


class RelationshipSystemMetadata(BaseModel):
    """Typed view over sibling system-owned relationship properties."""

    provenance: RelationshipProvenance | None = None
    assertion: RelationshipAssertionState = Field(default_factory=RelationshipAssertionState)


def load_relationship_system_metadata(
    properties: Mapping[str, Any] | None,
) -> RelationshipSystemMetadata:
    """Load typed relationship system metadata without changing storage shape."""
    props = properties or {}
    return RelationshipSystemMetadata(
        provenance=load_provenance(props.get(PROVENANCE_PROPERTY)),
        assertion=load_assertion_state(props),
    )


__all__ = [
    "RelationshipSystemMetadata",
    "load_relationship_system_metadata",
]
