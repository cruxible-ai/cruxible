"""Smoke tests for the project-state kit review-mediated done gate.

Proves the shipped kit config rejects WorkItem.status=closed until an
approved ReviewRequest reviews the work item, then allows the same
transition, across single direct writes and batch direct writes.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_batch_direct_write,
)

KIT_CONFIG = (
    Path(__file__).resolve().parents[2] / "kits" / "project-state" / "config.yaml"
)


def _project_state_instance(tmp_path: Path) -> CruxibleInstance:
    shutil.copy(KIT_CONFIG, tmp_path / "config.yaml")
    return CruxibleInstance.init(tmp_path, "config.yaml")


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
        )

        _close_work_item(instance)
        assert _work_item_status(instance) == "closed"

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
            )

        assert _work_item_status(instance) == "active"
        assert instance.load_graph().get_entity("ReviewRequest", "rr-batch") is None

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
        )

        assert result.valid is True
        assert _work_item_status(instance) == "closed"
