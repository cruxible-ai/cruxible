"""Tests for the receipt system: builder, types, and engine integration."""

import pytest

from cruxible_core.config.schema import (
    CoreConfig,
    EntityTypeSchema,
    NamedQuerySchema,
    PropertySchema,
    RelationshipSchema,
    TraversalStep,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.query.engine import execute_query
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.serializer import to_json, to_markdown, to_mermaid
from cruxible_core.receipt.types import Receipt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> CoreConfig:
    return CoreConfig(
        name="test",
        entity_types={
            "Vehicle": EntityTypeSchema(
                properties={
                    "vehicle_id": PropertySchema(type="string", primary_key=True),
                    "make": PropertySchema(type="string"),
                }
            ),
            "Part": EntityTypeSchema(
                properties={
                    "part_number": PropertySchema(type="string", primary_key=True),
                    "name": PropertySchema(type="string"),
                    "category": PropertySchema(type="string"),
                }
            ),
        },
        relationships=[
            RelationshipSchema(
                name="fits",
                from_entity="Part",
                to_entity="Vehicle",
                properties={
                    "verified": PropertySchema(type="bool"),
                },
            ),
        ],
        named_queries={
            "parts_for_vehicle": NamedQuerySchema(
                description="Find parts that fit a vehicle",
                entry_point="Vehicle",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="incoming",
                        filter={"verified": True},
                    )
                ],
                returns="list[Part]",
            ),
            "vehicles_for_part": NamedQuerySchema(
                description="Find vehicles a part fits",
                entry_point="Part",
                traversal=[
                    TraversalStep(
                        relationship="fits",
                        direction="outgoing",
                    )
                ],
                returns="list[Vehicle]",
            ),
        },
    )


@pytest.fixture
def graph() -> EntityGraph:
    g = EntityGraph()

    g.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "make": "Honda"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="P-1",
            properties={"part_number": "P-1", "name": "Brake Pad", "category": "brakes"},
        )
    )
    g.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="P-2",
            properties={"part_number": "P-2", "name": "Rotor", "category": "brakes"},
        )
    )

    # P-1 fits V-1 (verified)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="P-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True},
        )
    )
    # P-2 fits V-1 (not verified)
    g.add_relationship(
        RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="P-2",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": False},
        )
    )

    return g


# ---------------------------------------------------------------------------
# ReceiptBuilder unit tests
# ---------------------------------------------------------------------------


class TestReceiptTypes:
    def test_receipt_defaults_operation_type_query(self):
        receipt = Receipt(nodes=[], edges=[])
        assert receipt.operation_type == "query"
        assert receipt.committed is False

    def test_receipt_accepts_mutation_operation_type(self):
        receipt = Receipt(nodes=[], edges=[], operation_type="add_entity", committed=False)
        assert receipt.operation_type == "add_entity"
        assert receipt.committed is False

    def test_receipt_backward_compat_deserialization(self):
        """Old JSON without operation_type/committed deserializes with defaults."""
        old_json = (
            '{"receipt_id":"RCP-old","query_name":"q","parameters":{},'
            '"nodes":[],"edges":[],"results":[],"created_at":"2025-01-01T00:00:00Z",'
            '"duration_ms":1.0}'
        )
        receipt = Receipt.model_validate_json(old_json)
        assert receipt.operation_type == "query"
        assert receipt.committed is False
        assert receipt.head_snapshot_id is None
        assert receipt.workflow_mode is None

    def test_receipt_query_name_optional(self):
        receipt = Receipt(nodes=[], edges=[])
        assert receipt.query_name == ""

    def test_receipt_parameters_optional(self):
        receipt = Receipt(nodes=[], edges=[])
        assert receipt.parameters == {}

    def test_receipt_results_optional(self):
        receipt = Receipt(nodes=[], edges=[])
        assert receipt.results == []


class TestReceiptBuilder:
    def test_root_node_created(self):
        builder = ReceiptBuilder(query_name="test_q", parameters={"a": 1})
        receipt = builder.build(results=[])

        assert len(receipt.nodes) == 1
        root = receipt.nodes[0]
        assert root.node_type == "query"
        assert root.detail["query_name"] == "test_q"
        assert root.detail["parameters"] == {"a": 1}

    def test_receipt_id_format(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        receipt = builder.build(results=[])
        assert receipt.receipt_id.startswith("RCP-")

    def test_duration_tracked(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        receipt = builder.build(results=[])
        assert receipt.duration_ms >= 0

    def test_record_entity_lookup(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        node_id = builder.record_entity_lookup(
            entity_type="Vehicle",
            entity_id="V-1",
        )
        receipt = builder.build(results=[])

        # Root + entity_lookup
        assert len(receipt.nodes) == 2
        lookup = receipt.nodes[1]
        assert lookup.node_id == node_id
        assert lookup.node_type == "entity_lookup"
        assert lookup.entity_type == "Vehicle"
        assert lookup.entity_id == "V-1"

        # Edge from root to lookup
        assert len(receipt.edges) == 1
        assert receipt.edges[0].from_node == builder.root_id
        assert receipt.edges[0].to_node == node_id
        assert receipt.edges[0].edge_type == "consulted"

    def test_workflow_root_and_plan_step(self):
        builder = ReceiptBuilder(
            query_name="evaluate_promo",
            parameters={"sku": "SKU-123"},
            operation_type="workflow",
        )
        step_id = builder.record_plan_step(
            "lift",
            "provider",
            detail={"provider_name": "lift_predictor", "trace_id": "TRC-123"},
        )
        receipt = builder.build(results=[{"output": {"decision": "approve"}}])

        assert receipt.operation_type == "workflow"
        assert receipt.nodes[0].node_type == "workflow"
        assert receipt.nodes[0].detail["workflow_name"] == "evaluate_promo"
        plan_step = next(node for node in receipt.nodes if node.node_id == step_id)
        assert plan_step.node_type == "plan_step"
        assert plan_step.detail["kind"] == "provider"
        assert plan_step.detail["trace_id"] == "TRC-123"

    def test_record_traversal(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        builder.record_traversal(
            from_entity_type="Vehicle",
            from_entity_id="V-1",
            to_entity_type="Part",
            to_entity_id="P-1",
            relationship="fits",
            edge_props={"verified": True},
        )
        receipt = builder.build(results=[])

        traversal = receipt.nodes[1]
        assert traversal.node_type == "edge_traversal"
        assert traversal.entity_type == "Part"
        assert traversal.entity_id == "P-1"
        assert traversal.relationship == "fits"
        assert traversal.detail["from_entity_type"] == "Vehicle"
        assert traversal.detail["edge_properties"] == {"verified": True}

        assert receipt.edges[0].edge_type == "traversed"

    def test_record_filter(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        parent = builder.record_traversal(
            from_entity_type="V",
            from_entity_id="1",
            to_entity_type="P",
            to_entity_id="2",
            relationship="fits",
            edge_props={},
        )
        node_id = builder.record_filter(
            filter_spec={"verified": True},
            passed=False,
            parent_id=parent,
        )
        receipt = builder.build(results=[])

        filt = receipt.nodes[2]
        assert filt.node_type == "filter_applied"
        assert filt.detail["passed"] is False

        filter_edge = [e for e in receipt.edges if e.to_node == node_id][0]
        assert filter_edge.from_node == parent
        assert filter_edge.edge_type == "filtered"

    def test_record_constraint(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        parent = builder.record_traversal(
            from_entity_type="V",
            from_entity_id="1",
            to_entity_type="P",
            to_entity_id="2",
            relationship="fits",
            edge_props={},
        )
        node_id = builder.record_constraint(
            constraint="target.vehicle_id == $vid",
            passed=True,
            entity_type="P",
            entity_id="2",
            parent_id=parent,
        )
        receipt = builder.build(results=[])

        constraint = receipt.nodes[2]
        assert constraint.node_type == "constraint_check"
        assert constraint.detail["constraint"] == "target.vehicle_id == $vid"
        assert constraint.detail["passed"] is True

        edge = [e for e in receipt.edges if e.to_node == node_id][0]
        assert edge.edge_type == "evaluated"

    def test_record_results(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        p1 = builder.record_entity_lookup(entity_type="P", entity_id="1")
        p2 = builder.record_entity_lookup(entity_type="P", entity_id="2")

        results = [{"id": "1"}, {"id": "2"}]
        node_id = builder.record_results(results, parent_ids=[p1, p2])
        receipt = builder.build(results=results)

        result_node = [n for n in receipt.nodes if n.node_type == "result"][0]
        assert result_node.detail["count"] == 2

        produced_edges = [e for e in receipt.edges if e.to_node == node_id]
        assert len(produced_edges) == 2
        assert all(e.edge_type == "produced" for e in produced_edges)

    def test_results_default_parent_is_root(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        node_id = builder.record_results(results=[])
        receipt = builder.build(results=[])

        produced_edges = [e for e in receipt.edges if e.to_node == node_id]
        assert len(produced_edges) == 1
        assert produced_edges[0].from_node == builder.root_id

    def test_mutation_builder_creates_mutation_root(self):
        builder = ReceiptBuilder(operation_type="add_entity", parameters={"count": 2})
        receipt = builder.build()
        root = receipt.nodes[0]
        assert root.node_type == "mutation"
        assert root.detail["operation_type"] == "add_entity"
        assert receipt.operation_type == "add_entity"

    def test_default_builder_creates_query_root(self):
        builder = ReceiptBuilder(query_name="q", parameters={"a": 1})
        receipt = builder.build(results=[])
        root = receipt.nodes[0]
        assert root.node_type == "query"
        assert receipt.operation_type == "query"

    def test_mutation_committed_default_false(self):
        builder = ReceiptBuilder(operation_type="add_entity")
        receipt = builder.build()
        assert receipt.committed is False

    def test_query_committed_default_false(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        receipt = builder.build(results=[])
        assert receipt.committed is False

    def test_workflow_mode_and_head_snapshot_are_typed_fields(self):
        builder = ReceiptBuilder(
            query_name="wf",
            parameters={},
            operation_type="workflow",
            head_snapshot_id="snap_123",
            workflow_mode="preview",
        )
        receipt = builder.build(results=[])
        assert receipt.head_snapshot_id == "snap_123"
        assert receipt.workflow_mode == "preview"
        assert "head_snapshot_id" not in receipt.nodes[0].detail

    def test_mark_committed(self):
        builder = ReceiptBuilder(operation_type="add_entity")
        builder.mark_committed()
        receipt = builder.build()
        assert receipt.committed is True

    def test_record_validation(self):
        builder = ReceiptBuilder(operation_type="add_entity")
        nid = builder.record_validation(passed=True, detail={"entity": "test"})
        receipt = builder.build()
        node = [n for n in receipt.nodes if n.node_id == nid][0]
        assert node.node_type == "validation"
        assert node.detail["passed"] is True
        edge = [e for e in receipt.edges if e.to_node == nid][0]
        assert edge.edge_type == "validated"

    def test_record_entity_write(self):
        builder = ReceiptBuilder(operation_type="add_entity")
        nid = builder.record_entity_write("Vehicle", "V-1", is_update=False)
        receipt = builder.build()
        node = [n for n in receipt.nodes if n.node_id == nid][0]
        assert node.node_type == "entity_write"
        assert node.entity_type == "Vehicle"
        assert node.entity_id == "V-1"
        edge = [e for e in receipt.edges if e.to_node == nid][0]
        assert edge.edge_type == "mutated"

    def test_record_relationship_write(self):
        builder = ReceiptBuilder(operation_type="add_relationship")
        nid = builder.record_relationship_write(
            "Part", "P-1", "Vehicle", "V-1", "fits", is_update=False
        )
        receipt = builder.build()
        node = [n for n in receipt.nodes if n.node_id == nid][0]
        assert node.node_type == "relationship_write"
        assert node.detail["relationship"] == "fits"
        edge = [e for e in receipt.edges if e.to_node == nid][0]
        assert edge.edge_type == "mutated"

    def test_record_feedback_applied(self):
        builder = ReceiptBuilder(operation_type="feedback")
        nid = builder.record_feedback_applied("P:1:fits:V:1", "approve", True)
        receipt = builder.build()
        node = [n for n in receipt.nodes if n.node_id == nid][0]
        assert node.node_type == "feedback_applied"
        assert node.detail["applied"] is True
        edge = [e for e in receipt.edges if e.to_node == nid][0]
        assert edge.edge_type == "applied"

    def test_record_ingest_batch(self):
        builder = ReceiptBuilder(operation_type="ingest")
        nid = builder.record_ingest_batch("parts_csv", added=10, updated=2)
        receipt = builder.build()
        node = [n for n in receipt.nodes if n.node_id == nid][0]
        assert node.node_type == "ingest_batch"
        assert node.detail["added"] == 10
        edge = [e for e in receipt.edges if e.to_node == nid][0]
        assert edge.edge_type == "mutated"

    def test_build_no_args_returns_empty_results(self):
        builder = ReceiptBuilder(operation_type="add_entity")
        receipt = builder.build()
        assert receipt.results == []


# ---------------------------------------------------------------------------
# Engine integration: receipts produced by execute_query
# ---------------------------------------------------------------------------


class TestReceiptIntegration:
    def test_receipt_attached_to_result(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        assert result.receipt is not None
        assert isinstance(result.receipt, Receipt)

    def test_receipt_has_query_metadata(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt
        assert receipt.query_name == "parts_for_vehicle"
        assert receipt.parameters == {"vehicle_id": "V-1"}
        assert receipt.duration_ms >= 0

    def test_receipt_records_entry_lookup(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt

        lookups = [n for n in receipt.nodes if n.node_type == "entity_lookup"]
        assert len(lookups) == 1
        assert lookups[0].entity_type == "Vehicle"
        assert lookups[0].entity_id == "V-1"

    def test_receipt_records_traversals(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Both P-1 and P-2 are traversed (filter result recorded separately)."""
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt

        traversals = [n for n in receipt.nodes if n.node_type == "edge_traversal"]
        assert len(traversals) == 2
        traversed_ids = {t.entity_id for t in traversals}
        assert traversed_ids == {"P-1", "P-2"}
        assert all("edge_key" in t.detail for t in traversals)

    def test_receipt_records_filter_pass_and_fail(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """P-1 has verified=True (pass), P-2 has verified=False (fail)."""
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt

        filters = [n for n in receipt.nodes if n.node_type == "filter_applied"]
        assert len(filters) == 2
        passed = [f for f in filters if f.detail["passed"] is True]
        failed = [f for f in filters if f.detail["passed"] is False]
        assert len(passed) == 1
        assert len(failed) == 1

    def test_receipt_records_results(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt

        result_nodes = [n for n in receipt.nodes if n.node_type == "result"]
        assert len(result_nodes) == 1
        assert result_nodes[0].detail["count"] == 1  # Only P-1 passes filter
        produced = [e for e in receipt.edges if e.to_node == result_nodes[0].node_id]
        assert len(produced) == 1
        parent = next(n for n in receipt.nodes if n.node_id == produced[0].from_node)
        assert parent.node_type == "edge_traversal"
        assert parent.entity_id == "P-1"

    def test_no_filter_query_has_traversals_only(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """vehicles_for_part has no filter — should have traversals but no filter nodes."""
        result = execute_query(config, graph, "vehicles_for_part", {"part_number": "P-1"})
        receipt = result.receipt

        filters = [n for n in receipt.nodes if n.node_type == "filter_applied"]
        assert len(filters) == 0

        traversals = [n for n in receipt.nodes if n.node_type == "edge_traversal"]
        assert len(traversals) == 1
        assert traversals[0].entity_id == "V-1"

    def test_receipt_dag_edges_are_connected(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        """Every non-root node should be reachable from some edge."""
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt

        to_nodes = {e.to_node for e in receipt.edges}
        non_root = [n for n in receipt.nodes if n.node_type != "query"]
        for node in non_root:
            assert node.node_id in to_nodes, f"{node.node_id} has no incoming edge"

    def test_receipt_results_in_build_match_query_results(
        self,
        config: CoreConfig,
        graph: EntityGraph,
    ):
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        receipt = result.receipt
        assert len(receipt.results) == len(result.results)


# ---------------------------------------------------------------------------
# Serializer tests
# ---------------------------------------------------------------------------


class TestSerializer:
    @pytest.fixture
    def receipt(self, config: CoreConfig, graph: EntityGraph) -> Receipt:
        result = execute_query(config, graph, "parts_for_vehicle", {"vehicle_id": "V-1"})
        return result.receipt

    def test_to_json_roundtrip(self, receipt: Receipt):
        json_str = to_json(receipt)
        restored = Receipt.model_validate_json(json_str)
        assert restored.receipt_id == receipt.receipt_id
        assert restored.query_name == receipt.query_name
        assert len(restored.nodes) == len(receipt.nodes)
        assert len(restored.edges) == len(receipt.edges)

    def test_to_json_contains_nodes(self, receipt: Receipt):
        json_str = to_json(receipt)
        assert '"node_type"' in json_str
        assert '"entity_lookup"' in json_str

    def test_to_markdown_has_header(self, receipt: Receipt):
        md = to_markdown(receipt)
        assert f"# Receipt {receipt.receipt_id}" in md
        assert "**Query:** parts_for_vehicle" in md
        assert "**Committed:** No" not in md

    def test_to_markdown_has_sections(self, receipt: Receipt):
        md = to_markdown(receipt)
        assert "## Entry Points" in md
        assert "Vehicle:V-1" in md
        assert "## Traversals" in md
        assert "## Filters" in md
        assert "[PASS]" in md
        assert "[FAIL]" in md

    def test_to_markdown_omits_empty_sections(self):
        builder = ReceiptBuilder(query_name="q", parameters={})
        receipt = builder.build(results=[])
        md = to_markdown(receipt)
        assert "## Entry Points" not in md
        assert "## Traversals" not in md
        assert "## Filters" not in md
        assert "## Constraints" not in md

    def test_to_mermaid_starts_with_graph(self, receipt: Receipt):
        mermaid = to_mermaid(receipt)
        assert mermaid.startswith("graph TD")

    def test_to_mermaid_has_all_nodes(self, receipt: Receipt):
        mermaid = to_mermaid(receipt)
        for node in receipt.nodes:
            assert node.node_id in mermaid

    def test_to_mermaid_has_all_edges(self, receipt: Receipt):
        mermaid = to_mermaid(receipt)
        for edge in receipt.edges:
            assert edge.from_node in mermaid
            assert edge.to_node in mermaid
            assert edge.edge_type in mermaid

    def test_to_mermaid_labels(self, receipt: Receipt):
        mermaid = to_mermaid(receipt)
        assert "Query: parts_for_vehicle" in mermaid
        assert "Lookup: Vehicle:V-1" in mermaid
        assert "Filter: PASS" in mermaid

    def test_to_markdown_mutation_header(self):
        builder = ReceiptBuilder(operation_type="add_entity", parameters={"count": 1})
        builder.record_entity_write("Vehicle", "V-1", is_update=False)
        builder.mark_committed()
        receipt = builder.build()
        md = to_markdown(receipt)
        assert "(add_entity)" in md
        assert "**Operation:** add_entity" in md
        assert "**Committed:** Yes" in md

    def test_to_markdown_workflow_mode_and_uncommitted_state(self):
        builder = ReceiptBuilder(
            query_name="wf",
            parameters={},
            operation_type="workflow",
            head_snapshot_id="snap_123",
            workflow_mode="preview",
        )
        receipt = builder.build()
        md = to_markdown(receipt)
        assert "**Head snapshot:** snap_123" in md
        assert "**Workflow mode:** preview" in md
        assert "**Committed:** No" in md

    def test_to_markdown_mutation_writes_section(self):
        builder = ReceiptBuilder(operation_type="add_entity", parameters={"count": 1})
        builder.record_entity_write("Vehicle", "V-1", is_update=False)
        builder.mark_committed()
        receipt = builder.build()
        md = to_markdown(receipt)
        assert "## Writes" in md
        assert "Vehicle:V-1" in md

    def test_to_mermaid_mutation_label(self):
        builder = ReceiptBuilder(operation_type="add_entity", parameters={})
        receipt = builder.build()
        mermaid = to_mermaid(receipt)
        assert "Mutation: add_entity" in mermaid

    def test_node_label_validation(self):
        from cruxible_core.receipt.serializer import _node_label
        from cruxible_core.receipt.types import ReceiptNode

        node = ReceiptNode(node_id="n1", node_type="validation", detail={"passed": True})
        assert _node_label(node) == "Validation: PASS"

    def test_node_label_entity_write(self):
        from cruxible_core.receipt.serializer import _node_label
        from cruxible_core.receipt.types import ReceiptNode

        node = ReceiptNode(
            node_id="n1",
            node_type="entity_write",
            entity_type="Vehicle",
            entity_id="V-1",
            detail={"is_update": False},
        )
        assert _node_label(node) == "Write: Vehicle:V-1 (add)"

    def test_node_label_relationship_write(self):
        from cruxible_core.receipt.serializer import _node_label
        from cruxible_core.receipt.types import ReceiptNode

        node = ReceiptNode(
            node_id="n1",
            node_type="relationship_write",
            detail={
                "from_id": "P-1",
                "to_id": "V-1",
                "relationship": "fits",
                "is_update": True,
            },
        )
        assert _node_label(node) == "Write: P-1 --fits--> V-1 (update)"

    def test_node_label_feedback_applied(self):
        from cruxible_core.receipt.serializer import _node_label
        from cruxible_core.receipt.types import ReceiptNode

        node = ReceiptNode(
            node_id="n1",
            node_type="feedback_applied",
            detail={"action": "approve", "applied": True},
        )
        assert _node_label(node) == "Feedback: approve (applied)"

    def test_node_label_ingest_batch(self):
        from cruxible_core.receipt.serializer import _node_label
        from cruxible_core.receipt.types import ReceiptNode

        node = ReceiptNode(
            node_id="n1",
            node_type="ingest_batch",
            detail={"mapping": "parts_csv", "added": 10, "updated": 2},
        )
        assert _node_label(node) == "Ingest: parts_csv (10 added, 2 updated)"
