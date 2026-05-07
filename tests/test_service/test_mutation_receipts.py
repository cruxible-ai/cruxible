"""Tests for mutation receipt wiring across service functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import (
    service_add_entities,
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

    def test_receipt_persistence_failure_nonfatal(self, initialized_instance: CruxibleInstance):
        """If receipt store fails, graph write still succeeds."""
        original_fn = type(initialized_instance).get_receipt_store

        def broken_store(self_inst):
            store = original_fn(self_inst)

            def fail_save(receipt):
                raise RuntimeError("Store broken")

            store.save_receipt = fail_save
            return store

        with patch.object(type(initialized_instance), "get_receipt_store", broken_store):
            result = service_add_entities(
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
        assert result.receipt_id is None
        # Graph write succeeded
        graph = initialized_instance.load_graph()
        assert graph.get_entity("Vehicle", "V-PERSIST") is not None

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
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "year": 2024, "make": "Honda", "model": "Civic"},
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
