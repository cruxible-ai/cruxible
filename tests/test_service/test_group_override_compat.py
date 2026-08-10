"""A STORED ``assertion.group_override`` still governs, with no writer left.

The 0.4.0 removals deleted the only path that could set the flag, and with it
``tests/test_service/test_group_override.py`` -- which was also the only direct
coverage that the flag still WORKS. It does: 0.2.x/0.3 instances wrote edges
that carry it, ``group/governance.py`` still reads it, and the changelog
promises those edges keep behaving. Nothing writes it, so every case here seeds
the edge at the storage layer and asserts through the service layer.

Behaviors pinned, against ``src/cruxible_core/group/governance.py``:

- ``members_have_active_override`` (:286) reports true for a member whose live
  edge carries ``assertion.group_override is True`` (:299).
- ``review_priority_for_members`` (:353) then lifts a ``normal`` priority to
  ``review`` (:365).
- ``should_auto_resolve`` (:370) returns false whenever ``has_override`` (:379),
  so an otherwise auto-resolvable proposal is held for a human.

Each case pairs with a control that differs ONLY in the flag, so what is being
pinned is the override and not the mere existence of an edge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.graph.assertion_state import RelationshipAssertion
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
    mint_claim_id,
)
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import service_list, service_propose_group
from cruxible_core.service.groups import build_agent_proposal_signature_facts

CONFIG_YAML = """\
version: "1.0"
name: group_override_compat
description: Historical group_override edges under an auto-resolving policy

entity_types:
  Vehicle:
    properties:
      vehicle_id:
        type: string
        primary_key: true
      make:
        type: string
  Part:
    properties:
      part_number:
        type: string
        primary_key: true
      name:
        type: string

relationships:
  - name: fits
    from: Part
    to: Vehicle
    properties:
      verified:
        type: bool
        default: false
    proposal_policy:
      signals:
        check_v1:
          role: required
      auto_resolve_when: all_support
      auto_resolve_requires_prior_trust: trusted_only

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
            properties={"part_number": "BP-1", "name": "Pads"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vehicle",
            entity_id="V-1",
            properties={"vehicle_id": "V-1", "make": "Honda"},
        )
    )
    inst.save_graph(graph)
    return inst


def _member() -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support", evidence="fits")],
    )


def _seed_historical_edge(instance: CruxibleInstance, *, group_override: bool) -> None:
    """Write the edge the way a 0.2.x/0.3 instance left it, at the storage layer.

    Deliberately NOT through feedback: the write path that could set
    ``group_override`` is exactly what 0.4.0 removed, which is the whole point
    of this file.
    """
    graph = instance.load_graph()
    graph.add_relationship(
        RelationshipInstance(
            claim_id=mint_claim_id(),
            relationship_type="fits",
            from_type="Part",
            from_id="BP-1",
            to_type="Vehicle",
            to_id="V-1",
            properties={"verified": True},
            metadata=RelationshipMetadata(
                assertion=RelationshipAssertion(group_override=group_override)
            ),
        )
    )
    instance.save_graph(graph)


def _seed_trusted_precedent(instance: CruxibleInstance, members: list[CandidateMember]) -> None:
    """Store the confirmed, trusted prior approval auto-resolution requires."""
    rel_schema = instance.load_config().get_relationship("fits")
    assert rel_schema is not None
    facts: dict[str, Any] = build_agent_proposal_signature_facts(
        rel_schema=rel_schema,
        relationship_type="fits",
        signal_sources_used=[
            signal.signal_source for member in members for signal in member.signals
        ],
        agent_scope={},
        member_scope=[
            {
                "relationship_type": member.relationship_type,
                "from_type": member.from_type,
                "from_id": member.from_id,
                "to_type": member.to_type,
                "to_id": member.to_id,
            }
            for member in members
        ],
    )
    with instance.write_transaction() as uow:
        uow.groups.save_resolution(
            "fits",
            compute_group_signature("fits", facts),
            "approve",
            "prior rationale",
            "prior thesis",
            facts,
            {},
            trust_status="trusted",
            confirmed=True,
        )


def test_a_plain_historical_edge_leaves_auto_resolution_alone(
    instance: CruxibleInstance,
) -> None:
    """The control: an existing edge WITHOUT the flag changes nothing."""
    _seed_historical_edge(instance, group_override=False)
    members = [_member()]
    _seed_trusted_precedent(instance, members)

    result = service_propose_group(instance, "fits", members)

    assert result.review_priority == "normal"
    assert result.status == "resolved"
    assert result.resolution_id is not None


def test_a_stored_group_override_raises_review_priority_and_blocks_auto_resolve(
    instance: CruxibleInstance,
) -> None:
    """Same proposal, same precedent, one flag: held for review instead."""
    _seed_historical_edge(instance, group_override=True)
    members = [_member()]
    _seed_trusted_precedent(instance, members)

    result = service_propose_group(instance, "fits", members)

    # governance.py:365 -- policy said "normal", the stored override lifts it.
    assert result.review_priority == "review"
    # governance.py:379 -- has_override short-circuits auto-resolution, so the
    # trusted precedent does NOT approve this group.
    assert result.status == "pending_review"
    assert result.resolution_id is None


def test_a_stored_group_override_reads_back_faithfully(instance: CruxibleInstance) -> None:
    """The flag is write-once history now, and reads still report it."""
    _seed_historical_edge(instance, group_override=True)

    edges = service_list(instance, "edges", relationship_type="fits").items

    [edge] = edges
    assert edge["metadata"]["assertion"]["group_override"] is True
