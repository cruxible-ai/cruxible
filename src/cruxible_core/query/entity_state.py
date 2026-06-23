"""Entity visibility helpers for read paths.

The companion of :mod:`cruxible_core.query.relationship_state`. This module is
the single source of truth for deciding whether a stored entity is visible under
a given :data:`~cruxible_core.query.enums.QueryVisibilityState`. Every read path
that gates entity results by lifecycle routes through
:func:`entity_matches_query_state` so the answer is identical across the query
engine, the ``list entities`` service surface, and the MCP/HTTP read routes.

Entities are referents, not assertions: their existence is not "approved", so
they have NO review axis. They are gated purely on their lifecycle status. The
review-only visibility values (``accepted``/``pending``/``reviewable``) therefore
resolve to ``live`` for entities -- an entity that is live is "accepted" in the
only sense an entity can be.
"""

from __future__ import annotations

from typing import Any

from cruxible_core.graph.assertion_state import (
    EntityLifecycleStatus,
    entity_is_live,
    entity_lifecycle_status,
)
from cruxible_core.query.enums import REVIEW_ONLY_VISIBILITY_STATES, QueryVisibilityState


def entity_matches_query_state(
    metadata: Any,
    state: QueryVisibilityState,
) -> bool:
    """Return whether entity metadata matches the read-visibility state.

    * ``live`` (default) -- lifecycle status ``live`` and within its effective
      window.
    * ``all`` -- every stored entity, regardless of lifecycle.
    * ``not-live`` -- the complement of ``live``: retired/superseded/orphaned (or
      effective-window-expired) entities -- the set gated out of live reads.
    * ``accepted`` / ``pending`` / ``reviewable`` -- entities have no review axis,
      so these resolve to ``live``.
    """
    if state == "all":
        return True
    if state == "not-live":
        return not entity_is_live(metadata)
    # `live` plus all review-only refinements collapse to live for entities.
    if state == "live" or state in REVIEW_ONLY_VISIBILITY_STATES:
        return entity_is_live(metadata)
    raise ValueError(f"Unsupported query visibility state '{state}'")


def entity_visibility_status(metadata: Any) -> EntityLifecycleStatus:
    """Return the typed lifecycle status for entity metadata."""
    return entity_lifecycle_status(metadata)


def resolve_entity_visibility_state(state: QueryVisibilityState) -> QueryVisibilityState:
    """Collapse review-only selectors to ``live`` for the entity axis.

    Entities have no review axis, so ``accepted`` / ``pending`` / ``reviewable``
    all mean "live" for an entity. Callers that drive a real entity query through
    the engine (where path-shape constraints attach to the review-only values)
    should normalize the selector first so an entity surface never inherits a
    relationship-review-only constraint.
    """
    if state in REVIEW_ONLY_VISIBILITY_STATES:
        return "live"
    return state


__all__ = [
    "entity_matches_query_state",
    "entity_visibility_status",
    "resolve_entity_visibility_state",
]
