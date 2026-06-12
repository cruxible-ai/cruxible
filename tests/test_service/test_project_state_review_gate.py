"""Smoke tests for the project-state kit review-mediated done gate.

Proves the shipped kit config rejects any direct write resulting in
WorkItem.status=closed — creating work as closed included — until an
approved ReviewRequest reviews the work item, then allows the same write,
across single direct writes and batch direct writes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
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

KIT_CONFIG = (
    Path(__file__).resolve().parents[2] / "kits" / "project-state" / "config.yaml"
)


def _project_state_instance(tmp_path: Path) -> CruxibleInstance:
    shutil.copy(KIT_CONFIG, tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _actor_context(actor_id: str = "robert") -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org_1",
        operation_id=f"op_{actor_id}",
        timestamp=utc_now(),
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


def _seed_review(instance: CruxibleInstance, status: str) -> None:
    actor_context = _actor_context() if status == "approved" else None
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="ReviewRequest",
                entity_id="rr-gated",
                properties={
                    "review_request_id": "rr-gated",
                    "title": "Review gated work item",
                    "status": status,
                },
            )
        ],
        actor_context=actor_context,
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


class TestProjectStateReviewGate:
    def test_close_rejected_without_any_review(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            _close_work_item(instance)

        assert _work_item_status(instance) == "active"

    def test_close_rejected_with_unapproved_review(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="changes_requested")

        with pytest.raises(
            DataValidationError,
            match="work_item_closed_requires_approved_review",
        ):
            _close_work_item(instance)

        assert _work_item_status(instance) == "active"

    def test_close_allowed_after_approved_review(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="approved")

        _close_work_item(instance)

        assert _work_item_status(instance) == "closed"

    def test_same_transition_rejected_then_allowed(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(DataValidationError):
            _close_work_item(instance)
        assert _work_item_status(instance) == "active"

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

        _close_work_item(instance)
        assert _work_item_status(instance) == "closed"

    def test_approval_rejected_without_authorized_actor(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(
            DataValidationError,
            match="review_request_approval_requires_authorized_actor",
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
            )

        review = instance.load_graph().get_entity("ReviewRequest", "rr-gated")
        assert review is not None
        assert review.properties["status"] == "requested"

    def test_approval_rejected_with_unauthorized_actor(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)
        _seed_review(instance, status="requested")

        with pytest.raises(
            DataValidationError,
            match="review_request_approval_requires_authorized_actor",
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
                actor_context=_actor_context("codex-core"),
            )

        review = instance.load_graph().get_entity("ReviewRequest", "rr-gated")
        assert review is not None
        assert review.properties["status"] == "requested"

    def test_batch_close_rejected_atomically_without_review(
        self, tmp_path: Path
    ) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(
            DataValidationError, match="Batch direct write validation failed"
        ):
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
                    ],
                ),
                actor_context=_actor_context(),
            )

        assert _work_item_status(instance) == "active"
        assert instance.load_graph().get_entity("ReviewRequest", "rr-batch") is None

    def test_create_work_item_as_closed_rejected(self, tmp_path: Path) -> None:
        instance = _project_state_instance(tmp_path)

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
        instance = _project_state_instance(tmp_path)

        with pytest.raises(
            DataValidationError, match="Batch direct write validation failed"
        ):
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

    def test_batch_create_as_closed_allowed_with_same_batch_review(
        self, tmp_path: Path
    ) -> None:
        # The maintainer-led import path: historical closed work lands with
        # its reviews in the same batch.
        instance = _project_state_instance(tmp_path)

        result = service_batch_direct_write(
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
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="ReviewRequest",
                        from_id="rr-import",
                        relationship_type="review_request_for_work_item",
                        to_type="WorkItem",
                        to_id="wi-born-closed",
                    )
                ],
            ),
            actor_context=_actor_context(),
        )

        assert result.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-born-closed")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_batch_close_allowed_with_same_batch_approved_review(
        self, tmp_path: Path
    ) -> None:
        instance = _project_state_instance(tmp_path)
        _seed_work_item(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="ReviewRequest",
                        entity_id="rr-batch",
                        properties={
                            "review_request_id": "rr-batch",
                            "title": "Same-batch review",
                            "status": "approved",
                        },
                    ),
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
                    )
                ],
            ),
            actor_context=_actor_context(),
        )

        assert result.valid is True
        assert _work_item_status(instance) == "closed"
