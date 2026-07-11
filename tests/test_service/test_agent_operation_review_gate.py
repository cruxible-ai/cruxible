"""Smoke tests for the agent-operation kit review-mediated done gate.

Proves the shipped kit config rejects any direct write resulting in
WorkItem.status=closed — creating work as closed included — until an
approved ReviewRequest reviews the work item, then allows the same write,
across single direct writes and batch direct writes.

Unlike the retired project-state kit, agent-operation gates ReviewRequest
*verdicts* with two stricter guards:

* ``review_request_approval_requires_authorized_actor`` — advancing a
  ReviewRequest to ``approved`` requires an actor_context whose ``actor_id`` is
  ``authorized-reviewer`` AND (``distinct_from_creation_actor``) an actor that
  differs from the one recorded in the ReviewRequest's creation receipt. The
  separation makes create-with-approved impossible (creator == actor
  trivially), so approved reviews are always seeded in two steps: a creation
  write by the implementer actor, then an approval write by the reviewer.
* ``review_verdict_requires_rationale_note`` — any verdict transition
  (``changes_requested``/``approved``/``withdrawn``) must co-write a
  ``StateNote(kind=review_note)`` linked via ``state_note_about_review_request``
  in the same write.

These tests therefore satisfy both guards when seeding/approving reviews, and
assert the actor guard rejects unauthorized approvals and self-approvals.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import (
    RelationshipAssertion,
    RelationshipReviewState,
)
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_batch_direct_write,
)
from cruxible_core.temporal import utc_now

KIT_CONFIG = Path(__file__).resolve().parents[2] / "kits" / "agent-operation" / "config.yaml"


def _agent_operation_instance(tmp_path: Path) -> CruxibleInstance:
    shutil.copy(KIT_CONFIG, tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor_context(actor_id: str = "authorized-reviewer") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
    )


def _review_note_entity(note_id: str = "sn-gated") -> EntityWriteInput:
    return EntityWriteInput(
        entity_type="StateNote",
        entity_id=note_id,
        properties={
            "note_id": note_id,
            "kind": "review_note",
            "title": "Review rationale",
            "summary": "Verdict rationale.",
            "body": "Verdict recorded with rationale.",
            "created_at": utc_now(),
        },
    )


def _note_about_review(note_id: str, review_request_id: str) -> BatchRelationshipWriteInput:
    return BatchRelationshipWriteInput(
        from_type="StateNote",
        from_id=note_id,
        relationship_type="state_note_about_review_request",
        to_type="ReviewRequest",
        to_id=review_request_id,
    )


def _seed_work_item(instance: CruxibleInstance, status: str = "active") -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-gated",
                properties={
                    "work_item_id": "wi-gated",
                    "title": "Gated work item",
                    "type": "feature",
                    "status": status,
                    "priority": "high",
                },
            )
        ],
    )


_IMPLEMENTER = "impl-agent"


def _seed_review(instance: CruxibleInstance, status: str) -> None:
    """Seed a ReviewRequest at ``status`` linked to wi-gated.

    Creation is always a separate write by the implementer actor: approvals
    require an actor distinct from the creation actor, so create-with-approved
    can never satisfy the guard. Verdict statuses
    (changes_requested/approved/withdrawn) then advance in a second batch that
    co-writes the required review_note; approvals use the authorized-reviewer
    actor.
    """
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-gated",
                properties={
                    "review_request_id": "rr-gated",
                    "title": "Review gated work item",
                    "status": "requested",
                },
            )
        ],
        actor_context=_actor_context(_IMPLEMENTER),
    )
    service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="ReviewRequest",
                from_id="rr-gated",
                relationship_type="review_request_for_work_item",
                to_type="WorkItem",
                to_id="wi-gated",
            )
        ],
        source="test",
        source_ref="review-gate-smoke",
    )
    if status == "requested":
        return

    verdict_statuses = {"changes_requested", "approved", "withdrawn"}
    if status in verdict_statuses:
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-gated",
                        properties={"status": status},
                    ),
                    _review_note_entity(),
                ],
                relationships=[_note_about_review("sn-gated", "rr-gated")],
            ),
            actor_context=_actor_context(),
        )
        return

    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-gated",
                properties={"status": status},
            )
        ],
        actor_context=_actor_context(_IMPLEMENTER),
    )


def _approve_review(
    instance: CruxibleInstance,
    *,
    actor_context: GovernedActorContext | None,
    note_id: str = "sn-approve",
) -> None:
    """Advance rr-gated to approved, co-writing the required review_note."""
    service_batch_direct_write(
        instance,
        BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-gated",
                    properties={"status": "approved"},
                ),
                _review_note_entity(note_id),
            ],
            relationships=[_note_about_review(note_id, "rr-gated")],
        ),
        actor_context=actor_context,
    )


def _close_work_item(instance: CruxibleInstance) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-gated",
                properties={"status": "closed"},
            )
        ],
    )


def _work_item_status(instance: CruxibleInstance) -> str:
    entity = instance.load_graph().get_entity("WorkItem", "wi-gated")
    assert entity is not None
    return entity.properties["status"]


class TestAgentOperationReviewGate:
    def test_close_rejected_without_any_review(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            _close_work_item(instance)

        assert _work_item_status(instance) == "active"

    def test_close_rejected_with_unapproved_review(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="changes_requested")

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            _close_work_item(instance)

        assert _work_item_status(instance) == "active"

    def test_close_allowed_after_approved_review(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="approved")

        _close_work_item(instance)

        assert _work_item_status(instance) == "closed"

    def test_same_transition_rejected_then_allowed(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(DataValidationError):
            _close_work_item(instance)
        assert _work_item_status(instance) == "active"

        _approve_review(instance, actor_context=_actor_context())

        _close_work_item(instance)
        assert _work_item_status(instance) == "closed"

    def test_approval_rejected_without_authorized_actor(self, tmp_path: Path) -> None:
        # agent-operation gates approval on the authorized-reviewer actor. With
        # no actor_context the approval is rejected (project-state allowed this).
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(
            DataValidationError,
            match="review_request_approval_requires_authorized_actor",
        ):
            _approve_review(instance, actor_context=None)

        review = instance.load_graph().get_entity("ReviewRequest", "rr-gated")
        assert review is not None
        assert review.properties["status"] == "requested"

    def test_approval_requires_authorized_reviewer_actor(self, tmp_path: Path) -> None:
        # An arbitrary actor (e.g. the writer credential) cannot approve; only
        # the authorized-reviewer actor can advance the verdict to approved.
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(
            DataValidationError,
            match="review_request_approval_requires_authorized_actor",
        ):
            _approve_review(instance, actor_context=_actor_context("codex-core"))

        assert (
            instance.load_graph().get_entity("ReviewRequest", "rr-gated").properties["status"]
            == "requested"
        )

        _approve_review(instance, actor_context=_actor_context("authorized-reviewer"))

        review = instance.load_graph().get_entity("ReviewRequest", "rr-gated")
        assert review is not None
        assert review.properties["status"] == "approved"

    def test_approval_rejected_for_review_creator(self, tmp_path: Path) -> None:
        # distinct_from_creation_actor: even the authorized-reviewer actor
        # cannot approve a ReviewRequest whose creation receipt records that
        # same actor — the review's creator can never approve it.
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-gated",
                    properties={
                        "review_request_id": "rr-gated",
                        "title": "Review gated work item",
                        "status": "requested",
                    },
                )
            ],
            actor_context=_actor_context("authorized-reviewer"),
        )

        with pytest.raises(
            DataValidationError,
            match="review_request_approval_requires_authorized_actor",
        ):
            _approve_review(instance, actor_context=_actor_context("authorized-reviewer"))

        assert (
            instance.load_graph().get_entity("ReviewRequest", "rr-gated").properties["status"]
            == "requested"
        )

    def test_approval_rejected_without_co_written_review_note(self, tmp_path: Path) -> None:
        # The authorized actor still cannot approve without co-writing the
        # rationale review_note in the same batch.
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(
            DataValidationError,
            match="review_verdict_requires_rationale_note",
        ):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-gated",
                        properties={"status": "approved"},
                    )
                ],
                actor_context=_actor_context(),
            )

        assert (
            instance.load_graph().get_entity("ReviewRequest", "rr-gated").properties["status"]
            == "requested"
        )

    def test_batch_close_rejected_atomically_without_review(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-gated",
                            properties={"status": "closed"},
                        ),
                        EntityWriteInput(
                            entity_type="ReviewRequest",
                            entity_id="rr-batch",
                            properties={
                                "review_request_id": "rr-batch",
                                "title": "Unlinked review",
                                "status": "approved",
                            },
                        ),
                        _review_note_entity("sn-batch"),
                    ],
                    relationships=[_note_about_review("sn-batch", "rr-batch")],
                ),
                actor_context=_actor_context(),
            )

        assert _work_item_status(instance) == "active"
        assert instance.load_graph().get_entity("ReviewRequest", "rr-batch") is None

    def test_create_work_item_as_closed_rejected(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={
                            "work_item_id": "wi-born-closed",
                            "title": "Born closed",
                            "type": "feature",
                            "status": "closed",
                            "priority": "low",
                        },
                    )
                ],
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_as_closed_rejected_atomically(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-born-closed",
                            properties={
                                "work_item_id": "wi-born-closed",
                                "title": "Born closed",
                                "type": "feature",
                                "status": "closed",
                                "priority": "low",
                            },
                        )
                    ],
                ),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_as_approved_rejected(self, tmp_path: Path) -> None:
        # Creating a ReviewRequest already approved is impossible under
        # distinct_from_creation_actor: the creation actor IS the acting actor,
        # so the old maintainer-led import shape (closed work + its approved
        # review in one batch) is refused. Imports land reviews at requested
        # and approve them with a second, distinct credential.
        instance = _agent_operation_instance(tmp_path)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-born-closed",
                            properties={
                                "work_item_id": "wi-born-closed",
                                "title": "Imported finished work",
                                "type": "feature",
                                "status": "closed",
                                "priority": "low",
                            },
                        ),
                        EntityWriteInput(
                            entity_type="ReviewRequest",
                            entity_id="rr-import",
                            properties={
                                "review_request_id": "rr-import",
                                "title": "Import-time review",
                                "status": "approved",
                            },
                        ),
                        _review_note_entity("sn-import"),
                    ],
                    relationships=[
                        BatchRelationshipWriteInput(
                            from_type="ReviewRequest",
                            from_id="rr-import",
                            relationship_type="review_request_for_work_item",
                            to_type="WorkItem",
                            to_id="wi-born-closed",
                        ),
                        _note_about_review("sn-import", "rr-import"),
                    ],
                ),
                actor_context=_actor_context(),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None
        assert instance.load_graph().get_entity("ReviewRequest", "rr-import") is None

    def test_batch_close_allowed_with_same_batch_review_link(self, tmp_path: Path) -> None:
        # An approved review (created by the implementer, approved by the
        # reviewer) can land its work-item link in the same batch as the close.
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-batch",
                    properties={
                        "review_request_id": "rr-batch",
                        "title": "Same-batch review",
                        "status": "requested",
                    },
                )
            ],
            actor_context=_actor_context(_IMPLEMENTER),
        )
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-batch",
                        properties={"status": "approved"},
                    ),
                    _review_note_entity("sn-batch"),
                ],
                relationships=[_note_about_review("sn-batch", "rr-batch")],
            ),
            actor_context=_actor_context(),
        )

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-gated",
                        properties={"status": "closed"},
                    ),
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="ReviewRequest",
                        from_id="rr-batch",
                        relationship_type="review_request_for_work_item",
                        to_type="WorkItem",
                        to_id="wi-gated",
                    ),
                ],
            ),
            actor_context=_actor_context(),
        )

        assert result.valid is True
        assert _work_item_status(instance) == "closed"


class TestCloseGatePendingEdgeExploit:
    """Regression for wi-close-gate-requires-approved-review.

    The close gate query ``approved_reviews_for_work_item`` previously used
    ``relationship_state: reviewable`` (live OR review-pending edges). A
    ``review_request_for_work_item`` edge whose relationship-review assertion is
    still ``pending`` -- i.e. the link itself never cleared review -- therefore
    satisfied the gate, letting a WorkItem close with no adjudicated review.
    Switching the query to ``relationship_state: live`` drops pending/rejected
    edges while keeping the legitimate direct-write (``unreviewed``) edge that
    an approved ReviewRequest is linked by.
    """

    @staticmethod
    def _seed_approved_review_without_edge(instance: CruxibleInstance) -> None:
        """Create an APPROVED rr-gated (actor-guarded, note co-written), no work edge.

        Two writes: the implementer creates the review, the reviewer approves —
        create-with-approved is refused by distinct_from_creation_actor.
        """
        service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="ReviewRequest",
                    entity_id="rr-gated",
                    properties={
                        "review_request_id": "rr-gated",
                        "title": "Review gated work item",
                        "status": "requested",
                    },
                )
            ],
            actor_context=_actor_context(_IMPLEMENTER),
        )
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-gated",
                        properties={"status": "approved"},
                    ),
                    _review_note_entity(),
                ],
                relationships=[_note_about_review("sn-gated", "rr-gated")],
            ),
            actor_context=_actor_context(),
        )

    @staticmethod
    def _link_review_with_review_status(
        instance: CruxibleInstance,
        *,
        review_status: str,
    ) -> None:
        """Attach rr-gated -> wi-gated with an explicit assertion review status."""
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                from_type="ReviewRequest",
                from_id="rr-gated",
                relationship_type="review_request_for_work_item",
                to_type="WorkItem",
                to_id="wi-gated",
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(status=review_status)
                    )
                ),
            )
        )
        instance.save_graph(graph)

    def test_close_rejected_with_only_pending_review_edge(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        # An approved ReviewRequest entity, but linked by a still-PENDING edge
        # that never cleared review -- the exact exploit shape.
        self._seed_approved_review_without_edge(instance)
        self._link_review_with_review_status(instance, review_status="pending")

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            _close_work_item(instance)

        assert _work_item_status(instance) == "active"

    def test_close_allowed_with_live_review_edge(self, tmp_path: Path) -> None:
        instance = _agent_operation_instance(tmp_path)
        _seed_work_item(instance)
        self._seed_approved_review_without_edge(instance)
        # The legitimate link an approved review lands on via direct write:
        # lifecycle-active and not pending/rejected (unreviewed => live).
        self._link_review_with_review_status(instance, review_status="unreviewed")

        _close_work_item(instance)

        assert _work_item_status(instance) == "closed"
