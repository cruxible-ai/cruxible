"""Tests for mutation receipt wiring across service functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError, MutationError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    RelationshipWriteInput,
    service_add_entities,
    service_add_relationship_inputs,
    service_add_relationships,
    service_feedback,
    service_propose_group,
    service_query,
    service_resolve_group,
)

# ---------------------------------------------------------------------------
# add_entity receipts
# ---------------------------------------------------------------------------


class TestAddEntityReceipts:
    def test_add_entities_produces_receipt(self, initialized_instance: CruxibleInstance):
        result = service_add_entities(
            initialized_instance,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-NEW",
                    properties={
                        "vehicle_id": "V-NEW",
                        "year": 2025,
                        "make": "Toyota",
                        "model": "Camry",
                    },
                )
            ],
        )
        assert result.receipt_id is not None
        assert result.receipt_id.startswith("RCP-")

        # Receipt retrievable from store
        store = initialized_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "add_entity"
        assert receipt.committed is True

        # Has entity_write and validation nodes
        node_types = {n.node_type for n in receipt.nodes}
        assert "entity_write" in node_types
        assert "validation" in node_types

    def test_add_entities_failure_receipt(self, initialized_instance: CruxibleInstance):
        with pytest.raises(DataValidationError) as exc_info:
            service_add_entities(
                initialized_instance,
                [
                    EntityInstance(
                        entity_type="NonExistent",
                        entity_id="X-1",
                        properties={},
                    )
                ],
            )
        exc = exc_info.value
        assert exc.mutation_receipt_id is not None

        # Receipt retrievable
        store = initialized_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(exc.mutation_receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "add_entity"
        assert receipt.committed is False

    def test_receipt_persistence_failure_rolls_back_graph(
        self,
        initialized_instance: CruxibleInstance,
    ):
        """If the success receipt cannot persist, graph writes roll back too."""

        def fail_save(self, receipt):
            raise RuntimeError("Store broken")

        with (
            patch.object(SQLiteReceiptStore, "save_receipt", fail_save),
            pytest.raises(MutationError, match="Failed to persist mutation receipt"),
        ):
            service_add_entities(
                initialized_instance,
                [
                    EntityInstance(
                        entity_type="Vehicle",
                        entity_id="V-PERSIST",
                        properties={
                            "vehicle_id": "V-PERSIST",
                            "year": 2025,
                            "make": "X",
                            "model": "Y",
                        },
                    )
                ],
            )

        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-PERSIST") is None

    def test_create_receipt_false_suppresses(self, initialized_instance: CruxibleInstance):
        result = service_add_entities(
            initialized_instance,
            [
                EntityInstance(
                    entity_type="Vehicle",
                    entity_id="V-NORCPT",
                    properties={"vehicle_id": "V-NORCPT", "year": 2025, "make": "X", "model": "Y"},
                )
            ],
            _create_receipt=False,
        )
        assert result.receipt_id is None


# ---------------------------------------------------------------------------
# add_relationship receipts
# ---------------------------------------------------------------------------


class TestAddRelationshipReceipts:
    def test_add_relationships_produces_receipt(self, populated_instance: CruxibleInstance):
        result = service_add_relationships(
            populated_instance,
            [
                RelationshipInstance(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True, "source": "test"},
                )
            ],
            source="test",
            source_ref="test_receipts",
        )
        assert result.receipt_id is not None

        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "add_relationship"
        assert receipt.committed is True

        node_types = {n.node_type for n in receipt.nodes}
        assert "relationship_write" in node_types

    def test_add_relationships_receipt_records_evidence_detail(
        self,
        populated_instance: CruxibleInstance,
    ):
        result = service_add_relationship_inputs(
            populated_instance,
            [
                RelationshipWriteInput(
                    from_type="Part",
                    from_id="BP-1002",
                    relationship_type="fits",
                    to_type="Vehicle",
                    to_id="V-2024-ACCORD-SPORT",
                    properties={"verified": True, "source": "test"},
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
            source_ref="test_receipts_evidence",
        )
        assert result.receipt_id is not None

        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        write_nodes = [
            node for node in receipt.nodes if node.node_type == "relationship_write"
        ]
        assert len(write_nodes) == 1
        assert write_nodes[0].detail["evidence_refs"] == [
            {
                "source": "roadmap_doc",
                "source_record_id": "section-p0",
            }
        ]
        assert (
            write_nodes[0].detail["evidence_rationale"]
            == "Accepted direct source-backed assertion."
        )

    def test_add_relationships_failure_receipt(self, populated_instance: CruxibleInstance):
        with pytest.raises(DataValidationError) as exc_info:
            service_add_relationships(
                populated_instance,
                [
                    RelationshipInstance(
                        from_type="Part",
                        from_id="NONEXISTENT",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-2024-CIVIC-EX",
                        properties={},
                    )
                ],
                source="test",
                source_ref="test",
            )
        exc = exc_info.value
        assert exc.mutation_receipt_id is not None

        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(exc.mutation_receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.committed is False


# ---------------------------------------------------------------------------
# feedback receipts
# ---------------------------------------------------------------------------


def _edge_target() -> RelationshipInstance:
    return RelationshipInstance(
        from_type="Part",
        from_id="BP-1001",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-2024-CIVIC-EX",
    )


class TestFeedbackReceipts:
    def _run_query(self, instance: CruxibleInstance) -> str:
        """Run a query and return the receipt_id for feedback."""
        result = service_query(
            instance,
            "parts_for_vehicle",
            {"vehicle_id": "V-2024-CIVIC-EX"},
        )
        assert result.receipt_id is not None
        return result.receipt_id

    def test_feedback_produces_receipt(self, populated_instance: CruxibleInstance):
        receipt_id = self._run_query(populated_instance)
        result = service_feedback(
            populated_instance,
            receipt_id=receipt_id,
            action="approve",
            source="human",
            target=_edge_target(),
            reason="Confirmed fitment",
        )
        assert result.receipt_id is not None

        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "feedback"
        assert receipt.committed is True

        node_types = {n.node_type for n in receipt.nodes}
        assert "feedback_applied" in node_types

    def test_feedback_receipt_includes_applied_status(self, populated_instance: CruxibleInstance):
        receipt_id = self._run_query(populated_instance)
        result = service_feedback(
            populated_instance,
            receipt_id=receipt_id,
            action="approve",
            source="human",
            target=_edge_target(),
        )
        store = populated_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        # Find the feedback_applied node and check detail
        fb_nodes = [n for n in receipt.nodes if n.node_type == "feedback_applied"]
        assert len(fb_nodes) == 1
        assert "applied" in fb_nodes[0].detail

    def test_feedback_input_error_no_receipt(self, populated_instance: CruxibleInstance):
        """Bad action string raises ConfigError before builder created — no receipt."""
        with pytest.raises(ConfigError):
            service_feedback(
                populated_instance,
                receipt_id="RCP-doesnotmatter",
                action="invalid_action",  # type: ignore[arg-type]
                source="human",
                target=_edge_target(),
            )

    def test_feedback_apply_failure_rolls_back_feedback_row(
        self,
        populated_instance: CruxibleInstance,
    ):
        receipt_id = self._run_query(populated_instance)

        with (
            patch(
                "cruxible_core.service.feedback._apply_feedback_record",
                side_effect=RuntimeError("feedback graph update failed"),
            ),
            pytest.raises(
                MutationError,
                match="Unexpected failure: feedback graph update failed",
            ),
        ):
            service_feedback(
                populated_instance,
                receipt_id=receipt_id,
                action="reject",
                source="human",
                target=_edge_target(),
                reason="not a fit",
            )

        feedback_store = populated_instance.get_feedback_store()
        try:
            assert feedback_store.list_feedback(receipt_id=receipt_id) == []
        finally:
            feedback_store.close()
        rel = populated_instance.load_graph().get_relationship(
            "Part",
            "BP-1001",
            "Vehicle",
            "V-2024-CIVIC-EX",
            "fits",
        )
        assert rel is not None
        assert rel.metadata.assertion.review.status == "unreviewed"


# ---------------------------------------------------------------------------
# group_resolve receipts
# ---------------------------------------------------------------------------

RESOLVE_CONFIG_YAML = """\
version: "1.0"
name: resolve_receipt_test
description: For group_resolve receipt tests

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      year:
        type: int
      make:
        type: string
      model:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string
      category:
        type: string
        enum: [brakes, suspension, engine, electrical, body, interior]
      price:
        type: float
        optional: true

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
      source:
        type: string
        optional: true
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only
  - name: replaces
    from: Part
    to: Part
    properties:
      direction:
        type: string
        enum: [upgrade, downgrade, equivalent]
      confidence:
        type: float

constraints: []
"""


@pytest.fixture
def resolve_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance configured for group_resolve receipt tests."""
    (tmp_path / "config.yaml").write_text(RESOLVE_CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = inst.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1", "name": "Pads", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-2",
            properties={"part_number": "BP-2", "name": "Pads 2", "category": "brakes"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2",
            properties={"vehicle_id": "V-2", "year": 2024, "make": "Honda", "model": "Accord"},
        )
    )
    inst.save_graph(graph)
    return inst


def _resolve_member(from_id: str = "BP-1", to_id: str = "V-1") -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        properties={},
    )


def _propose_group(instance: CruxibleInstance, members=None) -> str:
    m = members or [_resolve_member()]
    result = service_propose_group(
        instance,
        "fits",
        m,
        thesis_text="test",
        thesis_facts={"style": "casual"},
    )
    return result.group_id


def _load_receipt(instance: CruxibleInstance, receipt_id: str | None) -> Receipt:
    assert receipt_id is not None
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return receipt


class TestGroupResolveReceipts:
    def test_resolve_approve_produces_receipt(self, resolve_instance: CruxibleInstance):
        group_id = _propose_group(resolve_instance)
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "approve",
            expected_pending_version=1,
        )
        assert result.receipt_id is not None

        store = resolve_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "group_resolve"
        assert receipt.committed is True

    def test_resolve_rollback_after_group_update_before_graph_replay(
        self,
        resolve_instance: CruxibleInstance,
    ):
        group_id = _propose_group(resolve_instance)

        with (
            patch(
                "cruxible_core.service.group_transitions._apply_resolved_relationships",
                side_effect=RuntimeError("graph replay failed"),
            ),
            pytest.raises(MutationError, match="Unexpected failure: graph replay failed"),
        ):
            service_resolve_group(
                resolve_instance,
                group_id,
                "approve",
                expected_pending_version=1,
            )

        store = resolve_instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group is not None
            assert group.status == "pending_review"
            assert group.resolution_id is None
            assert store.list_resolutions(relationship_type="fits") == []
        finally:
            store.close()
        assert (
            resolve_instance.load_graph().get_relationship(
                "Part",
                "BP-1",
                "Vehicle",
                "V-1",
                "fits",
            )
            is None
        )

    def test_resolve_approve_receipt_records_write_and_final_validation_shape(
        self,
        resolve_instance: CruxibleInstance,
    ):
        graph = resolve_instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": True},
            )
        )
        resolve_instance.save_graph(graph)

        group_id = _propose_group(
            resolve_instance,
            [_resolve_member("BP-1", "V-1"), _resolve_member("BP-2", "V-2")],
        )
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "approve",
            expected_pending_version=1,
        )
        receipt = _load_receipt(resolve_instance, result.receipt_id)

        assert receipt.operation_type == "group_resolve"
        assert receipt.committed is True
        write_nodes = [node for node in receipt.nodes if node.node_type == "relationship_write"]
        assert [node.detail for node in write_nodes] == [
            {
                "from_type": "Part",
                "from_id": "BP-2",
                "to_type": "Vehicle",
                "to_id": "V-2",
                "relationship": "fits",
                "is_update": False,
            }
        ]

        final_validation = next(
            node
            for node in receipt.nodes
            if node.node_type == "validation"
            and node.detail.get("resolution_id") == result.resolution_id
        )
        assert final_validation.detail["passed"] is True
        assert final_validation.detail["pending_version_at_resolve"] == 1
        assert final_validation.detail["applied_tuples"] == [
            {
                "from_type": "Part",
                "from_id": "BP-2",
                "to_type": "Vehicle",
                "to_id": "V-2",
                "relationship_type": "fits",
            }
        ]
        assert final_validation.detail["skipped_tuples_existing_edges"] == [
            {
                "from_type": "Part",
                "from_id": "BP-1",
                "to_type": "Vehicle",
                "to_id": "V-1",
                "relationship_type": "fits",
            }
        ]

    def test_resolve_reject_produces_receipt(self, resolve_instance: CruxibleInstance):
        group_id = _propose_group(resolve_instance)
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "reject",
            expected_pending_version=1,
        )
        assert result.receipt_id is not None

        store = resolve_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        assert receipt is not None
        assert receipt.operation_type == "group_resolve"
        assert receipt.committed is True

    def test_resolve_reject_receipt_records_validation_without_relationship_writes(
        self,
        resolve_instance: CruxibleInstance,
    ):
        group_id = _propose_group(resolve_instance)
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "reject",
            expected_pending_version=1,
        )
        receipt = _load_receipt(resolve_instance, result.receipt_id)

        assert receipt.operation_type == "group_resolve"
        assert receipt.committed is True
        assert [node for node in receipt.nodes if node.node_type == "relationship_write"] == []
        validation = next(node for node in receipt.nodes if node.node_type == "validation")
        assert validation.detail["passed"] is True
        assert validation.detail["action"] == "reject"
        assert validation.detail["members"] == 1
        assert validation.detail["pending_version_at_resolve"] == 1

    def test_resolve_no_inner_relationship_receipt(self, resolve_instance: CruxibleInstance):
        """Only 1 receipt (group_resolve), not 2 — inner add_relationships suppressed."""
        group_id = _propose_group(resolve_instance)
        service_resolve_group(resolve_instance, group_id, "approve", expected_pending_version=1)

        store = resolve_instance.get_receipt_store()
        try:
            receipts = store.list_receipts(operation_type="group_resolve")
            add_rel_receipts = store.list_receipts(operation_type="add_relationship")
        finally:
            store.close()
        assert len(receipts) == 1
        assert len(add_rel_receipts) == 0

    def test_resolve_receipt_has_validation_nodes(self, resolve_instance: CruxibleInstance):
        group_id = _propose_group(resolve_instance)
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "approve",
            expected_pending_version=1,
        )

        store = resolve_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        validation_nodes = [n for n in receipt.nodes if n.node_type == "validation"]
        assert len(validation_nodes) >= 1

    def test_resolve_receipt_has_write_nodes(self, resolve_instance: CruxibleInstance):
        group_id = _propose_group(resolve_instance)
        result = service_resolve_group(
            resolve_instance,
            group_id,
            "approve",
            expected_pending_version=1,
        )

        store = resolve_instance.get_receipt_store()
        try:
            receipt = store.get_receipt(result.receipt_id)
        finally:
            store.close()
        write_nodes = [n for n in receipt.nodes if n.node_type == "relationship_write"]
        assert len(write_nodes) >= 1
