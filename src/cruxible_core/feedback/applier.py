"""Apply feedback to the entity graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.feedback.types import FeedbackRecord
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipReviewSource,
    RelationshipReviewStatus,
)
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    stamp_provenance_modified,
)
from cruxible_core.graph.types import RelationshipMetadata
from cruxible_core.temporal import utc_now

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
    """Read existing provenance from an edge."""
    existing = _read_relationship(graph, t, relationship, edge_key)
    if existing:
        provenance = existing.metadata.provenance
        if isinstance(provenance, RelationshipProvenance):
            return provenance
    return None


def _stamp_provenance(
    prov: RelationshipProvenance,
    action: str,
    actor_context: GovernedActorContext | None,
) -> RelationshipProvenance:
    """Return provenance stamped for a feedback action."""
    return stamp_provenance_modified(
        prov,
        f"feedback:{action}",
        actor_context=actor_context,
    )


_SOURCE_PREFIX: dict[str, RelationshipReviewSource] = {
    "human": "human",
    "agent": "agent",
}

_ACTION_PAST: dict[str, RelationshipReviewStatus] = {
    "approve": "approved",
    "reject": "rejected",
}


def _review_metadata(
    graph: EntityGraph,
    t: Any,
    relationship: str,
    edge_key: int | None,
    *,
    status: RelationshipReviewStatus,
    source: RelationshipReviewSource,
    actor: str,
    actor_context: GovernedActorContext | None,
) -> RelationshipMetadata:
    existing = _read_relationship(graph, t, relationship, edge_key)
    metadata = existing.metadata if existing is not None else RelationshipMetadata()
    current_assertion = metadata.assertion if existing is not None else RelationshipAssertion()
    review = current_assertion.review.model_copy(
        update={
            "status": status,
            "source": source,
            "updated_at": utc_now(),
            "updated_by": actor,
            "actor_context": actor_context,
        }
    )
    assertion = current_assertion.model_copy(update={"review": review})
    return metadata.model_copy(update={"assertion": assertion})


def apply_feedback(graph: EntityGraph, feedback: FeedbackRecord) -> bool:
    """Apply a feedback record to the graph. Returns True if the edge was found.

    Review state is determined by (source, action) and written through relationship metadata.
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
        metadata = _review_metadata(
            graph,
            t,
            t.relationship_type,
            edge_key,
            status=_ACTION_PAST[feedback.action],
            source=prefix,
            actor=actor,
            actor_context=feedback.actor_context,
        )
        if prov:
            metadata = metadata.model_copy(
                update={
                    "provenance": _stamp_provenance(
                        prov,
                        feedback.action,
                        feedback.actor_context,
                    )
                }
            )
        return graph.update_relationship_state(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            metadata=metadata,
            edge_key=edge_key,
        )

    if feedback.action == "flag":
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        metadata = _review_metadata(
            graph,
            t,
            t.relationship_type,
            edge_key,
            status="pending",
            source=prefix,
            actor=actor,
            actor_context=feedback.actor_context,
        )
        if prov:
            metadata = metadata.model_copy(
                update={
                    "provenance": _stamp_provenance(
                        prov,
                        feedback.action,
                        feedback.actor_context,
                    )
                }
            )
        return graph.update_relationship_state(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            metadata=metadata,
            edge_key=edge_key,
        )

    if feedback.action == "correct":
        updates = dict(feedback.corrections)
        metadata = _review_metadata(
            graph,
            t,
            t.relationship_type,
            edge_key,
            status="approved",
            source=prefix,
            actor=actor,
            actor_context=feedback.actor_context,
        )
        prov = _read_provenance(graph, t, t.relationship_type, edge_key)
        if prov:
            metadata = metadata.model_copy(
                update={
                    "provenance": _stamp_provenance(
                        prov,
                        feedback.action,
                        feedback.actor_context,
                    )
                }
            )
        return graph.update_relationship_state(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            property_updates=updates,
            metadata=metadata,
            edge_key=edge_key,
        )

    return False
