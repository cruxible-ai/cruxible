"""Apply feedback to the entity graph."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.feedback.types import FeedbackRecord
from cruxible_core.governance.actors import GovernedActorContext, derived_actor_kind
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipReviewSource,
    RelationshipReviewStatus,
)
from cruxible_core.graph.provenance import (
    RelationshipProvenance,
    backfill_provenance_on_touch,
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


def _apply_feedback_provenance(
    metadata: RelationshipMetadata,
    prov: RelationshipProvenance | None,
    feedback: FeedbackRecord,
) -> RelationshipMetadata:
    """Stamp the touched edge's provenance, backfilling it when null.

    A null-provenance edge would otherwise stay null forever; touching it via feedback
    backfills a marker provenance so the edge becomes auditable. Feedback does not
    claim authorship of an edge it merely adjudicated — see
    :func:`backfill_provenance_on_touch`.
    """
    return metadata.model_copy(
        update={
            "provenance": backfill_provenance_on_touch(
                prov,
                f"feedback:{feedback.action}",
                actor_context=feedback.actor_context,
            ),
        }
    )


def _review_source_for(feedback: FeedbackRecord) -> RelationshipReviewSource:
    """Return the review-state source DERIVED from the feedback's actor.

    Read off ``actor_context``, never off a caller-declared field: an edge's
    review state records who adjudicated it, and that must not be a claim the
    adjudicator made about itself.
    """
    kind = derived_actor_kind(feedback.actor_context)
    if kind in ("human", "agent", "system"):
        return kind
    return "unknown"


_ACTION_PAST: dict[str, RelationshipReviewStatus] = {
    "accept": "approved",
    # Historical 0.2.x feedback rows retain the shipped verb on read.
    "approve": "approved",
    "reject": "rejected",
}

# Feedback actions that transition a relationship's review state. ``accept``
# and ``correct`` promote the status to ``approved`` (the ``correct`` branch in
# ``apply_feedback`` below calls ``_review_metadata(..., status="approved")``),
# making a previously non-live edge live and able to satisfy a review-mediated
# close-gate precondition. ``reject`` (-> ``rejected``) moves an edge OUT of
# live review state — an anonymous retraction is as much a governance hole as an
# anonymous promotion (wi-feedback-write-tier-bypass, mechanism 2). Every
# transition must therefore carry a resolved actor identity under the governed
# (auth-on) runtime. See ``runtime.api`` for the enforcement point.
#
# ``flag`` was removed in 2026-07: it also moved an edge out of live state
# (-> ``pending``) but stored no annotation, so it destroyed the reviewer's
# signal. Use ``cruxible attest --stance contradict`` instead.
REVIEW_TRANSITION_ACTIONS: frozenset[str] = frozenset({"accept", "approve", "correct", "reject"})


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

    prefix = _review_source_for(feedback)
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
        metadata = _apply_feedback_provenance(metadata, prov, feedback)
        return graph.update_relationship_state(
            from_type=t.from_type,
            from_id=t.from_id,
            to_type=t.to_type,
            to_id=t.to_id,
            relationship_type=t.relationship_type,
            metadata=metadata,
            edge_key=edge_key,
        )

    # REMOVED (2026-07-26): the ``flag`` action. It un-approved an edge to
    # ``pending`` while storing no annotation anywhere, so the reviewer's actual
    # signal -- what they doubted and why -- was destroyed at the moment it was
    # given, leaving only a status regression nobody could interpret. Recording
    # a doubt is what ``cruxible attest --stance contradict`` is for: it stores
    # the observation, its evidence refs, and its actor, and it changes no
    # status. There is no deprecation window; the action never worked.

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
        metadata = _apply_feedback_provenance(metadata, prov, feedback)
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
