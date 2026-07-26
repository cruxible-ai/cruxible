"""``workflow_apply`` marks group-approval drift like every other write path.

A canonical workflow apply is a LEGITIMATE governed write — it is not refused
when it changes a group-approved edge, because facts about the world do change.
But it never routed through the direct-write group-interaction detection, so it
could overwrite content a group signed off on and leave no trace whatsoever on
the edge. A reviewer reading that edge afterwards still believed the group had
approved what it now said.

These tests pin the marker on the workflow path, and the ruling that governs it
(Robert, 2026-07-25): the marker is CURRENT divergence — recomputed on every
write and DROPPED once the content matches the approval again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service import service_propose_group, service_resolve_group
from cruxible_core.workflow.apply import apply_relationship_set

CONFIG_YAML = """\
version: "1.0"
name: workflow_apply_group_drift_test
description: group-approval drift on the canonical workflow write path

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
    proposal_policy:
      signals:
        check_v1:
          role: required

contracts:
  EmptyInput:
    fields: {}

constraints: []
"""


@pytest.fixture
def approved_edge_instance(tmp_path: Path) -> CruxibleInstance:
    """An instance whose single ``fits`` edge was created by a group approval."""
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

    proposed = service_propose_group(
        instance,
        "fits",
        [
            CandidateMember(
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                relationship_type="fits",
                properties={"verified": False, "note": "as approved"},
                signals=[CandidateSignal(signal_source="check_v1", signal="support")],
            )
        ],
        thesis_text="fitment",
        thesis_facts={"rule": "catalog"},
    )
    service_resolve_group(instance, proposed.group_id, "approve", expected_pending_version=1)
    return instance


def _apply(instance: CruxibleInstance, properties: dict[str, object]) -> EntityGraph:
    graph = instance.load_graph()
    apply_relationship_set(
        instance,
        graph,
        "restate_fits",
        "apply_fits",
        {
            "relationship_type": "fits",
            "relationships": [
                RelationshipInstance(
                    relationship_type="fits",
                    from_type="Part",
                    from_id="BP-1",
                    to_type="Vehicle",
                    to_id="V-1",
                    properties=properties,
                ).model_dump(mode="python")
            ],
        },
        ReceiptBuilder(operation_type="workflow", parameters={}),
        persist_writes=True,
        parent_id=None,
    )
    instance.save_graph(graph)
    return graph


def _drift(instance: CruxibleInstance):
    edge = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
    assert edge is not None
    return edge.metadata.assertion.group_approval_drift


def test_workflow_apply_marks_drift_on_a_group_approved_edge(
    approved_edge_instance: CruxibleInstance,
) -> None:
    _apply(approved_edge_instance, {"verified": True, "note": "as approved"})

    drift = _drift(approved_edge_instance)
    assert drift is not None
    assert drift.changed_properties == ["verified"]
    assert drift.approved_values == {"verified": False}
    assert drift.receipt_id is not None
    assert drift.detected_at is not None

    # The edge stays live and stays approved: the write was legitimate.
    edge = approved_edge_instance.load_graph().get_relationship(
        "Part", "BP-1", "Vehicle", "V-1", "fits"
    )
    assert edge is not None
    assert edge.properties["verified"] is True
    assert edge.metadata.assertion.review.status == "approved"


def test_a_workflow_apply_that_changes_nothing_marks_nothing(
    approved_edge_instance: CruxibleInstance,
) -> None:
    _apply(approved_edge_instance, {"verified": False, "note": "as approved"})

    assert _drift(approved_edge_instance) is None


def test_a_full_revert_by_workflow_apply_drops_the_marker(
    approved_edge_instance: CruxibleInstance,
) -> None:
    """RULING: the marker is current divergence, not a permanent stain."""
    _apply(approved_edge_instance, {"verified": True, "note": "as approved"})
    assert _drift(approved_edge_instance) is not None

    _apply(approved_edge_instance, {"verified": False, "note": "as approved"})

    assert _drift(approved_edge_instance) is None


def test_a_partial_revert_lists_only_the_still_divergent_properties(
    approved_edge_instance: CruxibleInstance,
) -> None:
    _apply(approved_edge_instance, {"verified": True, "note": "changed"})
    first = _drift(approved_edge_instance)
    assert first is not None
    assert first.changed_properties == ["note", "verified"]
    assert first.approved_values == {"verified": False, "note": "as approved"}

    _apply(approved_edge_instance, {"verified": False, "note": "changed"})

    second = _drift(approved_edge_instance)
    assert second is not None
    # ``verified`` matches the approval again; ``note`` still does not. The
    # approved baseline is carried forward, so the record still says what the
    # GROUP approved rather than what the edge said last time.
    assert second.changed_properties == ["note"]
    assert second.approved_values == {"note": "as approved"}
    assert second.first_detected_at is not None
    assert second.first_detected_at < second.detected_at


def test_an_ungrouped_edge_is_never_marked(approved_edge_instance: CruxibleInstance) -> None:
    graph = approved_edge_instance.load_graph()
    graph.add_entity(
        EntityInstance(entity_type="Vehicle", entity_id="V-2", properties={"vehicle_id": "V-2"})
    )
    approved_edge_instance.save_graph(graph)

    for note in ("first", "second"):
        graph = approved_edge_instance.load_graph()
        apply_relationship_set(
            approved_edge_instance,
            graph,
            "restate_fits",
            "apply_fits",
            {
                "relationship_type": "fits",
                "relationships": [
                    RelationshipInstance(
                        relationship_type="fits",
                        from_type="Part",
                        from_id="BP-1",
                        to_type="Vehicle",
                        to_id="V-2",
                        properties={"verified": True, "note": note},
                    ).model_dump(mode="python")
                ],
            },
            ReceiptBuilder(operation_type="workflow", parameters={}),
            persist_writes=True,
            parent_id=None,
        )
        approved_edge_instance.save_graph(graph)

    edge = approved_edge_instance.load_graph().get_relationship(
        "Part", "BP-1", "Vehicle", "V-2", "fits"
    )
    assert edge is not None
    assert edge.metadata.assertion.group_approval_drift is None
