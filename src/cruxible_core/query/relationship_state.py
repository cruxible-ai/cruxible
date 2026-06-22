"""Relationship visibility helpers for query traversal.

This module is the single source of truth for deciding whether a stored edge is
visible under a given relationship/review state. Every read path that filters
edges by review/relationship state must route through these helpers (rather than
re-deriving the decision from raw metadata dicts) so the answer is identical
across the query engine, the ``list edges`` service surface, edge export, and
graph-quality checks.
"""

from __future__ import annotations

from typing import Any

from cruxible_core.graph.assertion_state import (
    RelationshipReviewStatus,
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


def relationship_review_status(metadata: Any) -> RelationshipReviewStatus:
    """Return the typed review status for relationship metadata.

    Single source of truth for review-status inspection. Read surfaces must use
    this (rather than reaching into raw ``metadata['assertion']['review']``
    dict chains) so a malformed/partial metadata shape is decoded consistently.
    """
    return relationship_assertion_from_metadata(metadata).review.status


def relationship_review_is_rejected(metadata: Any) -> bool:
    """Return whether a relationship's assertion review status is ``rejected``."""
    return relationship_review_status(metadata) == "rejected"


__all__ = [
    "relationship_matches_query_state",
    "relationship_review_is_rejected",
    "relationship_review_status",
]
