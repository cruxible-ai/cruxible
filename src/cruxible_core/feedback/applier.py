"""Apply feedback to the entity graph.

Actions:
- approve: set review_status based on source (human_approved or agent_approved)
- reject: set review_status based on source (human_rejected or agent_rejected)
- correct: merge corrections into edge properties, set approved status
- flag: set review_status to pending_review
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.feedback.types import FeedbackRecord
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    dump_provenance,
    load_provenance,
    stamp_provenance_modified,
)
from cruxible_core.graph.types import USER_STRIPPED_PROPERTIES

if TYPE_CHECKING:
    from cruxible_core.graph.entity_graph import EntityGraph


def _read_provenance(
    graph: EntityGraph,
    t: Any,
    relationship: str,
    edge_key: int | None,
) -> RelationshipProvenance | None:
    """Read existing _provenance from an edge when it is usable."""
    existing = graph.get_relationship(
        t.from_type,
        t.from_id,
        t.to_type,
        t.to_id,
        relationship,
        edge_key=edge_key,
    )
    if existing:
        old_prov = existing.properties.get("_provenance")
        return load_provenance(old_prov)
    return None


def _stamp_provenance(prov: RelationshipProvenance, action: str) -> dict[str, Any]:
    """Return JSON-ready provenance stamped for a feedback action."""
    return dump_provenance(stamp_provenance_modified(prov, f"feedback:{action}"))


_SOURCE_PREFIX = {
    "human": "human",
    "agent": "agent",
}

_ACTION_PAST = {"approve": "approved", "reject": "rejected"}


def apply_feedback(graph: EntityGraph, feedback: FeedbackRecord) -> bool:
    """Apply a feedback record to the graph. Returns True if the edge was found.

    review_status is determined by (source, action):
    - human approve/reject → human_approved/human_rejected
    - agent approve/reject → agent_approved/agent_rejected
    - flag → pending_review (any source)
    - correct → merges corrections, sets approved status per source
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

    if feedback.action in _ACTION_PAST:
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        updates: dict[str, Any] = {"review_status": f"{prefix}_{_ACTION_PAST[feedback.action]}"}
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
        updates = {"review_status": "pending_review"}
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
        updates["review_status"] = f"{prefix}_approved"
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
