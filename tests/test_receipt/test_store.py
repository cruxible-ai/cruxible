"""Tests for SQLite receipt storage."""

from datetime import datetime, timezone

import pytest

from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.receipt.types import Receipt


def _trace_timing() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {"started_at": now, "finished_at": now, "duration_ms": 0.0}


@pytest.fixture
def store() -> SQLiteReceiptStore:
    return SQLiteReceiptStore(":memory:")


@pytest.fixture
def sample_receipt() -> Receipt:
    builder = ReceiptBuilder(query_name="parts_for_vehicle", parameters={"vehicle_id": "V-1"})
    builder.record_entity_lookup(entity_type="Vehicle", entity_id="V-1")
    tid = builder.record_traversal(
        from_entity_type="Vehicle",
        from_entity_id="V-1",
        to_entity_type="Part",
        to_entity_id="P-1",
        relationship="fits",
        edge_props={"verified": True},
    )
    builder.record_filter(filter_spec={"verified": True}, passed=True, parent_id=tid)
    results = [{"entity_type": "Part", "entity_id": "P-1"}]
    builder.record_results(results)
    return builder.build(results)


class TestSQLiteReceiptStore:
    def test_save_and_get(self, store: SQLiteReceiptStore, sample_receipt: Receipt):
        receipt_id = store.save_receipt(sample_receipt)
        assert receipt_id == sample_receipt.receipt_id

        loaded = store.get_receipt(receipt_id)
        assert loaded is not None
        assert loaded.receipt_id == sample_receipt.receipt_id
        assert loaded.query_name == "parts_for_vehicle"
        assert loaded.parameters == {"vehicle_id": "V-1"}
        assert len(loaded.nodes) == len(sample_receipt.nodes)
        assert len(loaded.edges) == len(sample_receipt.edges)
        assert len(loaded.results) == len(sample_receipt.results)

    def test_get_nonexistent(self, store: SQLiteReceiptStore):
        assert store.get_receipt("RCP-nonexistent") is None

    def test_save_overwrites(self, store: SQLiteReceiptStore, sample_receipt: Receipt):
        store.save_receipt(sample_receipt)
        store.save_receipt(sample_receipt)
        loaded = store.get_receipt(sample_receipt.receipt_id)
        assert loaded is not None

    def test_list_receipts(self, store: SQLiteReceiptStore, sample_receipt: Receipt):
        store.save_receipt(sample_receipt)

        items = store.list_receipts()
        assert len(items) == 1
        assert items[0]["receipt_id"] == sample_receipt.receipt_id
        assert items[0]["query_name"] == "parts_for_vehicle"
        assert items[0]["parameters"] == {"vehicle_id": "V-1"}
        assert store.count_receipts() == 1

    def test_list_filter_by_query_name(self, store: SQLiteReceiptStore):
        b1 = ReceiptBuilder(query_name="query_a", parameters={})
        b2 = ReceiptBuilder(query_name="query_b", parameters={})
        store.save_receipt(b1.build(results=[]))
        store.save_receipt(b2.build(results=[]))

        items = store.list_receipts(query_name="query_a")
        assert len(items) == 1
        assert items[0]["query_name"] == "query_a"
        assert store.count_receipts(query_name="query_a") == 1

    def test_list_limit_and_offset(self, store: SQLiteReceiptStore):
        for i in range(5):
            b = ReceiptBuilder(query_name="q", parameters={"i": i})
            store.save_receipt(b.build(results=[]))

        items = store.list_receipts(limit=2)
        assert len(items) == 2

        all_items = store.list_receipts(limit=10)
        assert len(all_items) == 5

        offset_items = store.list_receipts(limit=2, offset=3)
        assert len(offset_items) == 2

    def test_delete_receipt(self, store: SQLiteReceiptStore, sample_receipt: Receipt):
        store.save_receipt(sample_receipt)
        assert store.delete_receipt(sample_receipt.receipt_id) is True
        assert store.get_receipt(sample_receipt.receipt_id) is None
        row = store._conn.execute(
            "SELECT COUNT(*) AS count FROM receipt_entities WHERE receipt_id = ?",
            (sample_receipt.receipt_id,),
        ).fetchone()
        assert row["count"] == 0

    def test_delete_nonexistent(self, store: SQLiteReceiptStore):
        assert store.delete_receipt("RCP-nonexistent") is False

    def test_list_empty(self, store: SQLiteReceiptStore):
        assert store.list_receipts() == []

    def test_receipt_preserves_dag_structure(
        self,
        store: SQLiteReceiptStore,
        sample_receipt: Receipt,
    ):
        store.save_receipt(sample_receipt)
        loaded = store.get_receipt(sample_receipt.receipt_id)

        node_types = {n.node_type for n in loaded.nodes}
        assert "query" in node_types
        assert "entity_lookup" in node_types
        assert "edge_traversal" in node_types
        assert "filter_applied" in node_types
        assert "result" in node_types

        edge_types = {e.edge_type for e in loaded.edges}
        assert "consulted" in edge_types
        assert "traversed" in edge_types
        assert "filtered" in edge_types
        assert "produced" in edge_types

    def test_get_receipts_for_entity(self, store: SQLiteReceiptStore, sample_receipt: Receipt):
        store.save_receipt(sample_receipt)
        ids = store.get_receipts_for_entity("Part", "P-1")
        assert sample_receipt.receipt_id in ids

    def test_receipt_entity_index_replaces_old_rows(self, store: SQLiteReceiptStore):
        builder = ReceiptBuilder(query_name="q", parameters={})
        builder.record_entity_lookup(entity_type="Vehicle", entity_id="V-1")
        first = builder.build(results=[])
        receipt_id = store.save_receipt(first)
        assert store.get_receipts_for_entity("Vehicle", "V-1") == [receipt_id]

        updated = first.model_copy()
        updated.nodes = []
        store.save_receipt(updated)
        assert store.get_receipts_for_entity("Vehicle", "V-1") == []

    def test_file_persistence(self, tmp_path):
        db_path = tmp_path / "test.db"
        store1 = SQLiteReceiptStore(db_path)

        b = ReceiptBuilder(query_name="q", parameters={"x": 1})
        receipt = b.build(results=[])
        store1.save_receipt(receipt)
        store1.close()

        store2 = SQLiteReceiptStore(db_path)
        loaded = store2.get_receipt(receipt.receipt_id)
        assert loaded is not None
        assert loaded.query_name == "q"
        store2.close()

    def test_migration_adds_operation_type(self, tmp_path):
        """Open store, close, reopen — operation_type column exists."""
        db_path = tmp_path / "migrate.db"
        store1 = SQLiteReceiptStore(db_path)
        b = ReceiptBuilder(query_name="q", parameters={})
        store1.save_receipt(b.build(results=[]))
        store1.close()

        store2 = SQLiteReceiptStore(db_path)
        items = store2.list_receipts()
        assert items[0]["operation_type"] == "query"
        store2.close()

    def test_save_stores_operation_type(self, store: SQLiteReceiptStore):
        b = ReceiptBuilder(operation_type="add_entity", parameters={"count": 1})
        b.mark_committed()
        receipt = b.build()
        store.save_receipt(receipt)
        items = store.list_receipts()
        assert items[0]["operation_type"] == "add_entity"

    def test_list_filter_by_operation_type(self, store: SQLiteReceiptStore):
        b1 = ReceiptBuilder(query_name="q", parameters={})
        b2 = ReceiptBuilder(operation_type="add_entity", parameters={})
        b2.mark_committed()
        store.save_receipt(b1.build(results=[]))
        store.save_receipt(b2.build())

        items = store.list_receipts(operation_type="add_entity")
        assert len(items) == 1
        assert items[0]["operation_type"] == "add_entity"

    def test_count_filter_by_operation_type(self, store: SQLiteReceiptStore):
        b1 = ReceiptBuilder(query_name="q", parameters={})
        b2 = ReceiptBuilder(operation_type="add_entity", parameters={})
        b2.mark_committed()
        store.save_receipt(b1.build(results=[]))
        store.save_receipt(b2.build())

        assert store.count_receipts(operation_type="add_entity") == 1
        assert store.count_receipts(operation_type="query") == 1

    def test_combined_filters(self, store: SQLiteReceiptStore):
        b1 = ReceiptBuilder(query_name="q1", parameters={})
        b2 = ReceiptBuilder(query_name="q1", parameters={}, operation_type="add_entity")
        b2.mark_committed()
        store.save_receipt(b1.build(results=[]))
        store.save_receipt(b2.build())

        items = store.list_receipts(query_name="q1", operation_type="add_entity")
        assert len(items) == 1

    def test_old_receipts_default_to_query(
        self, store: SQLiteReceiptStore, sample_receipt: Receipt
    ):
        store.save_receipt(sample_receipt)
        loaded = store.get_receipt(sample_receipt.receipt_id)
        assert loaded.operation_type == "query"

    def test_save_and_get_trace(self, store: SQLiteReceiptStore):
        trace = ExecutionTrace(
            workflow_name="evaluate_promo",
            step_id="lift",
            provider_name="lift_predictor",
            provider_version="1.2.0",
            provider_ref="tests.support.workflow_test_providers.lift_predictor",
            runtime="python",
            deterministic=True,
            side_effects=False,
            artifact_name="promo_model",
            artifact_sha256="abc123",
            input_payload={"sku": "SKU-123"},
            output_payload={"predicted_lift_pct": 0.12},
            **_trace_timing(),
        )

        trace_id = store.save_trace(trace)
        loaded = store.get_trace(trace_id)

        assert loaded is not None
        assert loaded.trace_id == trace.trace_id
        assert loaded.provider_name == "lift_predictor"
        assert loaded.output_payload["predicted_lift_pct"] == 0.12

    def test_list_traces(self, store: SQLiteReceiptStore):
        trace_a = ExecutionTrace(
            workflow_name="wf_a",
            step_id="step",
            provider_name="provider_a",
            provider_version="1.0.0",
            provider_ref="tests.support.workflow_test_providers.lift_predictor",
            runtime="python",
            deterministic=True,
            side_effects=False,
            **_trace_timing(),
        )
        trace_b = ExecutionTrace(
            workflow_name="wf_b",
            step_id="step",
            provider_name="provider_b",
            provider_version="1.0.0",
            provider_ref="tests.support.workflow_test_providers.margin_calculator",
            runtime="python",
            deterministic=True,
            side_effects=False,
            **_trace_timing(),
        )
        store.save_trace(trace_a)
        store.save_trace(trace_b)

        listed = store.list_traces(workflow_name="wf_a")
        assert len(listed) == 1
        assert listed[0]["provider_name"] == "provider_a"
