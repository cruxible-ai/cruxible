"""Retained query-service donor oracles after DP-0C config-authority deletion."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

import cruxible_core.service.queries as queries_module
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import (
    ConfigError,
    EntityTypeNotFoundError,
    ReceiptNotFoundError,
    RelationshipAmbiguityError,
    RelationshipNotFoundError,
    TraceNotFoundError,
)
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.provenance import RelationshipProvenance
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
    mint_claim_id,
)
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutations import (
    service_add_entities,
    service_add_entity_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.queries import (
    _warn_on_dropped_read,
    service_get_entity,
    service_get_entity_change_history,
    service_get_receipt,
    service_get_relationship,
    service_get_relationship_lineage,
    service_get_trace,
    service_inspect_entity,
    service_list,
    service_list_traces,
    service_query,
    service_query_inline_surface,
    service_sample,
    service_stats,
)
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    EntityWriteInput,
    QueryServiceResult,
)

STATUS_HISTORY_YAML = """\
version: '1.0'
name: status_history_demo
entity_types:
  Task:
    properties:
      task_id: {type: string, primary_key: true}
      status:
        type: string
        enum: [planned, active, closed]
      title: {type: string, optional: true}
  Note:
    properties:
      note_id: {type: string, primary_key: true}
      body: {type: string}
relationships: []
"""


def _status_history_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(STATUS_HISTORY_YAML)
    return CruxibleInstance.init(tmp_path, "config.yaml")


def _history_changes(result):
    return [
        (
            item.change_kind,
            [
                (change.property, change.from_value, change.to_value)
                for change in item.property_changes
            ],
        )
        for item in result.items
    ]


# ---------------------------------------------------------------------------
# service_sample
# ---------------------------------------------------------------------------


class TestSample:
    def test_entities(self, populated_instance: CruxibleInstance) -> None:
        result = service_sample(populated_instance, "Vehicle", limit=10)
        assert len(result.items) == 2  # 2 vehicles in populated graph
        assert all(e.entity_type == "Vehicle" for e in result.items)
        assert result.total == 2
        assert result.truncated is False

    def test_sample_reports_true_total_and_truncation(
        self, populated_instance: CruxibleInstance
    ) -> None:
        result = service_sample(populated_instance, "Vehicle", limit=1)
        assert len(result.items) == 1
        assert result.total == 2  # TRUE stored count, not the sampled count
        assert result.truncated is True

    def test_bad_type(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(EntityTypeNotFoundError) as exc_info:
            service_sample(populated_instance, "NonexistentType")

        assert exc_info.value.entity_type == "NonexistentType"
        assert exc_info.value.known_entity_types == ["Part", "Vehicle"]


# ---------------------------------------------------------------------------
# service_get_entity
# ---------------------------------------------------------------------------


class TestGetEntity:
    def test_found(self, populated_instance: CruxibleInstance) -> None:
        entity = service_get_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX")
        assert entity is not None
        assert entity.entity_id == "V-2024-CIVIC-EX"
        assert entity.properties["make"] == "Honda"

    def test_get_entity_exposes_derived_primary_key(
        self, initialized_instance: CruxibleInstance
    ) -> None:
        service_add_entities(
            initialized_instance,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-1",
                    properties={
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                )
            ],
        )
        raw = initialized_instance.load_graph().get_entity("Vehicle", "V-1")
        assert raw is not None
        assert "vehicle_id" not in raw.properties

        entity = service_get_entity(initialized_instance, "Vehicle", "V-1")
        assert entity is not None
        assert entity.properties["vehicle_id"] == "V-1"

    def test_get_entity_preserves_metadata(self, initialized_instance: CruxibleInstance) -> None:
        service_add_entities(
            initialized_instance,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-1",
                    properties={
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                    metadata={"source": "fixture"},
                )
            ],
        )

        entity = service_get_entity(initialized_instance, "Vehicle", "V-1")

        assert entity is not None
        # Free-form metadata is carried in the typed envelope's `extra` slot.
        assert entity.metadata.extra == {"source": "fixture"}

    def test_not_found(self, populated_instance: CruxibleInstance) -> None:
        entity = service_get_entity(populated_instance, "Vehicle", "NONEXISTENT")
        assert entity is None

    def test_unknown_entity_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(EntityTypeNotFoundError) as exc_info:
            service_get_entity(populated_instance, "NonexistentType", "ANY")

        assert exc_info.value.entity_type == "NonexistentType"
        assert exc_info.value.known_entity_types == ["Part", "Vehicle"]

    def test_inspect_entity_returns_neighbors(self, populated_instance: CruxibleInstance) -> None:
        graph = populated_instance.load_graph()
        graph.update_entity_metadata("Vehicle", "V-2024-CIVIC-EX", {"source": "fixture"})
        graph.update_relationship_state(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
            metadata=RelationshipMetadata(
                provenance=RelationshipProvenance(
                    source="ingest",
                    source_ref="fixture",
                )
            ),
        )
        populated_instance.save_graph(graph)

        result = service_inspect_entity(populated_instance, "Vehicle", "V-2024-CIVIC-EX")

        assert result.found is True
        assert result.properties["vehicle_id"] == "V-2024-CIVIC-EX"
        # The read surface serializes the typed envelope to its flat dict shape:
        # free-form keys are nested under "extra".
        assert result.metadata == {"extra": {"source": "fixture"}}
        assert result.total_neighbors == 2
        assert {neighbor.relationship_type for neighbor in result.neighbors} == {"fits"}
        assert {neighbor.direction for neighbor in result.neighbors} == {"incoming"}
        metadata_rows = [neighbor.metadata for neighbor in result.neighbors if neighbor.metadata]
        assert any(
            metadata.get("provenance", {}).get("source") == "ingest" for metadata in metadata_rows
        )

    def test_inspect_entity_not_found(self, populated_instance: CruxibleInstance) -> None:
        result = service_inspect_entity(populated_instance, "Vehicle", "MISSING")
        assert result.found is False
        assert result.neighbors == []

    def test_inspect_unknown_entity_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(EntityTypeNotFoundError) as exc_info:
            service_inspect_entity(populated_instance, "NonexistentType", "ANY")

        assert exc_info.value.entity_type == "NonexistentType"
        assert exc_info.value.known_entity_types == ["Part", "Vehicle"]

    def test_inspect_unknown_relationship_filter_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(RelationshipNotFoundError) as exc_info:
            service_inspect_entity(
                populated_instance,
                "Vehicle",
                "V-2024-CIVIC-EX",
                relationship_type="missing_relationship",
            )

        assert exc_info.value.relationship_name == "missing_relationship"


# ---------------------------------------------------------------------------
# service_get_entity_change_history
# ---------------------------------------------------------------------------


class TestEntityChangeHistory:
    def test_entity_specific_change_history_from_mutation_receipts(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-1",
                    properties={"status": "planned", "title": "First"},
                )
            ],
        )
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-1",
                    properties={"status": "active"},
                )
            ],
        )
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-1",
                    properties={"title": "Renamed"},
                )
            ],
        )
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-1",
                    properties={"status": "active"},
                )
            ],
        )

        history = service_get_entity_change_history(instance, "Task", entity_id="T-1")

        assert history.total == 3
        assert history.legacy_entity_write_count == 0
        assert _history_changes(history) == [
            ("updated", [("title", "First", "Renamed")]),
            ("updated", [("status", "planned", "active")]),
            (
                "created",
                [
                    ("status", None, "planned"),
                    ("title", None, "First"),
                ],
            ),
        ]
        assert {item.operation_type for item in history.items} == {"add_entity"}
        assert all(item.receipt_id.startswith("RCP-") for item in history.items)

    def test_type_wide_change_history_and_pagination(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-1",
                    properties={"status": "planned"},
                ),
                EntityInstance(
                    entity_type="Task",
                    entity_id="T-2",
                    properties={"status": "active"},
                ),
            ],
        )

        history = service_get_entity_change_history(instance, "Task", limit=1, offset=1)

        assert history.total == 2
        assert len(history.items) == 1
        assert history.items[0].change_kind == "created"
        assert history.items[0].entity_id in {"T-1", "T-2"}

    def test_batch_direct_write_records_property_changes(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Task",
                        entity_id="T-1",
                        properties={"status": "planned"},
                    )
                ]
            ),
        )
        service_batch_direct_write(
            instance,
            BatchDirectWriteInput(
                entities=[
                    EntityWriteInput(
                        entity_type="Task",
                        entity_id="T-1",
                        properties={"status": "closed"},
                    )
                ]
            ),
        )

        history = service_get_entity_change_history(instance, "Task", entity_id="T-1")

        assert _history_changes(history) == [
            ("updated", [("status", "planned", "closed")]),
            ("created", [("status", None, "planned")]),
        ]
        assert {item.operation_type for item in history.items} == {"batch_direct_write"}

    def test_legacy_entity_write_receipts_are_not_inferred(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)
        builder = ReceiptBuilder(operation_type="add_entity")
        builder.record_entity_write("Task", "T-legacy", is_update=False)
        receipt = builder.build()
        with instance.write_transaction() as uow:
            uow.receipts.save_receipt(receipt)

        history = service_get_entity_change_history(instance, "Task", entity_id="T-legacy")

        assert history.items == []
        assert history.total == 0
        assert history.legacy_entity_write_count == 1
        assert history.warnings == ["1 legacy entity write(s) lacked property change detail"]

    def test_entity_type_without_status_is_supported(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Note",
                    entity_id="N-1",
                    properties={"body": "Initial"},
                )
            ],
        )
        service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Note",
                    entity_id="N-1",
                    properties={"body": "Updated"},
                )
            ],
        )

        history = service_get_entity_change_history(instance, "Note", entity_id="N-1")

        assert _history_changes(history) == [
            ("updated", [("body", "Initial", "Updated")]),
            ("created", [("body", None, "Initial")]),
        ]

    def test_unknown_entity_type_raises_typed_error(self, tmp_path: Path) -> None:
        instance = _status_history_instance(tmp_path)

        with pytest.raises(EntityTypeNotFoundError):
            service_get_entity_change_history(instance, "Missing")


# ---------------------------------------------------------------------------
# service_get_relationship
# ---------------------------------------------------------------------------


class TestGetRelationship:
    def test_found(self, populated_instance: CruxibleInstance) -> None:
        rel = service_get_relationship(
            populated_instance,
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
        )
        assert rel is not None
        assert isinstance(rel, RelationshipInstance)
        assert rel.relationship_type == "fits"

    def test_ambiguous(self, populated_instance: CruxibleInstance) -> None:
        """Multi-edge without edge_key raises RelationshipAmbiguityError."""
        graph = populated_instance.load_graph()
        # Add a second fits edge between same endpoints
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1001",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
                properties={"verified": False, "source": "duplicate"},
            )
        )
        populated_instance.save_graph(graph)

        with pytest.raises(RelationshipAmbiguityError):
            service_get_relationship(
                populated_instance,
                from_type="Part",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
            )

    def test_unknown_endpoint_entity_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(EntityTypeNotFoundError) as exc_info:
            service_get_relationship(
                populated_instance,
                from_type="NonexistentType",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
            )

        assert exc_info.value.entity_type == "NonexistentType"
        assert exc_info.value.known_entity_types == ["Part", "Vehicle"]

    def test_unknown_relationship_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(RelationshipNotFoundError) as exc_info:
            service_get_relationship(
                populated_instance,
                from_type="Part",
                from_id="BP-1001",
                relationship_type="missing_relationship",
                to_type="Vehicle",
                to_id="V-2024-CIVIC-EX",
            )

        assert exc_info.value.relationship_name == "missing_relationship"

    def test_lineage_warns_on_missing_provenance(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        lineage = service_get_relationship_lineage(
            populated_instance,
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-CIVIC-EX",
        )

        assert lineage.found is True
        assert lineage.relationship is not None
        assertion = lineage.relationship.metadata.assertion
        assert assertion.review.status == "unreviewed"
        assert assertion.lifecycle.status == "active"
        assert lineage.warnings == ["missing_provenance"]

    def test_lineage_warns_when_relationship_not_found(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        lineage = service_get_relationship_lineage(
            populated_instance,
            from_type="Part",
            from_id="BP-1001",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-NOT-FOUND",
        )

        assert lineage.found is False
        assert lineage.relationship is None
        assert lineage.warnings == ["relationship_not_found"]

    def test_lineage_unknown_endpoint_entity_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(EntityTypeNotFoundError):
            service_get_relationship_lineage(
                populated_instance,
                from_type="Part",
                from_id="BP-1001",
                relationship_type="fits",
                to_type="NonexistentType",
                to_id="V-NOT-FOUND",
            )

    def test_lineage_warns_on_non_group_provenance(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1002",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
                properties={"verified": True},
                metadata=RelationshipMetadata(
                    provenance=RelationshipProvenance(
                        source="workflow_apply",
                        source_ref="workflow:canonical-fitment",
                    )
                ),
            )
        )
        populated_instance.save_graph(graph)

        lineage = service_get_relationship_lineage(
            populated_instance,
            from_type="Part",
            from_id="BP-1002",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-ACCORD-SPORT",
        )

        assert lineage.found is True
        assert lineage.relationship is not None
        assert lineage.provenance == {
            "source": "workflow_apply",
            "source_ref": "workflow:canonical-fitment",
        }
        assertion = lineage.relationship.metadata.assertion
        assert assertion.review.status == "unreviewed"
        assert lineage.group is None
        assert lineage.warnings == ["non_group_provenance"]

    def test_lineage_warns_when_group_provenance_points_to_missing_group(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1002",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
                properties={"verified": True},
                metadata=RelationshipMetadata(
                    provenance=RelationshipProvenance(
                        source="group_resolve",
                        source_ref="group:GRP-missing",
                    )
                ),
            )
        )
        populated_instance.save_graph(graph)

        lineage = service_get_relationship_lineage(
            populated_instance,
            from_type="Part",
            from_id="BP-1002",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-2024-ACCORD-SPORT",
        )

        assert lineage.found is True
        assert lineage.relationship is not None
        assert lineage.provenance == {
            "source": "group_resolve",
            "source_ref": "group:GRP-missing",
        }
        assert lineage.group is None
        assert lineage.warnings == ["missing_group"]


# ---------------------------------------------------------------------------
# service_get_receipt
# ---------------------------------------------------------------------------


class TestGetReceipt:
    def test_found(self, populated_instance: CruxibleInstance) -> None:
        query_result = service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert query_result.receipt_id is not None
        receipt = service_get_receipt(populated_instance, query_result.receipt_id)
        assert receipt.receipt_id == query_result.receipt_id
        assert query_result.param_hints is not None
        assert query_result.param_hints.primary_key == "vehicle_id"
        assert "V-2024-CIVIC-EX" in query_result.param_hints.example_ids

    def test_not_found(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ReceiptNotFoundError):
            service_get_receipt(populated_instance, "nonexistent-receipt")

    def test_store_lifecycle(self, populated_instance: CruxibleInstance) -> None:
        """Verify store closes even on error."""
        with pytest.raises(ReceiptNotFoundError):
            service_get_receipt(populated_instance, "bad-id")
        # Should be able to open store again
        store = populated_instance.get_receipt_store()
        store.close()


# ---------------------------------------------------------------------------
# service_get_trace / service_list_traces
# ---------------------------------------------------------------------------


def _trace(
    *,
    trace_id: str,
    workflow_name: str = "load_assets",
    provider_name: str = "asset_loader",
) -> ExecutionTrace:
    started_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ExecutionTrace(
        trace_id=trace_id,
        workflow_name=workflow_name,
        step_id="load",
        provider_name=provider_name,
        provider_version="1.0.0",
        provider_ref="tests.support.workflow_test_providers.asset_loader",
        runtime="python",
        deterministic=True,
        side_effects=False,
        input_payload={"source": "fixture"},
        output_payload={"rows": 2},
        started_at=started_at,
        finished_at=started_at,
        duration_ms=0.0,
    )


class TestTraceReads:
    def test_get_trace_found(self, populated_instance: CruxibleInstance) -> None:
        trace = _trace(trace_id="TRC-service-001")
        with populated_instance.write_transaction() as uow:
            uow.receipts.save_trace(trace)

        loaded = service_get_trace(populated_instance, trace.trace_id)

        assert loaded.trace_id == trace.trace_id
        assert loaded.output_payload["rows"] == 2

    def test_get_trace_returns_large_payload_preview_by_default(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        large_payload = {"body": "x" * 40000, "source": "fixture"}
        trace = _trace(trace_id="TRC-service-large")
        trace.input_payload = large_payload
        trace.output_payload = large_payload
        with populated_instance.write_transaction() as uow:
            uow.receipts.save_trace(trace)

        preview = service_get_trace(populated_instance, trace.trace_id)

        assert preview.input_payload != large_payload
        assert preview.input_payload_metadata is not None
        assert preview.input_payload_metadata.retention == "preview"
        assert preview.input_payload_metadata.stored_inline is False

    def test_get_trace_not_found(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(TraceNotFoundError):
            service_get_trace(populated_instance, "TRC-missing")

    def test_list_traces_with_filters(self, populated_instance: CruxibleInstance) -> None:
        with populated_instance.write_transaction() as uow:
            uow.receipts.save_trace(_trace(trace_id="TRC-a", workflow_name="wf_a"))
            uow.receipts.save_trace(_trace(trace_id="TRC-b", workflow_name="wf_b"))

        result = service_list_traces(populated_instance, workflow_name="wf_a", limit=10)

        assert result.total == 1
        assert result.items[0]["trace_id"] == "TRC-a"
        assert result.limit == 10
        assert result.offset == 0
        assert result.truncated is False
        assert result.read_revision == populated_instance.get_read_revision()


# ---------------------------------------------------------------------------
# service_list
# ---------------------------------------------------------------------------


class TestList:
    def test_entities(self, populated_instance: CruxibleInstance) -> None:
        result = service_list(populated_instance, "entities", entity_type="Vehicle")
        assert result.total == 2
        assert len(result.items) == 2

    def test_entities_unknown_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(EntityTypeNotFoundError) as exc_info:
            service_list(populated_instance, "entities", entity_type="NonexistentType")

        assert exc_info.value.entity_type == "NonexistentType"
        assert exc_info.value.known_entity_types == ["Part", "Vehicle"]

    def test_entities_property_filter(self, populated_instance: CruxibleInstance) -> None:
        result = service_list(
            populated_instance,
            "entities",
            entity_type="Vehicle",
            property_filter={"model": "Civic"},
        )
        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].entity_id == "V-2024-CIVIC-EX"

    def test_entities_primary_key_property_filter(
        self, initialized_instance: CruxibleInstance
    ) -> None:
        service_add_entities(
            initialized_instance,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-1",
                    properties={
                        "vehicle_id": "V-1",
                        "year": 2024,
                        "make": "Honda",
                        "model": "Civic",
                    },
                )
            ],
        )

        result = service_list(
            initialized_instance,
            "entities",
            entity_type="Vehicle",
            property_filter={"vehicle_id": "V-1"},
        )

        assert result.total == 1
        assert result.items[0].properties["vehicle_id"] == "V-1"

    def test_entities_where_filters_with_query_predicates(
        self, populated_instance: CruxibleInstance
    ) -> None:
        eq_result = service_list(
            populated_instance,
            "entities",
            entity_type="Part",
            where={"category": {"eq": "brakes"}},
        )
        assert eq_result.total == 2

        contains_result = service_list(
            populated_instance,
            "entities",
            entity_type="Part",
            where={"name": {"contains": "Performance"}},
        )
        assert contains_result.total == 1
        assert contains_result.items[0].entity_id == "BP-1002"

        in_result = service_list(
            populated_instance,
            "entities",
            entity_type="Vehicle",
            where={"model": {"in": ["Civic", "Missing"]}},
        )
        assert in_result.total == 1
        assert in_result.items[0].entity_id == "V-2024-CIVIC-EX"

    def test_entities_where_rejects_unknown_fields_and_property_filter_mix(
        self, populated_instance: CruxibleInstance
    ) -> None:
        with pytest.raises(ConfigError, match="Unknown where field"):
            service_list(
                populated_instance,
                "entities",
                entity_type="Part",
                where={"unknown": {"eq": "value"}},
            )

        with pytest.raises(ConfigError, match="mutually exclusive"):
            service_list(
                populated_instance,
                "entities",
                entity_type="Part",
                property_filter={"category": "brakes"},
                where={"name": {"contains": "Brake"}},
            )

    def test_edges(self, populated_instance: CruxibleInstance) -> None:
        result = service_list(populated_instance, "edges")
        assert result.total >= 3  # 3 fits + 1 replaces in populated graph

    def test_edges_unknown_relationship_type_raises_typed_error(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with pytest.raises(RelationshipNotFoundError) as exc_info:
            service_list(
                populated_instance,
                "edges",
                relationship_type="missing_relationship",
            )

        assert exc_info.value.relationship_name == "missing_relationship"

    def test_edges_property_filter(self, populated_instance: CruxibleInstance) -> None:
        result = service_list(
            populated_instance,
            "edges",
            relationship_type="fits",
            property_filter={"source": "catalog"},
        )
        assert result.total == 2
        assert len(result.items) == 2
        assert all(edge["properties"]["source"] == "catalog" for edge in result.items)

    def test_edges_where_filters_with_query_predicates(
        self, populated_instance: CruxibleInstance
    ) -> None:
        contains_result = service_list(
            populated_instance,
            "edges",
            relationship_type="fits",
            where={"source": {"contains": "user"}},
        )
        assert contains_result.total == 1
        assert contains_result.items[0]["from_id"] == "BP-1002"

        in_result = service_list(
            populated_instance,
            "edges",
            relationship_type="replaces",
            where={"direction": {"in": ["upgrade", "equivalent"]}},
        )
        assert in_result.total == 1
        assert in_result.items[0]["relationship_type"] == "replaces"

    def test_edges_where_rejects_unknown_fields(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Unknown where field"):
            service_list(
                populated_instance,
                "edges",
                relationship_type="fits",
                where={"confidence": {"eq": 0.95}},
            )

    def test_edges_list_keeps_rejected_stored_edges_visible(
        self, populated_instance: CruxibleInstance
    ) -> None:
        graph = populated_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                claim_id=mint_claim_id(),
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1002",
                to_type="Vehicle",
                to_id="V-2024-ACCORD-SPORT",
                properties={"verified": True, "source": "catalog"},
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(status="rejected")
                    )
                ),
            )
        )
        populated_instance.save_graph(graph)

        result = service_list(
            populated_instance,
            "edges",
            relationship_type="fits",
            where={"source": {"eq": "catalog"}},
        )

        assert result.total == 3
        assert any(edge["from_id"] == "BP-1002" for edge in result.items)

    def test_receipts(self, populated_instance: CruxibleInstance) -> None:
        # Create a receipt first
        service_query(
            populated_instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        result = service_list(populated_instance, "receipts")
        assert result.total >= 1

    def test_entities_requires_type(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="entity_type is required"):
            service_list(populated_instance, "entities")


class TestStats:
    def test_returns_grouped_counts(self, populated_instance: CruxibleInstance) -> None:
        result = service_stats(populated_instance)

        assert result.entity_count == 4
        assert result.edge_count == 4
        assert result.entity_counts["Vehicle"] == 2
        assert result.entity_counts["Part"] == 2
        assert result.relationship_counts["fits"] == 3
        assert result.relationship_counts["replaces"] == 1
        assert result.read_revision == populated_instance.get_read_revision()

    def test_property_only_update_advances_read_revision(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        before = service_stats(populated_instance)

        result = service_add_entity_inputs(
            populated_instance,
            [
                EntityWriteInput(
                    entity_type="Vehicle",
                    entity_id="V-2024-CIVIC-EX",
                    properties={"year": 2025},
                )
            ],
        )
        after = service_stats(populated_instance)

        assert result.added == 0
        assert result.updated == 1
        assert after.entity_count == before.entity_count
        assert after.edge_count == before.edge_count
        assert after.read_revision == before.read_revision + 1

    def test_returns_status_counts_for_enum_backed_status_properties(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            'version: "1.0"\n'
            "name: status_counts\n"
            "enums:\n"
            "  work_status:\n"
            "    values: [planned, active, closed]\n"
            "entity_types:\n"
            "  WorkItem:\n"
            "    properties:\n"
            "      work_item_id: {type: string, primary_key: true}\n"
            "      status: {type: string, enum_ref: work_status}\n"
            "  Risk:\n"
            "    properties:\n"
            "      risk_id: {type: string, primary_key: true}\n"
            "      status: {type: string, enum: [open, mitigated]}\n"
            "  Note:\n"
            "    properties:\n"
            "      note_id: {type: string, primary_key: true}\n"
            "      status: {type: string}\n"
            "relationships: []\n"
        )
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        graph = instance.load_graph()
        graph.add_entity(
            EntityInstance(
                entity_type="WorkItem",
                entity_id="WI-1",
                properties={"work_item_id": "WI-1", "status": "planned"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="WorkItem",
                entity_id="WI-2",
                properties={"work_item_id": "WI-2", "status": "active"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="WorkItem",
                entity_id="WI-3",
                properties={"work_item_id": "WI-3", "status": "planned"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Risk",
                entity_id="R-1",
                properties={"risk_id": "R-1", "status": "open"},
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Note",
                entity_id="N-1",
                properties={"note_id": "N-1", "status": "draft"},
            )
        )
        instance.save_graph(graph)

        result = service_stats(instance)

        assert result.status_counts == {
            "WorkItem": {"planned": 2, "active": 1, "closed": 0},
            "Risk": {"open": 1, "mitigated": 0},
        }

    def test_invalid_resource(self, populated_instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Unknown resource"):
            service_list(populated_instance, "bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# read-pipeline drop detection (diagnostic invariant)
# ---------------------------------------------------------------------------


def _drop_warnings(events: list[dict]) -> list[dict]:
    return [
        event
        for event in events
        if event.get("event") == "read_pipeline_drop" and event.get("log_level") == "warning"
    ]


def _fake_query_result(total: int, items: list, *, truncation_reasons: list[str] | None = None):
    return QueryServiceResult(
        items=items,
        receipt_id=None,
        receipt=None,
        total=total,
        limit=None,
        truncated=bool(truncation_reasons),
        steps_executed=0,
        truncation_reasons=list(truncation_reasons or []),
    )


class TestReadPipelineDropDetection:
    """The guard warns loudly when a read drops rows it should have returned."""

    def test_guard_warns_on_total_with_empty_items(self) -> None:
        with structlog.testing.capture_logs() as events:
            _warn_on_dropped_read(resource="query:foo", total=8, returned=0)
        warnings = _drop_warnings(events)
        assert len(warnings) == 1
        assert warnings[0]["resource"] == "query:foo"
        assert warnings[0]["total"] == 8
        assert warnings[0]["returned"] == 0

    def test_guard_silent_on_total_zero(self) -> None:
        with structlog.testing.capture_logs() as events:
            _warn_on_dropped_read(resource="query:foo", total=0, returned=0)
        assert _drop_warnings(events) == []

    def test_guard_silent_on_normal_result(self) -> None:
        with structlog.testing.capture_logs() as events:
            _warn_on_dropped_read(resource="query:foo", total=3, returned=3)
        assert _drop_warnings(events) == []

    def test_guard_silent_when_offset_past_total(self) -> None:
        # Paging beyond the end legitimately yields an empty page.
        with structlog.testing.capture_logs() as events:
            _warn_on_dropped_read(resource="query:foo", total=5, returned=0, offset=5)
        assert _drop_warnings(events) == []

    def test_guard_silent_when_truncation_reason_explains_shortfall(self) -> None:
        with structlog.testing.capture_logs() as events:
            _warn_on_dropped_read(
                resource="query:foo",
                total=8,
                returned=0,
                truncation_reasons=["response_limit"],
            )
        assert _drop_warnings(events) == []

    def test_query_surface_warns_when_items_dropped(
        self,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the anomaly: a result that reports rows but returns none.
        monkeypatch.setattr(
            queries_module,
            "_evaluate_inline_query_result",
            lambda *args, **kwargs: _fake_query_result(8, []),
        )
        with structlog.testing.capture_logs() as events:
            result = service_query_inline_surface(
                populated_instance,
                {
                    "name": "brake_parts",
                    "mode": "collection",
                    "returns": "Part",
                    "result_shape": "entity",
                },
                {},
            )
        assert result.total == 8
        assert result.items == []
        warnings = _drop_warnings(events)
        assert len(warnings) == 1
        assert warnings[0]["resource"] == "query_inline:brake_parts"
        assert warnings[0]["total"] == 8
        assert warnings[0]["returned"] == 0

    def test_query_surface_silent_on_normal_result(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with structlog.testing.capture_logs() as events:
            result = service_query_inline_surface(
                populated_instance,
                {
                    "name": "brake_parts",
                    "mode": "collection",
                    "returns": "Part",
                    "result_shape": "entity",
                    "where": {"result.properties.category": {"eq": "brakes"}},
                },
                {},
            )
        assert result.total == 2
        assert len(result.items) == 2
        assert _drop_warnings(events) == []

    def test_query_surface_silent_on_legitimate_empty(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with structlog.testing.capture_logs() as events:
            result = service_query_inline_surface(
                populated_instance,
                {
                    "name": "no_match",
                    "mode": "collection",
                    "returns": "Part",
                    "result_shape": "entity",
                    "where": {"result.properties.category": {"eq": "nonexistent"}},
                },
                {},
            )
        assert result.total == 0
        assert result.items == []
        assert _drop_warnings(events) == []

    def test_list_warns_when_items_dropped(
        self,
        populated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from cruxible_core.service.types import ListResult

        monkeypatch.setattr(
            queries_module,
            "_service_list_entities",
            lambda *args, **kwargs: ListResult(items=[], total=2),
        )
        with structlog.testing.capture_logs() as events:
            result = service_list(populated_instance, "entities", entity_type="Vehicle")
        assert result.total == 2
        assert result.items == []
        warnings = _drop_warnings(events)
        assert len(warnings) == 1
        assert warnings[0]["resource"] == "list:entities"
        assert warnings[0]["total"] == 2
        assert warnings[0]["returned"] == 0

    def test_list_silent_on_normal_result(
        self,
        populated_instance: CruxibleInstance,
    ) -> None:
        with structlog.testing.capture_logs() as events:
            result = service_list(populated_instance, "entities", entity_type="Vehicle")
        assert result.total == 2
        assert len(result.items) == 2
        assert _drop_warnings(events) == []

    def test_list_silent_on_legitimate_empty(
        self,
        initialized_instance: CruxibleInstance,
    ) -> None:
        with structlog.testing.capture_logs() as events:
            result = service_list(initialized_instance, "receipts")
        assert result.total == 0
        assert result.items == []
        assert _drop_warnings(events) == []
