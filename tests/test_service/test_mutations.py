"""Tests for service layer mutation functions."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
    service_add_entities,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_add_relationships,
    service_batch_direct_write,
    service_register_source_artifact,
)
from cruxible_core.storage.sqlite import SQLiteGraphRepository


def _vehicle(
    vid: str, year: int = 2024, make: str = "Honda", model: str = "Civic"
) -> EntityInstance:
    return EntityInstance(
        entity_type="Vehicle",
        entity_id=vid,
        properties={"vehicle_id": vid, "year": year, "make": make, "model": model},
    )


def _part(pid: str, name: str = "Pads", category: str = "brakes") -> EntityInstance:
    return EntityInstance(
        entity_type="Part",
        entity_id=pid,
        properties={"part_number": pid, "name": name, "category": category},
    )


def _batch_payload() -> BatchDirectWriteInput:
    return BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type="Vehicle",
                entity_id="V-BATCH",
                properties={
                    "vehicle_id": "V-BATCH",
                    "year": 2026,
                    "make": "Honda",
                    "model": "Pilot",
                },
            ),
            EntityWriteInput(
                entity_type="Part",
                entity_id="BP-BATCH",
                properties={
                    "part_number": "BP-BATCH",
                    "name": "Batch Pads",
                    "category": "brakes",
                },
            ),
        ],
        relationships=[
            BatchRelationshipWriteInput(
                from_type="Part",
                from_id="BP-BATCH",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-BATCH",
                properties={"verified": True, "source": "batch"},
                shared_evidence_keys=["doc"],
                evidence_rationale="Batch payload establishes the fitment.",
            )
        ],
        shared_evidence={
            "doc": SharedEvidenceInput(
                evidence_refs=[{"source": "roadmap_doc", "source_record_id": "batch-section"}],
            )
        },
    )


GUARDED_STATE_YAML = """\
version: "1.0"
name: guarded_project_state

enums:
  lifecycle_status:
    values: [planned, active, closed]
  review_status:
    values: [pending, approved]

entity_types:
  WorkItem:
    properties:
      work_item_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: lifecycle_status
      title:
        type: string
        optional: true
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string
        enum_ref: review_status

relationships:
  - name: review_approves_work_item
    from: Review
    to: WorkItem

named_queries:
  approved_review_for_work_item:
    mode: traversal
    entry_point: WorkItem
    traversal:
      - relationship: review_approves_work_item
        direction: incoming
        where:
          candidate.properties.status:
            eq: approved
    returns: list[Review]
    result_shape: entity
  trusted_batch_approved_review_for_work_item:
    mode: traversal
    entry_point: WorkItem
    traversal:
      - relationship: review_approves_work_item
        direction: incoming
        where:
          candidate.properties.status:
            eq: approved
          edge.metadata.provenance.source:
            eq: trusted_batch
    returns: list[Review]
    result_shape: entity

mutation_guards:
  - name: work_item_closed_requires_review
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      query_name: approved_review_for_work_item
      params:
        work_item_id: "$entity.entity_id"
      min_count: 1
    message: "Work item cannot be closed until approved review exists."
  - name: work_item_active_requires_trusted_batch_review
    entity_type: WorkItem
    property: status
    new_value: active
    condition:
      query_name: trusted_batch_approved_review_for_work_item
      params:
        work_item_id: "$entity.entity_id"
      min_count: 1
    message: "Work item cannot be activated until a trusted batch review exists."
"""


def _guarded_instance(tmp_path: Path) -> CruxibleInstance:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(dedent(GUARDED_STATE_YAML))
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _seed_work_item(instance: CruxibleInstance, status: str = "planned") -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="WorkItem",
                entity_id="wi-guarded",
                properties={
                    "work_item_id": "wi-guarded",
                    "status": status,
                    "title": "Guarded item",
                },
            )
        ],
    )


def _seed_approved_review(instance: CruxibleInstance) -> None:
    service_add_entity_inputs(
        instance,
        [
            EntityWriteInput(
                entity_type="Review",
                entity_id="rev-approved",
                properties={"review_id": "rev-approved", "status": "approved"},
            )
        ],
    )
    service_add_relationship_inputs(
        instance,
        [
            RelationshipWriteInput(
                from_type="Review",
                from_id="rev-approved",
                relationship_type="review_approves_work_item",
                to_type="WorkItem",
                to_id="wi-guarded",
            )
        ],
        source="test",
        source_ref="approved-review",
    )


# ---------------------------------------------------------------------------
# service_add_entities
# ---------------------------------------------------------------------------


class TestAddEntities:
    def test_single(self, initialized_instance: CruxibleInstance) -> None:
        result = service_add_entities(initialized_instance, [_vehicle("V-1")])
        assert result.added == 1
        assert result.updated == 0

        graph = initialized_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-1")
        assert entity is not None
        assert entity.properties["make"] == "Honda"

    def test_input_wrapper(self, initialized_instance: CruxibleInstance) -> None:
        result = service_add_entity_inputs(
            initialized_instance,
            [
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-1",
                    properties={
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                    metadata={"source": "input-wrapper"},
                )
            ],
        )

        assert result.added == 1
        graph = initialized_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-1")
        assert entity is not None
        assert entity.metadata == {"source": "input-wrapper"}

    def test_batch(self, initialized_instance: CruxibleInstance) -> None:
        entities = [
            _vehicle("V-1"),
            _vehicle("V-2", make="Toyota", model="Camry"),
            _part("BP-1"),
        ]
        result = service_add_entities(initialized_instance, entities)
        assert result.added == 3
        assert result.updated == 0

    def test_dedup_error(self, initialized_instance: CruxibleInstance) -> None:
        entities = [_vehicle("V-1"), _vehicle("V-1", year=2025)]
        with pytest.raises(DataValidationError, match="duplicate in batch"):
            service_add_entities(initialized_instance, entities)

    def test_bad_type(self, initialized_instance: CruxibleInstance) -> None:
        with pytest.raises(DataValidationError, match="not found in config"):
            service_add_entities(
                initialized_instance,
                [EntityInstance(entity_type="Spaceship", entity_id="X-1")],
            )

    def test_update(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_entities(
            populated_instance,
            [_vehicle("V-2024-CIVIC-EX", year=2025)],
        )
        assert result.added == 0
        assert result.updated == 1

        graph = populated_instance.load_graph()
        entity = graph.get_entity("Vehicle", "V-2024-CIVIC-EX")
        assert entity is not None
        assert entity.properties["year"] == 2025

    def test_update_merges_entity_metadata(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.update_entity_metadata("Vehicle", "V-2024-CIVIC-EX", {"origin": "fixture"})
        populated_instance.save_graph(graph)

        result = service_add_entity_inputs(
            populated_instance,
            [
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-2024-CIVIC-EX",
                    properties={"year": 2025},
                    metadata={"last_seen": "service"},
                )
            ],
        )

        assert result.added == 0
        assert result.updated == 1
        entity = populated_instance.load_graph().get_entity("Vehicle", "V-2024-CIVIC-EX")
        assert entity is not None
        assert entity.metadata == {"origin": "fixture", "last_seen": "service"}

    def test_incremental_entity_mutation_does_not_full_save(
        self, initialized_instance: CruxibleInstance
    ) -> None:
        def fail_full_save(_self: SQLiteGraphRepository, _graph) -> None:
            raise AssertionError("service_add_entities should not replace the full graph")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_full_save):
            result = service_add_entities(initialized_instance, [_vehicle("V-1")])

        assert result.added == 1
        restarted = CruxibleInstance.load(initialized_instance.root)
        assert restarted.load_graph().get_entity("Vehicle", "V-1") is not None


class TestEntityMutationGuards:
    def test_unmatched_guard_allows_unrelated_update(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"title": "Updated title"},
                )
            ],
        )

        assert result.updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"
        assert entity.properties["title"] == "Updated title"

    def test_add_entity_update_rejects_closed_without_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError) as exc_info:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            )

        assert "work_item_closed_requires_review" in str(exc_info.value)
        assert "Work item cannot be closed" in str(exc_info.value)
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"

    def test_add_entity_guard_failure_receipt_records_guard_error(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError) as exc_info:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            )

        receipt_id = exc_info.value.mutation_receipt_id
        assert receipt_id is not None
        store = instance.get_receipt_store()
        try:
            receipt = store.get_receipt(receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False
        failed_validations = [
            node
            for node in receipt.nodes
            if node.node_type == "validation" and node.detail.get("passed") is False
        ]
        assert any(
            "work_item_closed_requires_review" in node.detail.get("guard_error", "")
            for node in failed_validations
        )

    def test_add_entity_update_allows_closed_with_approved_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "closed"},
                )
            ],
        )

        assert result.updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_batch_dry_run_reports_guard_failure_without_mutating(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            ),
            dry_run=True,
        )

        assert result.valid is False
        assert any(
            "work_item_closed_requires_review" in error for error in result.validation_errors
        )
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"

    def test_batch_apply_rejects_guard_atomically(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="WorkItem",
                            entity_id="wi-guarded",
                            properties={"status": "closed"},
                        ),
                        EntityWriteInput(
                            entity_type="Review",
                            entity_id="rev-pending",
                            properties={"review_id": "rev-pending", "status": "pending"},
                        ),
                    ],
                ),
            )

        graph = instance.load_graph()
        work_item = graph.get_entity("WorkItem", "wi-guarded")
        assert work_item is not None
        assert work_item.properties["status"] == "planned"
        assert graph.get_entity("Review", "rev-pending") is None

    def test_batch_apply_allows_closed_with_approved_review(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    )
                ],
            ),
        )

        assert result.valid is True
        assert result.entities_updated == 1
        entity = instance.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_batch_apply_allows_closed_with_same_batch_approved_review(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-approved",
                        properties={"review_id": "rev-approved", "status": "approved"},
                    ),
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-guarded",
                        properties={"status": "closed"},
                    ),
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Review",
                        from_id="rev-approved",
                        relationship_type="review_approves_work_item",
                        to_type="WorkItem",
                        to_id="wi-guarded",
                    )
                ],
            ),
        )

        assert result.valid is True
        assert result.entities_added == 1
        assert result.entities_updated == 1
        assert result.relationships_added == 1
        graph = instance.load_graph()
        entity = graph.get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "closed"
        assert (
            graph.get_relationship(
                "Review",
                "rev-approved",
                "WorkItem",
                "wi-guarded",
                "review_approves_work_item",
            )
            is not None
        )

    def test_batch_guard_evaluates_same_batch_relationship_with_real_source(
        self,
        tmp_path: Path,
    ) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        payload = BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-approved",
                    properties={"review_id": "rev-approved", "status": "approved"},
                ),
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "active"},
                ),
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Review",
                    from_id="rev-approved",
                    relationship_type="review_approves_work_item",
                    to_type="WorkItem",
                    to_id="wi-guarded",
                )
            ],
        )

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(
                instance,
                payload,
                source="untrusted_batch",
                source_ref="untrusted",
            )
        graph = instance.load_graph()
        entity = graph.get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["status"] == "planned"
        assert graph.get_entity("Review", "rev-approved") is None

        trusted_instance = _guarded_instance(tmp_path / "trusted")
        _seed_work_item(trusted_instance)
        result = service_batch_direct_write(
            trusted_instance,
            payload,
            source="trusted_batch",
            source_ref="trusted",
        )

        assert result.valid is True
        relationship = trusted_instance.load_graph().get_relationship(
            "Review",
            "rev-approved",
            "WorkItem",
            "wi-guarded",
            "review_approves_work_item",
        )
        assert relationship is not None
        assert relationship.metadata.provenance is not None
        assert relationship.metadata.provenance.source == "trusted_batch"

    def test_dry_run_validates_guards_without_mutating(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="work_item_closed_requires_review"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-dry",
                        properties={"work_item_id": "wi-dry", "status": "closed"},
                    )
                ],
                dry_run=True,
            )

        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-dry",
                    properties={"work_item_id": "wi-dry", "status": "planned"},
                )
            ],
            dry_run=True,
        )

        assert result.added == 1
        assert result.updated == 0
        assert result.receipt_id is None
        assert instance.load_graph().get_entity("WorkItem", "wi-dry") is None

    def test_dry_run_add_relationships_does_not_mutate(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)
        _seed_work_item(instance)
        _seed_approved_review(instance)

        result = service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type="Review",
                    from_id="rev-approved",
                    relationship_type="review_approves_work_item",
                    to_type="WorkItem",
                    to_id="wi-guarded",
                )
            ],
            source="test",
            source_ref="dry-run",
            dry_run=True,
        )

        assert result.added == 0
        assert result.updated == 1
        assert result.receipt_id is None

    def test_create_with_guarded_value_rejected(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

        with pytest.raises(DataValidationError, match="work_item_closed_requires_review"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    )
                ],
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_with_guarded_value_rejected_atomically(self, tmp_path: Path) -> None:
        instance = _guarded_instance(tmp_path)

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
                                "status": "closed",
                            },
                        )
                    ],
                ),
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None

    def test_batch_create_with_guarded_value_allowed_with_same_batch_review(
        self, tmp_path: Path
    ) -> None:
        instance = _guarded_instance(tmp_path)

        result = service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    ),
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-import",
                        properties={"review_id": "rev-import", "status": "approved"},
                    ),
                ],
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Review",
                        from_id="rev-import",
                        relationship_type="review_approves_work_item",
                        to_type="WorkItem",
                        to_id="wi-born-closed",
                    )
                ],
            ),
        )

        assert result.valid is True
        entity = instance.load_graph().get_entity("WorkItem", "wi-born-closed")
        assert entity is not None
        assert entity.properties["status"] == "closed"

    def test_reasserting_guarded_value_is_not_a_transition(self, tmp_path: Path) -> None:
        # Work closed before a guard existed must stay editable: re-asserting
        # status=closed alongside other changes is not a transition.
        tmp_path.mkdir(parents=True, exist_ok=True)
        unguarded = GUARDED_STATE_YAML.split("mutation_guards:")[0]
        (tmp_path / "config.yaml").write_text(dedent(unguarded))
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        _seed_work_item(instance, status="closed")

        (tmp_path / "config.yaml").write_text(dedent(GUARDED_STATE_YAML))
        guarded = CruxibleInstance.load(tmp_path)

        result = service_add_entity_inputs(
            guarded,
            [
                EntityWriteInput(
                    entity_type="WorkItem",
                    entity_id="wi-guarded",
                    properties={"status": "closed", "title": "Retitled closed item"},
                )
            ],
        )

        assert result.updated == 1
        entity = guarded.load_graph().get_entity("WorkItem", "wi-guarded")
        assert entity is not None
        assert entity.properties["title"] == "Retitled closed item"

    def test_current_ref_guard_rejects_creates_fail_closed(self, tmp_path: Path) -> None:
        # $current.* params have no prior state on creates; such guards are
        # transition-only in practice and must reject creates, not pass them.
        current_ref_guard = dedent(
            """\
            mutation_guards:
              - name: closed_requires_prior_state_lookup
                entity_type: WorkItem
                property: status
                new_value: closed
                condition:
                  query_name: approved_review_for_work_item
                  params:
                    work_item_id: "$current.properties.work_item_id"
                  min_count: 1
            """
        )
        config_yaml = GUARDED_STATE_YAML.split("mutation_guards:")[0] + current_ref_guard
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(dedent(config_yaml))
        instance = CruxibleInstance.init(tmp_path, "config.yaml")

        with pytest.raises(DataValidationError, match="Missing mutation guard param reference"):
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="WorkItem",
                        entity_id="wi-born-closed",
                        properties={"work_item_id": "wi-born-closed", "status": "closed"},
                    )
                ],
            )

        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None


class TestBatchDirectWrite:
    def test_dry_run_validates_same_payload_relationship_without_mutating(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = _batch_payload()

        result = service_batch_direct_write(initialized_instance, payload, dry_run=True)

        assert result.valid is True
        assert result.dry_run is True
        assert result.entities_added == 2
        assert result.relationships_added == 1
        assert result.evidence_sources_used == ["roadmap_doc"]
        assert result.receipt_id is None
        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-BATCH") is None
        assert (
            graph.get_relationship(
                "Part",
                "BP-BATCH",
                "Vehicle",
                "V-BATCH",
                "fits",
            )
            is None
        )

    def test_apply_writes_batch_and_compact_receipt_summary(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        result = service_batch_direct_write(
            initialized_instance,
            _batch_payload(),
            source="test_batch",
            source_ref="test-batch",
        )

        assert result.valid is True
        assert result.dry_run is False
        assert result.entities_added == 2
        assert result.relationships_added == 1
        assert result.receipt_id is not None
        graph = initialized_instance.load_graph()
        relationship = graph.get_relationship(
            "Part",
            "BP-BATCH",
            "Vehicle",
            "V-BATCH",
            "fits",
        )
        assert relationship is not None
        assert relationship.metadata.evidence is not None
        assert relationship.metadata.evidence.rationale == (
            "Batch payload establishes the fitment."
        )
        assert relationship.metadata.evidence.evidence_refs[0].source == "roadmap_doc"

        store = initialized_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "batch_direct_write"
        assert receipt.committed is True

    def test_invalid_apply_does_not_commit_partial_entities(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        payload = BatchDirectWriteInput(
            entities=[
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-BATCH",
                    properties={
                        "vehicle_id": "V-BATCH",
                        "year": 2026,
                        "make": "Honda",
                        "model": "Pilot",
                    },
                )
            ],
            relationships=[
                BatchRelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-MISSING",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-BATCH",
                    shared_evidence_keys=["missing"],
                )
            ],
        )

        dry_run = service_batch_direct_write(initialized_instance, payload, dry_run=True)
        assert dry_run.valid is False
        assert "shared_evidence key 'missing' not found" in dry_run.validation_errors[0]

        with pytest.raises(DataValidationError, match="Batch direct write validation failed"):
            service_batch_direct_write(initialized_instance, payload)

        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-BATCH") is None


# ---------------------------------------------------------------------------
# service_add_relationships
# ---------------------------------------------------------------------------


class TestAddRelationships:
    def test_single(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref="test_single",
        )
        assert result.added == 1
        assert result.updated == 0

        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None
        assert rel.metadata.provenance is not None

    def test_input_wrapper(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                )
            ],
            source="test",
            source_ref="test_input_wrapper",
        )

        assert result.added == 1
        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None

    def test_input_wrapper_persists_explicit_evidence_refs(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                    evidence_refs=[
                        {
                            "source": "roadmap_doc",
                            "source_record_id": "section-p0",
                        }
                    ],
                    evidence_rationale="Accepted direct source-backed assertion.",
                )
            ],
            source="test",
            source_ref="test_input_wrapper_evidence",
        )

        assert result.added == 1
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "unreviewed"
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.rationale == "Accepted direct source-backed assertion."
        assert [ref.source for ref in rel.metadata.evidence.evidence_refs] == ["roadmap_doc"]
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "section-p0"

    def test_input_wrapper_persists_source_evidence_refs(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        source_path = populated_instance.root / "fitment.md"
        source_path.write_text("# Fitment\n\nBP-1002 fits Accord Sport.\n")
        registered = service_register_source_artifact(
            populated_instance,
            source_path=str(source_path),
        )
        paragraph = next(
            chunk for chunk in registered.chunks if chunk.block_selector == "paragraph:1"
        )

        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                    source_evidence=[
                        {
                            "source_artifact_id": registered.source_artifact_id,
                            "chunk_id": paragraph.chunk_id,
                        }
                    ],
                )
            ],
            source="test",
            source_ref="test_input_wrapper_source_evidence",
        )

        assert result.added == 1
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        evidence_ref = rel.metadata.evidence.evidence_refs[0]
        assert evidence_ref.source == "source_artifact"
        assert evidence_ref.artifact_id == registered.source_artifact_id
        assert evidence_ref.source_record_id == paragraph.chunk_id
        assert evidence_ref.metadata["content_hash"] == paragraph.content_hash

    def test_update_preserves_or_replaces_relationship_evidence(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        target = RelationshipWriteInput(
            from_type="Part",
            from_id="BP-1002",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-ACCORD-SPORT",
            properties={"verified": True},
            evidence_refs=[{"source": "doc", "source_record_id": "first"}],
        )
        service_add_relationship_inputs(
            populated_instance,
            [target],
            source="test",
            source_ref="initial",
        )

        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type=target.from_type,
                    from_id=target.from_id,
                    relationship_type=target.relationship_type,
                    to_type=target.to_type,
                    to_id=target.to_id,
                    properties={"verified": True, "source": "updated"},
                )
            ],
            source="test",
            source_ref="preserve",
        )
        rel = populated_instance.load_graph().get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "first"

        service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type=target.from_type,
                    from_id=target.from_id,
                    relationship_type=target.relationship_type,
                    to_type=target.to_type,
                    to_id=target.to_id,
                    properties={"verified": True, "source": "replaced"},
                    evidence_refs=[{"source": "doc", "source_record_id": "second"}],
                    evidence_rationale="Replacement evidence.",
                )
            ],
            source="test",
            source_ref="replace",
        )
        rel = populated_instance.load_graph().get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
        )
        assert rel is not None
        assert rel.metadata.evidence is not None
        assert rel.metadata.evidence.evidence_refs[0].source_record_id == "second"
        assert rel.metadata.evidence.rationale == "Replacement evidence."

    def test_missing_source_evidence_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(ConfigError, match="Source artifact 'SRC-missing' not found"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        source_evidence=[
                            {
                                "source_artifact_id": "SRC-missing",
                                "chunk_id": "chunk-missing",
                            }
                        ],
                    )
                ],
                source="test",
                source_ref="missing_source",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_malformed_evidence_ref_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(DataValidationError, match="Invalid evidence_ref"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        evidence_refs=[{"source": "roadmap_doc"}],
                    )
                ],
                source="test",
                source_ref="malformed_evidence",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_malformed_source_evidence_does_not_commit_relationship(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(DataValidationError, match="Invalid source_evidence"):
            service_add_relationship_inputs(
                populated_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                        source_evidence=[{"source_artifact_id": "SRC-1"}],
                    )
                ],
                source="test",
                source_ref="malformed_source_evidence",
            )

        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1002",
            "Vehicle",
            "V-2024-ACCORD-SPORT",
            "fits",
        )
        assert rel is None

    def test_batch(self, populated_instance: CruxibleInstance) -> None:
        result = service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True},
                ),
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1001",
                    relationship="replaces",
                    to_type="Part",
                    to_id="BP-1002",
                    properties={"direction": "downgrade", "confidence": 0.8},
                ),
            ],
            source="test",
            source_ref="test_batch",
        )
        assert result.added == 2
        assert result.updated == 0

    def test_dedup_error(self, populated_instance: CruxibleInstance) -> None:
        edges = [
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1002",
                relationship="fits",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
            ),
            RelationshipInstance(
                from_type="Part",
                from_id="BP-1002",
                relationship="fits",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
            ),
        ]
        with pytest.raises(DataValidationError, match="duplicate in batch"):
            service_add_relationships(populated_instance, edges, source="test", source_ref="test")

    def test_source_provenance(self, populated_instance: CruxibleInstance) -> None:
        service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                )
            ],
            source="agent_review",
            source_ref="review-123",
        )
        graph = populated_instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1002", "Vehicle", "V-2024-ACCORD-SPORT", "fits")
        assert rel is not None
        prov = rel.metadata.provenance
        assert prov is not None
        assert prov.source == "agent_review"
        assert prov.source_ref == "review-123"

    def test_incremental_relationship_mutation_does_not_full_save(
        self, populated_instance: CruxibleInstance
    ) -> None:
        def fail_full_save(_self: SQLiteGraphRepository, _graph) -> None:
            raise AssertionError("service_add_relationships should not replace the full graph")

        with patch.object(SQLiteGraphRepository, "save_graph", fail_full_save):
            result = service_add_relationships(
                populated_instance,
                [
                    RelationshipInstance(
                        from_type="Part",
                        from_id="BP-1002",
                        relationship="fits",
                        to_type="Vehicle",
                        to_id="V-2024-ACCORD-SPORT",
                        properties={"verified": True},
                    )
                ],
                source="test",
                source_ref="incremental",
            )

        assert result.added == 1
        restarted = CruxibleInstance.load(populated_instance.root)
        assert (
            restarted.load_graph().get_relationship(
                "Part",
                "BP-1002",
                "Vehicle",
                "V-2024-ACCORD-SPORT",
                "fits",
            )
            is not None
        )
