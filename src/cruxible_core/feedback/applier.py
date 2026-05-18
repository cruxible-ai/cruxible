"""Apply feedback to the entity graph."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.feedback.types import FeedbackRecord
from cruxible_core.graph.assertion_state import (
    ASSERTION_PROPERTY,
    RelationshipAssertionState,
    RelationshipReviewSource,
    RelationshipReviewStatus,
    dump_assertion_state,
    load_assertion_state,
    review_state_to_legacy_review_status,
)
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    stamp_provenance_modified,
)
from cruxible_core.graph.types import USER_STRIPPED_PROPERTIES

if TYPE_CHECKING:
    from cruxible_core.graph.entity_graph import EntityGraph


def _read_relationship(
    graph: EntityGraph,
    t: Any,
    relationship: str,
    edge_key: int | None,
) -> Any | None:
    """Read an edge when it exists."""
    return graph.get_relationship(
        t.from_type,
        t.from_id,
        t.to_type,
        t.to_id,
        relationship,
        edge_key=edge_key,
    )


def _read_provenance(
    graph: EntityGraph,
    t: Any,
    relationship: str,
    edge_key: int | None,
) -> RelationshipProvenance | None:
    """Read existing _provenance from an edge when it is usable."""
    existing = _read_relationship(graph, t, relationship, edge_key)
    if existing:
        old_prov = existing.properties.get("_provenance")
        return load_provenance(old_prov)
    return None


def _stamp_provenance(prov: RelationshipProvenance, action: str) -> dict[str, Any]:
    """Return JSON-ready provenance stamped for a feedback action."""
    return dump_provenance(stamp_provenance_modified(prov, f"feedback:{action}"))


_SOURCE_PREFIX: dict[str, RelationshipReviewSource] = {
    "human": "human",
    "agent": "agent",
}

_ACTION_PAST: dict[str, RelationshipReviewStatus] = {
    "approve": "approved",
    "reject": "rejected",
}


def _review_updates(
    graph: EntityGraph,
    t: Any,
    relationship: str,
    edge_key: int | None,
    *,
    status: RelationshipReviewStatus,
    source: RelationshipReviewSource,
    actor: str,
) -> dict[str, Any]:
    existing = _read_relationship(graph, t, relationship, edge_key)
    current_state = (
        load_assertion_state(existing.properties)
        if existing is not None
        else RelationshipAssertionState()
    )
    review = current_state.review.model_copy(
        update={
            "status": status,
            "source": source,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": actor,
        }
    )
    state = current_state.model_copy(update={"review": review})
    updates: dict[str, Any] = {ASSERTION_PROPERTY: dump_assertion_state(state)}
    legacy_review_status = review_state_to_legacy_review_status(review)
    if legacy_review_status is not None:
        updates["review_status"] = legacy_review_status
    return updates


def apply_feedback(graph: EntityGraph, feedback: FeedbackRecord) -> bool:
    """Apply a feedback record to the graph. Returns True if the edge was found.

    Review state is determined by (source, action) and written through the
    typed assertion protocol while preserving legacy review_status.
    """
    t = feedback.target
    edge_key = t.edge_key

    if edge_key is None:
        match_count = graph.relationship_count_between(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
        )
        if match_count > 1:
            raise RelationshipAmbiguityError(
                from_type=t.from_type,
                from_id=t.from_id,
                to_type=t.to_type,
                to_id=t.to_id,
                relationship_type=t.relationship_type,
            )

    prefix = _SOURCE_PREFIX[feedback.source]
    actor = f"feedback:{feedback.action}"

    if feedback.action in _ACTION_PAST:
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        updates = _review_updates(
            graph,
            t,
            t.relationship_type,
            edge_key,
            status=_ACTION_PAST[feedback.action],
            source=prefix,
            actor=actor,
        )
        if prov:
            updates["_provenance"] = _stamp_provenance(prov, feedback.action)
        return graph.update_edge_properties(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            updates=updates,
            edge_key=edge_key,
        )

    if feedback.action == "flag":
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        updates = _review_updates(
            graph,
            t,
            t.relationship_type,
            edge_key,
            status="pending",
            source=prefix,
            actor=actor,
        )
        if prov:
            updates["_provenance"] = _stamp_provenance(prov, feedback.action)
        return graph.update_edge_properties(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            updates=updates,
            edge_key=edge_key,
        )

    if feedback.action == "correct":
        # Strip system-owned properties from corrections (prevent spoofing).
        updates = {
            k: v for k, v in feedback.corrections.items() if k not in USER_STRIPPED_PROPERTIES
        }
        updates.update(
            _review_updates(
                graph,
                t,
                t.relationship_type,
                edge_key,
                status="approved",
                source=prefix,
                actor=actor,
            )
        )
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        if prov:
            updates["_provenance"] = _stamp_provenance(prov, feedback.action)
        return graph.update_edge_properties(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            updates=updates,
            edge_key=edge_key,
        )

    return False
