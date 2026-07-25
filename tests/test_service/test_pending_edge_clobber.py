"""The relationship chokepoint refuses non-pending writes onto PENDING edges.

wi-pending-edge-clobber. ``graph.get_relationship`` is state-blind: it returns a
pending proposal exactly like a live edge, so ``validate_relationship`` reported
``is_update=True`` and the update branch of ``apply_relationship`` replaced the
proposal's properties IN PLACE while a reviewer was still adjudicating it. The
reviewer then approved content nobody proposed.

Robert's ruling is REFUSE. These tests pin:

  - a direct single write onto a pending edge is refused, and the proposal's
    content survives byte-for-byte;
  - the batch direct-write path inherits the same refusal (it funnels through
    the same chokepoint, in the prepare phase that both dry-run and live share);
  - canonical ``workflow_apply`` inherits it too — a governed source is NOT
    exempt, because an unattended workflow overwriting a human's staged proposal
    is precisely the clobber being closed;
  - the refusal is receipted, and its message teaches BOTH proposal-rail exits
    (re-propose/withdraw for the proposer, the resolution machinery for the
    reviewer);
  - once the proposal is accepted, ordinary updates work again — the refusal is
    a state conflict, not a permanent lock on the tuple;
  - pending-onto-pending is untouched: it stays governed by the existing
    create-only rule in the service layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import PendingEdgeWriteRefusedError
from cruxible_core.graph.assertion_state import RelationshipLifecycleState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.server.errors import error_to_response
from cruxible_core.service.execution import service_lock, service_run
from cruxible_core.service.feedback import service_feedback
from cruxible_core.service.mutations import (
    service_add_relationship_inputs,
    service_batch_direct_write,
)
from cruxible_core.service.types import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    RelationshipWriteInput,
)
from cruxible_core.workflow.apply import apply_relationship_set

CONFIG_YAML = """\
version: "1.0"
name: pending_edge_clobber_test
description: pending-proposal clobber fixture

entity_types:
  Vehicle:
    properties:
      vehicle_id: {type: string, primary_key: true}
  Part:
    properties:
      part_number: {type: string, primary_key: true}

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified: {type: bool, default: false}
      note: {type: string, optional: true}

named_queries:
  all_parts:
    mode: collection
    returns: Part
    result_shape: entity

contracts:
  EmptyInput:
    fields: {}

workflows:
  clobber_fits:
    type: canonical
    contract_in: EmptyInput
    returns: edges
    steps:
      - id: parts
        query: all_parts
        as: parts
      - id: edges
        make_relationships:
          relationship_type: fits
          items: $steps.parts.results
          from_type: Part
          from_id: $item.entity_id
          to_type: Vehicle
          to_id: V-1
          properties:
            verified: true
            note: clobbered
        as: edges
      - id: apply_edges
        apply_relationships:
          relationships_from: edges
        as: apply_edges

constraints: []
"""

PROPOSED_PROPERTIES = {"verified": False, "note": "as proposed"}
CLOBBER_PROPERTIES = {"verified": True, "note": "clobbered"}


def _fits_input(
    properties: dict[str, object],
    *,
    pending: bool = False,
    lifecycle: RelationshipLifecycleState | None = None,
) -> RelationshipWriteInput:
    return RelationshipWriteInput(
        from_type="Part",
        from_id="BP-1",
        relationship_type="fits",
        to_type="Vehicle",
        to_id="V-1",
        properties=properties,
        pending=pending,
        lifecycle=lifecycle,
    )


@pytest.fixture
def pending_edge_instance(tmp_path: Path) -> CruxibleInstance:
    """An instance whose single ``fits`` edge is a staged, unresolved proposal."""
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Part",
            entity_id="BP-1",
            properties={"part_number": "BP-1"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1"},
        )
    )
    instance.save_graph(graph)
    result = service_add_relationship_inputs(
        instance,
        [_fits_input(PROPOSED_PROPERTIES, pending=True)],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.added == 1
    assert _review_status(instance) == "pending"
    return instance


def _edge(instance: CruxibleInstance) -> RelationshipInstance:
    edge = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert edge is not None
    return edge


def _review_status(instance: CruxibleInstance) -> str:
    return _edge(instance).metadata.assertion.review.status


def _assert_proposal_intact(instance: CruxibleInstance) -> None:
    """The proposal a reviewer will see must be exactly what was proposed."""
    edge = _edge(instance)
    assert edge.properties == PROPOSED_PROPERTIES
    assert edge.metadata.assertion.review.status == "pending"


# ---------------------------------------------------------------------------
# Direct writes onto a pending edge are REFUSED
# ---------------------------------------------------------------------------


def test_single_direct_write_onto_pending_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    with pytest.raises(PendingEdgeWriteRefusedError) as exc:
        service_add_relationship_inputs(
            pending_edge_instance,
            [_fits_input(CLOBBER_PROPERTIES)],
            source="add_relationship",
            source_ref="add_relationship",
        )
    assert exc.value.relationship_type == "fits"
    assert (exc.value.from_type, exc.value.from_id) == ("Part", "BP-1")
    assert (exc.value.to_type, exc.value.to_id) == ("Vehicle", "V-1")
    _assert_proposal_intact(pending_edge_instance)


def test_batch_direct_write_onto_pending_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    with pytest.raises(PendingEdgeWriteRefusedError):
        service_batch_direct_write(
            pending_edge_instance,
            BatchDirectWriteInput(
                relationships=[
                    BatchRelationshipWriteInput(
                        from_type="Part",
                        from_id="BP-1",
                        relationship_type="fits",
                        to_type="Vehicle",
                        to_id="V-1",
                        properties=CLOBBER_PROPERTIES,
                    )
                ]
            ),
        )
    _assert_proposal_intact(pending_edge_instance)


def test_typed_lifecycle_write_onto_pending_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """The review-SAFE lifecycle channel is still a property-replacing write.

    ``lifecycle`` cannot flip the review axis, but it lands in the same update
    branch and replaces the edge's properties, so it clobbers a proposal exactly
    like a plain property write does.
    """
    with pytest.raises(PendingEdgeWriteRefusedError):
        service_add_relationship_inputs(
            pending_edge_instance,
            [
                _fits_input(
                    CLOBBER_PROPERTIES,
                    lifecycle=RelationshipLifecycleState(status="inactive"),
                )
            ],
            source="add_relationship",
            source_ref="add_relationship",
        )
    _assert_proposal_intact(pending_edge_instance)


def test_workflow_apply_onto_pending_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """A governed source is NOT exempt from this rail.

    ``workflow_apply`` reaches the update branch with ordinary upsert semantics,
    so an unattended canonical workflow would silently overwrite a human's staged
    proposal. It is refused with the same message every other path gets.
    """
    graph = pending_edge_instance.load_graph()
    relationship_set = {
        "relationship_type": "fits",
        "relationships": [
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties=CLOBBER_PROPERTIES,
            ).model_dump(mode="python")
        ],
    }
    with pytest.raises(PendingEdgeWriteRefusedError):
        apply_relationship_set(
            pending_edge_instance,
            graph,
            "clobber_workflow",
            "apply_fits",
            relationship_set,
            ReceiptBuilder(operation_type="workflow", parameters={}),
            persist_writes=True,
            parent_id=None,
        )
    _assert_proposal_intact(pending_edge_instance)


def test_workflow_service_facade_onto_pending_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """The same refusal through the SERVICE facade an operator actually calls.

    ``test_workflow_apply_onto_pending_refused`` above drives
    ``apply_relationship_set`` directly, which proves the chokepoint but skips
    the executor. Drive ``service_run`` (canonical preview) instead: the
    refusal surfaces at PREVIEW, so a canonical workflow whose apply step would
    clobber a staged proposal never even yields an apply digest — there is no
    window in which an operator could confirm the clobber.
    """
    service_lock(pending_edge_instance)

    with pytest.raises(PendingEdgeWriteRefusedError):
        service_run(pending_edge_instance, "clobber_fits", {})
    _assert_proposal_intact(pending_edge_instance)


# ---------------------------------------------------------------------------
# The refusal is receipted and teaches the proposal rail
# ---------------------------------------------------------------------------


def test_refusal_is_receipted_and_names_both_proposal_rail_exits(
    pending_edge_instance: CruxibleInstance,
) -> None:
    with pytest.raises(PendingEdgeWriteRefusedError) as exc:
        service_add_relationship_inputs(
            pending_edge_instance,
            [_fits_input(CLOBBER_PROPERTIES)],
            source="add_relationship",
            source_ref="add_relationship",
        )
    assert exc.value.mutation_receipt_id is not None

    message = str(exc.value)
    assert "PENDING proposal awaiting review" in message
    # Proposer exit.
    assert "withdraw" in message
    assert "pending=true" in message
    # Reviewer exit.
    assert "feedback approve/reject" in message
    assert "group resolve" in message


def test_pending_edge_refusal_maps_to_409() -> None:
    status, body = error_to_response(
        PendingEdgeWriteRefusedError("fits", "Part", "BP-1", "Vehicle", "V-1")
    )
    assert status == 409
    assert body.error_type == "PendingEdgeWriteRefusedError"
    assert body.error_code == "pending_edge_write_refused"
    assert body.context == {
        "relationship_type": "fits",
        "from_type": "Part",
        "from_id": "BP-1",
        "to_type": "Vehicle",
        "to_id": "V-1",
    }


# ---------------------------------------------------------------------------
# The rail is a STATE conflict, not a permanent lock
# ---------------------------------------------------------------------------


def test_write_after_acceptance_still_works(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """Approve the proposal, then update it: post-acceptance writes are normal."""
    service_feedback(
        pending_edge_instance,
        receipt_id=None,
        action="approve",
        source="human",
        target=RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
        ),
        reason="reviewed",
    )
    assert _review_status(pending_edge_instance) == "approved"

    result = service_add_relationship_inputs(
        pending_edge_instance,
        [_fits_input(CLOBBER_PROPERTIES)],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.updated == 1
    edge = _edge(pending_edge_instance)
    assert edge.properties == CLOBBER_PROPERTIES
    # The write did not disturb the accepted review state.
    assert edge.metadata.assertion.review.status == "approved"


def test_write_onto_a_rejected_edge_is_not_refused(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """Only ``pending`` blocks. A resolved (rejected) edge is adjudicated, so
    there is no in-flight proposal left to protect."""
    service_feedback(
        pending_edge_instance,
        receipt_id=None,
        action="reject",
        source="human",
        target=RelationshipInstance(
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
        ),
        reason="reviewed",
    )
    assert _review_status(pending_edge_instance) == "rejected"

    result = service_add_relationship_inputs(
        pending_edge_instance,
        [_fits_input(CLOBBER_PROPERTIES)],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.updated == 1


def test_pending_onto_pending_keeps_the_create_only_rule(
    pending_edge_instance: CruxibleInstance,
) -> None:
    """Unchanged by this rail: a pending write onto an existing edge is still
    rejected by the service layer's create-only rule, with its own message."""
    with pytest.raises(Exception) as exc:
        service_add_relationship_inputs(
            pending_edge_instance,
            [_fits_input(CLOBBER_PROPERTIES, pending=True)],
            source="add_relationship",
            source_ref="add_relationship",
        )
    assert not isinstance(exc.value, PendingEdgeWriteRefusedError)
    assert "pending relationship writes can only create new edges" in str(exc.value)
    _assert_proposal_intact(pending_edge_instance)


# ---------------------------------------------------------------------------
# Unrelated writes are untouched
# ---------------------------------------------------------------------------


def test_write_to_a_different_tuple_is_unaffected(
    pending_edge_instance: CruxibleInstance,
) -> None:
    graph: EntityGraph = pending_edge_instance.load_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-2",
            properties={"vehicle_id": "V-2"},
        )
    )
    pending_edge_instance.save_graph(graph)
    result = service_add_relationship_inputs(
        pending_edge_instance,
        [
            RelationshipWriteInput(
                from_type="Part",
                from_id="BP-1",
                relationship_type="fits",
                to_type="Vehicle",
                to_id="V-2",
                properties={"verified": True},
            )
        ],
        source="add_relationship",
        source_ref="add_relationship",
    )
    assert result.added == 1
    _assert_proposal_intact(pending_edge_instance)
