"""Tests for service_feedback group_override modification."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, RelationshipAmbiguityError
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import (
    service_feedback,
    service_propose_group,
    service_query,
    service_resolve_group,
)

CONFIG_YAML = """\
version: "1.0"
name: override_test
description: For group_override tests

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

named_queries:
  parts_for_vehicle:
    mode: traversal
    description: Find parts for vehicle
    entry_point: Vehicle
    traversal:
      - relationship: fits
        direction: incoming
    returns: "list[Part]"

constraints: []
"""


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
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
            properties={
                "vehicle_id": "V-1",
                "year": 2024,
                "make": "Honda",
                "model": "Civic",
            },
        )
    )
    # Add an edge to work with
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
    inst.save_graph(graph)
    return inst


def _get_receipt_id(instance: CruxibleInstance) -> str:
    """Create a query receipt for feedback."""
    result = service_query(
        instance,
        "parts_for_vehicle",
        {"vehicle_id": "V-1"},
    )
    return result.receipt_id


class TestGroupOverride:
    def test_stamps_on_edge(self, instance: CruxibleInstance) -> None:
        receipt_id = _get_receipt_id(instance)
        target = RelationshipInstance(
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
        )
        service_feedback(
            instance,
            receipt_id,
            "approve",
            "human",
            target,
            group_override=True,
        )
        graph = instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        assert rel.properties.get("group_override") is True

    def test_edge_not_in_graph_fails(self, instance: CruxibleInstance) -> None:
        """group_override requires the edge to exist."""
        receipt_id = _get_receipt_id(instance)
        target = RelationshipInstance(
            from_type="Part",
            from_id="BP-2",  # no edge for BP-2→V-1
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
        )
        with pytest.raises(ConfigError, match="group_override requires the edge to exist"):
            service_feedback(
                instance,
                receipt_id,
                "approve",
                "human",
                target,
                group_override=True,
            )

    def test_ambiguous_edge_fails(self, instance: CruxibleInstance) -> None:
        """group_override with no edge_key and multiple edges raises RelationshipAmbiguityError."""
        # Add a second same-type edge between BP-1→V-1
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": False, "source": "duplicate"},
            )
        )
        instance.save_graph(graph)

        receipt_id = _get_receipt_id(instance)
        target = RelationshipInstance(
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
            # no edge_key
        )
        with pytest.raises(RelationshipAmbiguityError):
            service_feedback(
                instance,
                receipt_id,
                "approve",
                "human",
                target,
                group_override=True,
            )

    def test_override_edge_skipped_in_future_resolve(self, instance: CruxibleInstance) -> None:
        """Edge with group_override exists → member skipped in resolve_group.
        With only one member and an existing edge, approve raises ConfigError
        (zero-edge first-time guard). With a second valid member, the override
        member is skipped and the valid one proceeds."""
        receipt_id = _get_receipt_id(instance)
        target = RelationshipInstance(
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
        )
        service_feedback(
            instance,
            receipt_id,
            "approve",
            "human",
            target,
            group_override=True,
        )

        # Propose with override member + a new valid member
        members = [
            CandidateMember(
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                relationship_type="fits",
                signals=[CandidateSignal(signal_source="check_v1", signal="support")],
            ),
            CandidateMember(
                from_type="Part",
                from_id="BP-2",
                to_type="Vehicle",
                to_id="V-1",
                relationship_type="fits",
                signals=[CandidateSignal(signal_source="check_v1", signal="support")],
            ),
        ]
        pr = service_propose_group(instance, "fits", members, thesis_facts={"test": True})
        result = service_resolve_group(instance, pr.group_id, "approve", expected_pending_version=1)
        # BP-1→V-1 already exists (group_override) → skipped
        assert result.edges_skipped == 1
        assert result.edges_created == 1

    def test_without_override_no_stamp(self, instance: CruxibleInstance) -> None:
        """Without group_override flag, no group_override property stamped."""
        receipt_id = _get_receipt_id(instance)
        target = RelationshipInstance(
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
        )
        service_feedback(instance, receipt_id, "approve", "human", target)
        graph = instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert "group_override" not in rel.properties
