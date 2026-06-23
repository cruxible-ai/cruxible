"""Lightweight query enum contracts."""

from __future__ import annotations

from typing import Literal

QueryResultShape = Literal["entity", "path", "relationship"]
QueryDedupe = Literal["entity", "path", "none"]

# Unified read-visibility selector. Applies uniformly to entities (gated by
# lifecycle) and relationships (gated by review AND lifecycle):
#   live       -- default; only live entities / live (active + reviewed-in) edges
#   accepted   -- review-approved edges (entities: resolves to live, no review axis)
#   all        -- everything, regardless of lifecycle or review state
#   not-live   -- exactly the set gated out of `live`: retired/superseded/orphaned
#                 entities + rejected (review) or closed/retracted (lifecycle) edges
#   pending    -- pending-review edges (relationship-review refinement)
#   reviewable -- live-or-pending edges (relationship-review refinement)
QueryVisibilityState = Literal[
    "live",
    "accepted",
    "all",
    "not-live",
    "pending",
    "reviewable",
]

# Values that refine the relationship REVIEW axis only. For entities (which have
# no review axis) these resolve to `live`.
REVIEW_ONLY_VISIBILITY_STATES: frozenset[str] = frozenset({"accepted", "pending", "reviewable"})

__all__ = [
    "REVIEW_ONLY_VISIBILITY_STATES",
    "QueryDedupe",
    "QueryResultShape",
    "QueryVisibilityState",
]
