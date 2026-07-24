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
from cruxible_core.receipt.mutation_payloads import (
    MAX_RETAINED_PAYLOAD_BYTES,
    RETAINED_PAYLOAD_HEAD_BYTES,
    retain_mutation_payload,
)
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


GUARD_ERROR_CONFIG_YAML = """\
version: "1.0"
name: negative_experience_guard_errors

entity_types:
  WorkItem:
    properties:
      work_item_id:
        type: string
        primary_key: true
      status:
        type: string
  Review:
    properties:
      review_id:
        type: string
        primary_key: true
      status:
        type: string

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

mutation_guards:
  - name: closed_requires_prior_state_lookup
    entity_type: WorkItem
    property: status
    new_value: closed
    condition:
      type: query
      query_name: approved_review_for_work_item
      params:
        work_item_id: "$current.properties.work_item_id"
      min_count: 1
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


def _proposal_node(receipt: Receipt) -> ReceiptNode:
    nodes = [node for node in receipt.nodes if node.node_type == "proposal"]
    assert len(nodes) == 1
    return nodes[0]


def _proposal_body(receipt: Receipt) -> dict[str, Any]:
    body = _proposal_node(receipt).detail["proposal"]
    assert isinstance(body, dict)
    return body


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


class TestGuardEvaluatorErrorRefusals:
    """A refusal nobody can attribute must still be joinable to its subject.

    Guard-EVALUATION errors (an unresolvable ``$current`` reference on a create,
    say) are routed through ``GuardEvaluation.from_messages``: there is no guard
    to name and no coordinates to lift off the condition, so the refusal nodes
    are deliberately unattributed. Fabricating a guard name would be a lie; what
    makes the receipt findable instead is the PROPOSAL node's subjects.
    """

    @pytest.fixture
    def guard_error_instance(self, tmp_path: Path) -> CruxibleInstance:
        (tmp_path / "config.yaml").write_text(dedent(GUARD_ERROR_CONFIG_YAML))
        return CruxibleInstance.init(tmp_path, "config.yaml")

    def _refuse_born_closed(self, instance: CruxibleInstance) -> str:
        with pytest.raises(
            DataValidationError, match="Missing mutation guard param reference"
        ) as excinfo:
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
        receipt_id = excinfo.value.mutation_receipt_id
        assert instance.load_graph().get_entity("WorkItem", "wi-born-closed") is None
        assert receipt_id is not None
        return receipt_id

    def test_evaluator_error_refusal_is_joinable_from_its_subject(
        self, guard_error_instance: CruxibleInstance
    ) -> None:
        receipt_id = self._refuse_born_closed(guard_error_instance)
        store = guard_error_instance.get_receipt_store()
        try:
            found = store.get_receipts_for_entity("WorkItem", "wi-born-closed")
        finally:
            store.close()
        assert receipt_id in found

    def test_evaluator_error_refusal_names_no_guard_but_keeps_the_proposal(
        self, guard_error_instance: CruxibleInstance
    ) -> None:
        receipt = _load_receipt(
            guard_error_instance, self._refuse_born_closed(guard_error_instance)
        )
        assert receipt.committed is False
        refusals = _guard_refusal_nodes(receipt)
        assert refusals
        # Honest: no guard is attributed, because none can be.
        assert all("guard_name" not in node.detail for node in refusals)
        assert all(node.entity_type is None for node in refusals)
        # The proposal carries the coordinates and the body instead.
        body = _proposal_body(receipt)
        assert body["entities"][0]["properties"]["status"] == "closed"
        assert _proposal_node(receipt).detail["subjects"] == [
            {"entity_type": "WorkItem", "entity_id": "wi-born-closed"}
        ]


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


class TestRefusedProposalBody:
    """What a refusal retains must be the thing that was refused.

    The receipt's ``parameters`` are a SUMMARY the call site opens the receipt
    with (``{"count": 1}``), and the nodes carrying proposed properties are
    recorded only after the guards pass. Retaining the summary in full retains
    nothing: the proposal itself has to be on the receipt before any guard runs.
    """

    def test_refused_proposal_equals_the_submitted_payload(
        self, instance: CruxibleInstance
    ) -> None:
        assert instance.load_config().runtime.mutation_payloads == "metadata"
        receipt = _load_receipt(instance, _refuse_head_change(instance))

        submitted = EntityInstance(
            entity_type="Review",
            entity_id="rev-1",
            properties={"review_id": "rev-1", "status": "approved", "head": "sha-b"},
        )
        assert _proposal_body(receipt) == {
            "operation": "add_entity",
            "entities": [
                {
                    "entity_type": "Review",
                    "entity_id": "rev-1",
                    "properties": submitted.properties,
                    "metadata": submitted.metadata.model_dump(mode="json"),
                }
            ],
        }

    def test_refused_proposal_survives_metadata_retention(self, instance: CruxibleInstance) -> None:
        """The refusal floor covers the proposal, not only the summary parameters."""
        receipt = _load_receipt(instance, _refuse_head_change(instance))
        node = _proposal_node(receipt)
        assert "_cruxible_payload_omitted" not in node.detail["proposal"]
        assert node.payload_metadata is not None
        assert node.payload_metadata.retention == "full"
        assert node.payload_metadata.stored_inline is True
        assert node.payload_metadata.truncated is False
        # The digest is still stamped, exactly as under every other mode.
        assert node.payload_metadata.payload_digest.startswith("sha256:")

        # The summary parameters keep their own floor.
        assert "_cruxible_payload_omitted" not in receipt.parameters
        assert receipt.parameters == {"count": 1}

    def test_proposal_records_every_batch_member(self, instance: CruxibleInstance) -> None:
        """A refusal on one member must not shed the members it travelled with."""
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-a"})
        with pytest.raises(DataValidationError) as excinfo:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-1",
                        properties={"review_id": "rev-1", "status": "approved", "head": "sha-b"},
                    ),
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-2",
                        properties={"review_id": "rev-2", "status": "requested"},
                    ),
                ],
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        body = _proposal_body(_load_receipt(instance, receipt_id))
        assert [member["entity_id"] for member in body["entities"]] == ["rev-1", "rev-2"]
        assert body["entities"][1]["properties"] == {
            "review_id": "rev-2",
            "status": "requested",
        }

    def test_edge_proposal_carries_endpoints_and_evidence(
        self, edge_guard_instance: CruxibleInstance
    ) -> None:
        with pytest.raises(DataValidationError) as excinfo:
            service_add_relationship_inputs(
                edge_guard_instance,
                [
                    RelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        properties={"note": "hand-checked"},
                        evidence_rationale="looks right",
                    )
                ],
                source="test",
                source_ref="negative_experience_edge_refusal",
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        body = _proposal_body(_load_receipt(edge_guard_instance, receipt_id))
        assert body["operation"] == "add_relationship"
        assert body["source"] == "test"
        assert body["source_ref"] == "negative_experience_edge_refusal"
        member = body["relationships"][0]
        assert member["from_type"] == "Part"
        assert member["from_id"] == "BP-1"
        assert member["to_type"] == "Vehicle"
        assert member["to_id"] == "V-1"
        assert member["relationship"] == "fits"
        assert member["properties"] == {"note": "hand-checked"}
        # The evidence that FAILED the guard is exactly what the refusal is about.
        assert member["metadata"]["evidence"]["rationale"] == "looks right"

    def test_batch_direct_write_proposal_carries_both_kinds(
        self, edge_guard_instance: CruxibleInstance
    ) -> None:
        with pytest.raises(DataValidationError) as excinfo:
            service_batch_direct_write(
                edge_guard_instance,
                BatchDirectWriteInput(
                    entities=[
                        EntityWriteInput(
                            entity_type="Part",
                            entity_id="BP-2",
                            properties={"part_number": "BP-2"},
                        )
                    ],
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
        body = _proposal_body(_load_receipt(edge_guard_instance, receipt_id))
        assert body["operation"] == "batch_direct_write"
        assert [member["entity_id"] for member in body["entities"]] == ["BP-2"]
        assert body["relationships"][0]["evidence_rationale"] == "looks right"

    def test_validation_refusal_before_any_guard_still_retains_the_proposal(
        self, instance: CruxibleInstance
    ) -> None:
        """The proposal is attached before validation, not just before the guards."""
        with pytest.raises(DataValidationError) as excinfo:
            service_add_entity_inputs(
                instance,
                [
                    EntityWriteInput(
                        entity_type="Review",
                        entity_id="rev-3",
                        properties={"review_id": "rev-3", "status": "open", "nonsense": 1},
                    )
                ],
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        body = _proposal_body(_load_receipt(instance, receipt_id))
        assert body["entities"][0]["properties"]["nonsense"] == 1

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

        # The proposal sheds with it: a committed write IS its own record.
        node = _proposal_node(receipt)
        assert "_cruxible_payload_omitted" in node.detail["proposal"]
        assert node.payload_metadata is not None
        assert node.payload_metadata.retention == "metadata"
        # Coordinates are join keys, never body: they survive every mode.
        assert node.detail["subjects"] == [{"entity_type": "Review", "entity_id": "rev-9"}]


class TestRetainedProposalCeiling:
    """The refusal floor is bounded. Unbounded retention is not a floor."""

    def test_oversized_refused_proposal_is_capped_with_a_truncation_marker(
        self, instance: CruxibleInstance
    ) -> None:
        oversized = "x" * (MAX_RETAINED_PAYLOAD_BYTES + 1)
        _write_review(instance, {"review_id": "rev-1", "status": "approved", "head": "sha-a"})
        with pytest.raises(DataValidationError) as excinfo:
            _write_review(
                instance,
                {"review_id": "rev-1", "status": "approved", "head": oversized},
            )
        receipt_id = excinfo.value.mutation_receipt_id
        assert receipt_id is not None
        node = _proposal_node(_load_receipt(instance, receipt_id))

        marker = node.detail["proposal"]["_cruxible_payload_truncated"]
        assert marker["truncated"] is True
        assert marker["max_retained_bytes"] == MAX_RETAINED_PAYLOAD_BYTES
        assert marker["byte_count"] > MAX_RETAINED_PAYLOAD_BYTES
        assert marker["payload_digest"].startswith("sha256:")
        # A bounded head, not a silent truncation of the body in place.
        assert len(marker["head"].encode("utf-8")) <= RETAINED_PAYLOAD_HEAD_BYTES
        assert marker["head"].startswith('{"entities":')

        assert node.payload_metadata is not None
        assert node.payload_metadata.truncated is True
        assert node.payload_metadata.stored_inline is False
        assert node.payload_metadata.byte_count == marker["byte_count"]

    def test_under_the_ceiling_is_retained_verbatim(self, instance: CruxibleInstance) -> None:
        receipt = _load_receipt(instance, _refuse_head_change(instance))
        node = _proposal_node(receipt)
        assert "_cruxible_payload_truncated" not in node.detail["proposal"]
        assert node.payload_metadata is not None
        assert node.payload_metadata.byte_count <= MAX_RETAINED_PAYLOAD_BYTES

    def test_configured_full_retention_is_unchanged(self) -> None:
        """The ceiling is scoped to the policy-imposed floor, not to ``full`` mode."""
        big = {"body": "x" * (MAX_RETAINED_PAYLOAD_BYTES + 1)}
        retained, metadata = retain_mutation_payload(big, retention="full")
        assert retained == big
        assert metadata.stored_inline is True
        assert metadata.truncated is False


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
