"""Refused acts must be as auditable as accepted ones.

A refusal is the highest-information row the instance produces: it names a
proposal that a governed rule rejected. Before these fixes a refusal receipt
carried a prose ``guard_error`` string with no entity coordinates (indexing zero
rows in ``receipt_entities``, so "prior refusals on this subject" was
unanswerable) and shed the refused proposal body to a digest under the default
retention mode — destroying the only copy, since a refused write leaves no state
to reconstruct it from.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.receipt.types import Receipt, ReceiptNode
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entities,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_batch_direct_write,
)

GUARD_CONFIG_YAML = """\
version: "1.0"
name: negative_experience_fidelity

entity_types:
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string
      head:
        type: string
        optional: true

mutation_guards:
  - name: review_head_frozen_while_approved
    entity_type: Review
    property: head
    condition:
      type: frozen
      while: {status: approved}
    message: "approved reviews pin the reviewed head"
"""


EDGE_GUARD_CONFIG_YAML = """\
version: "1.0"
name: negative_experience_fidelity_edges

entity_types:
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true

relationships:
  - name: fits
    from: Part
    to: Vehicle

mutation_guards:
  - name: fits_requires_source_evidence
    relationship_type: fits
    condition:
      type: evidence
      require_evidence: source_evidence
    message: "Fitment observations require source evidence."
"""


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(dedent(GUARD_CONFIG_YAML))
    return CruxibleInstance.init(tmp_path, "config.yaml")


@pytest.fixture
def edge_guard_instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(dedent(EDGE_GUARD_CONFIG_YAML))
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    service_add_entity_inputs(
        inst,
        [
            EntityWriteInput(
                entity_type="Part",
                entity_id="BP-1",
                properties={"part_number": "BP-1"},
            ),
            EntityWriteInput(
                entity_type="Vehicle",
                entity_id="V-1",
                properties={"vehicle_id": "V-1"},
            ),
        ],
    )
    return inst


def _write_review(instance: CruxibleInstance, properties: dict[str, Any]) -> None:
    service_add_entity_inputs(
        instance,
        [EntityWriteInput(entity_type="Review", entity_id="rev-1", properties=properties)],
    )


def _refuse_head_change(instance: CruxibleInstance) -> str:
    """Trip the freeze guard and return the refusal receipt id."""
    _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-a"})
    with pytest.raises(DataValidationError) as excinfo:
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-b"})
    receipt_id = excinfo.value.mutation_receipt_id
    assert receipt_id is not None
    return receipt_id


def _load_receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    assert receipt is not None
    return receipt


def _guard_refusal_nodes(receipt: Receipt) -> list[ReceiptNode]:
    return [node for node in receipt.nodes if "guard_error" in node.detail]


def _guard_pass_nodes(receipt: Receipt) -> list[ReceiptNode]:
    return [node for node in receipt.nodes if "guard_passed" in node.detail]


class TestStructuredGuardRefusalNodes:
    def test_refusal_node_carries_structured_coordinates(self, instance: CruxibleInstance) -> None:
        receipt = _load_receipt(instance, _refuse_head_change(instance))
        assert receipt.committed is False

        refusals = _guard_refusal_nodes(receipt)
        assert len(refusals) == 1
        node = refusals[0]
        # Node-level coordinates: what makes the refusal reverse-indexable.
        assert node.node_type == "validation"
        assert node.entity_type == "Review"
        assert node.entity_id == "rev-1"
        assert node.detail["passed"] is False
        # Structured detail: which guard fired, on which property, at what value.
        assert node.detail["guard_name"] == "review_head_frozen_while_approved"
        assert node.detail["guard_property"] == "head"
        assert node.detail["guard_value"] == "sha-b"
        assert node.detail["entity_type"] == "Review"
        assert node.detail["entity_id"] == "rev-1"
        assert "approved reviews pin the reviewed head" in node.detail["guard_error"]

    def test_refusal_receipt_is_returned_by_get_receipts_for_entity(
        self, instance: CruxibleInstance
    ) -> None:
        receipt_id = _refuse_head_change(instance)
        store = instance.get_receipt_store()
        try:
            receipt_ids = store.get_receipts_for_entity("Review", "rev-1")
        finally:
            store.close()
        assert receipt_id in receipt_ids

    def test_guard_refusal_message_contract_is_unchanged(self, instance: CruxibleInstance) -> None:
        """The flat error-string contract callers depend on still holds."""
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-a"})
        with pytest.raises(DataValidationError) as excinfo:
            _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-b"})
        assert len(excinfo.value.errors) == 1
        assert "review_head_frozen_while_approved" in excinfo.value.errors[0]


class TestRelationshipGuardRefusalCoordinates:
    """Edge refusals carry both endpoints, mirroring relationship_write nodes."""

    def _refuse_edge(self, instance: CruxibleInstance) -> str:
        with pytest.raises(DataValidationError) as excinfo:
            service_add_relationship_inputs(
                instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        evidence_rationale="looks right",
                    )
                ],
                source="test",
                source_ref="negative_experience_edge_refusal",
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        return receipt_id

    def test_edge_refusal_node_carries_both_endpoints(
        self, edge_guard_instance: CruxibleInstance
    ) -> None:
        receipt = _load_receipt(edge_guard_instance, self._refuse_edge(edge_guard_instance))
        refusals = _guard_refusal_nodes(receipt)
        assert len(refusals) == 1
        detail = refusals[0].detail
        assert detail["guard_name"] == "fits_requires_source_evidence"
        assert detail["from_type"] == "Part"
        assert detail["from_id"] == "BP-1"
        assert detail["to_type"] == "Vehicle"
        assert detail["to_id"] == "V-1"
        assert detail["relationship"] == "fits"

    def test_edge_refusal_is_indexed_from_both_endpoints(
        self, edge_guard_instance: CruxibleInstance
    ) -> None:
        receipt_id = self._refuse_edge(edge_guard_instance)
        store = edge_guard_instance.get_receipt_store()
        try:
            assert receipt_id in store.get_receipts_for_entity("Part", "BP-1")
            assert receipt_id in store.get_receipts_for_entity("Vehicle", "V-1")
        finally:
            store.close()

    def test_batch_direct_write_refusal_carries_coordinates(
        self, edge_guard_instance: CruxibleInstance
    ) -> None:
        """The batch path records the same structured refusal as the single path."""
        with pytest.raises(DataValidationError) as excinfo:
            service_batch_direct_write(
                edge_guard_instance,
                BatchDirectWriteInput(
                    relationships=[
                        BatchRelationshipWriteInput(
                            from_type="Part",
                            from_id="BP-1",
                            relationship_type="fits",
                            to_type="Vehicle",
                            to_id="V-1",
                            evidence_rationale="looks right",
                        )
                    ],
                ),
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        receipt = _load_receipt(edge_guard_instance, receipt_id)
        refusals = _guard_refusal_nodes(receipt)
        assert len(refusals) == 1
        assert refusals[0].detail["from_id"] == "BP-1"
        assert refusals[0].detail["to_id"] == "V-1"

        store = edge_guard_instance.get_receipt_store()
        try:
            assert receipt_id in store.get_receipts_for_entity("Part", "BP-1")
        finally:
            store.close()


class TestSuccessSideGuardAnnotation:
    def test_permitting_guard_is_recorded_with_coordinates(
        self, instance: CruxibleInstance
    ) -> None:
        """A guard that evaluated and permitted the write says so on the receipt."""
        _write_review(instance, {"review_id": "rev-1", "status": "requested", "head": "sha-a"})
        # status != approved, so the freeze is inactive: the guard runs and permits.
        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-1",
                    properties={"review_id": "rev-1", "status": "requested", "head": "sha-b"},
                )
            ],
        )
        assert result.receipt_id is not None
        receipt = _load_receipt(instance, result.receipt_id)
        assert receipt.committed is True

        passes = _guard_pass_nodes(receipt)
        assert len(passes) == 1
        node = passes[0]
        assert node.node_type == "validation"
        assert node.detail["passed"] is True
        assert node.detail["guard_passed"] == "review_head_frozen_while_approved"
        assert node.detail["guard_property"] == "head"
        assert node.entity_type == "Review"
        assert node.entity_id == "rev-1"

    def test_guard_annotation_is_indexed(self, instance: CruxibleInstance) -> None:
        _write_review(instance, {"review_id": "rev-1", "status": "requested", "head": "sha-a"})
        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-1",
                    properties={"review_id": "rev-1", "status": "requested", "head": "sha-b"},
                )
            ],
        )
        store = instance.get_receipt_store()
        try:
            receipt_ids = store.get_receipts_for_entity("Review", "rev-1")
        finally:
            store.close()
        assert result.receipt_id in receipt_ids

    def test_creates_are_not_annotated_as_freeze_evaluations(
        self, instance: CruxibleInstance
    ) -> None:
        """A freeze cannot trip on a create, so a create is not an evaluation."""
        result = service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type="Review",
                    entity_id="rev-1",
                    properties={"review_id": "rev-1", "status": "approved", "head": "sha-a"},
                )
            ],
        )
        assert result.receipt_id is not None
        receipt = _load_receipt(instance, result.receipt_id)
        assert _guard_pass_nodes(receipt) == []


class TestRefusalRetentionFloor:
    def test_refused_proposal_body_survives_metadata_retention(
        self, instance: CruxibleInstance
    ) -> None:
        assert instance.load_config().runtime.mutation_payloads == "metadata"
        receipt = _load_receipt(instance, _refuse_head_change(instance))

        # The refused proposal body is intact, not reduced to a digest marker.
        assert "_cruxible_payload_omitted" not in receipt.parameters
        assert receipt.parameters == {"count": 1}
        root = receipt.nodes[0]
        assert root.node_type == "mutation"
        assert root.detail["parameters"] == {"count": 1}
        assert root.payload_metadata is not None
        assert root.payload_metadata.retention == "full"
        assert root.payload_metadata.stored_inline is True
        # The digest is still stamped, exactly as under every other mode.
        assert root.payload_metadata.payload_digest.startswith("sha256:")

    def test_accepted_path_still_sheds_under_metadata_retention(
        self, instance: CruxibleInstance
    ) -> None:
        """The exemption is refusal-scoped: committed receipts are unaffected."""
        result = service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Review",
                    entity_id="rev-9",
                    properties={"review_id": "rev-9", "status": "requested"},
                )
            ],
        )
        assert result.receipt_id is not None
        receipt = _load_receipt(instance, result.receipt_id)
        assert receipt.committed is True
        assert "_cruxible_payload_omitted" in receipt.parameters
        root = receipt.nodes[0]
        assert root.payload_metadata is not None
        assert root.payload_metadata.retention == "metadata"
        assert root.payload_metadata.stored_inline is False


class TestMutationReceiptStateCoordinates:
    def test_committed_mutation_receipt_carries_both_coordinates(
        self, instance: CruxibleInstance
    ) -> None:
        snapshot = instance.create_snapshot("baseline")
        revision_before = instance.get_read_revision()
        result = service_add_entities(
            instance,
            [
                EntityInstance(
                    entity_type="Review",
                    entity_id="rev-2",
                    properties={"review_id": "rev-2", "status": "requested"},
                )
            ],
        )
        assert result.receipt_id is not None
        receipt = _load_receipt(instance, result.receipt_id)
        assert receipt.head_snapshot_id == snapshot.snapshot_id
        # Decision-time, not commit-time: the revision observed when the write opened.
        assert receipt.read_revision == revision_before
        assert instance.get_read_revision() > revision_before

    def test_refusal_receipt_carries_both_coordinates(self, instance: CruxibleInstance) -> None:
        instance.create_snapshot("baseline")
        receipt = _load_receipt(instance, _refuse_head_change(instance))
        assert receipt.head_snapshot_id is not None
        assert receipt.read_revision is not None

    def test_query_receipts_are_unchanged(self) -> None:
        """Stamping is mutation-scoped; read receipts keep their prior shape."""
        built = ReceiptBuilder(query_name="q", parameters={}).build()
        assert built.read_revision is None
        assert built.head_snapshot_id is None

    def test_procedure_receipt_builder_still_omits_read_revision(self) -> None:
        """Procedure/workflow builders keep stamping only head_snapshot_id."""
        built = ReceiptBuilder(
            query_name="proc",
            parameters={},
            operation_type="procedure",
            head_snapshot_id="SNAP-1",
        ).build()
        assert built.head_snapshot_id == "SNAP-1"
        assert built.read_revision is None
