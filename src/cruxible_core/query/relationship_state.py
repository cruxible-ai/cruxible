"""Relationship visibility helpers for query traversal."""

from __future__ import annotations

from typing import Any

from cruxible_core.graph.assertion_state import (
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.query.enums import QueryRelationshipState


def relationship_matches_query_state(
    metadata: Any,
    state: QueryRelationshipState,
) -> bool:
    """Return whether relationship metadata matches query visibility state."""
    if state == "live":
        return relationship_is_live(metadata)

    assertion = relationship_assertion_from_metadata(metadata)
    if not relationship_lifecycle_is_active(assertion):
        return False
    if state == "accepted":
        return assertion.review.status == "approved"
    if state == "pending":
        return assertion.review.status == "pending"
    if state == "reviewable":
        return relationship_is_live(assertion) or assertion.review.status == "pending"
    raise ValueError(f"Unsupported query relationship state '{state}'")


__all__ = ["relationship_matches_query_state"]
