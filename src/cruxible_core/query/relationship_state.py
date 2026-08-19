"""Relationship visibility helpers, retained as config-schema donor residue.

This module was the single source of truth for deciding whether a stored edge
is visible under a given read-visibility state, so the query engine, the
``list edges`` service surface, edge export, and graph-quality checks could not
disagree. PC-F deleted all of those readers, along with the companion
``query.entity_state`` module that gated entities by lifecycle under the same
:data:`QueryVisibilityState` value space.

What keeps this module alive is narrower: ``query.predicates`` imports it, and
``config/schema.py`` reaches ``query.predicates`` when it validates a named
query's predicate scopes. It leaves with the config donor in PC-H.
"""

from __future__ import annotations

from typing import Any

from cruxible_core.graph.assertion_state import (
    RelationshipReviewStatus,
    relationship_assertion_from_metadata,
    relationship_is_live,
    relationship_lifecycle_is_active,
)
from cruxible_core.query.enums import QueryVisibilityState


def relationship_matches_query_state(
    metadata: Any,
    state: QueryVisibilityState,
) -> bool:
    """Return whether relationship metadata matches the read-visibility state.

    Relationships are gated on BOTH the review axis and the lifecycle axis:

    * ``live`` -- lifecycle-active AND not pending/rejected (the default).
    * ``accepted`` -- lifecycle-active AND review-approved.
    * ``all`` -- every stored edge, regardless of review or lifecycle.
    * ``not-live`` -- the complement of ``live``: edges hidden from live reads
      because review rejected them OR lifecycle closed/retracted/superseded them.
    * ``pending`` -- lifecycle-active AND review-pending.
    * ``reviewable`` -- lifecycle-active AND (live OR review-pending).
    """
    if state == "all":
        return True
    if state == "live":
        return relationship_is_live(metadata)
    if state == "not-live":
        return not relationship_is_live(metadata)

    assertion = relationship_assertion_from_metadata(metadata)
    if not relationship_lifecycle_is_active(assertion):
        return False
    if state == "accepted":
        return assertion.review.status == "approved"
    if state == "pending":
        return assertion.review.status == "pending"
    if state == "reviewable":
        return relationship_is_live(assertion) or assertion.review.status == "pending"
    raise ValueError(f"Unsupported query visibility state '{state}'")


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
