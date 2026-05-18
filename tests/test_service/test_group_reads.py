"""Tests for service_get_group, service_list_groups, service_list_resolutions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import GroupNotFoundError
from cruxible_core.graph.assertion_state import RelationshipAssertion, RelationshipReviewState
from cruxible_core.graph.types import RelationshipInstance, RelationshipMetadata
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateGroup, CandidateMember, CandidateSignal
from cruxible_core.service import (
    service_get_group,
    service_group_status,
    service_list_groups,
    service_list_resolutions,
    service_propose_group,
    service_resolve_group,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_YAML = """\
version: "1.0"
name: read_tests
description: For read service tests

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
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    # Seed entities
    from cruxible_core.graph.types import EntityInstance

    graph = inst.load_graph()
    for pid in ("BP-1", "BP-2", "BP-3"):
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id=pid,
                properties={"part_number": pid, "name": f"Part {pid}", "category": "brakes"},
            )
        )
    for vid in ("V-1", "V-2", "V-3"):
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id=vid,
                properties={
                    "vehicle_id": vid,
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
    inst.save_graph(graph)
    return inst


def _member(from_id="BP-1", to_id="V-1", properties: dict | None = None):
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        properties=properties or {},
    )


# ---------------------------------------------------------------------------
# service_get_group
# ---------------------------------------------------------------------------


class TestGetGroup:
    def test_returns_group_and_members(self, instance: CruxibleInstance) -> None:
        result = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1"), _member("BP-2", "V-2")],
            thesis_text="test thesis",
            thesis_facts={"k": "v"},
        )
        get_result = service_get_group(instance, result.group_id)
        assert get_result.group.group_id == result.group_id
        assert len(get_result.members) == 2
        assert get_result.group.thesis_text == "test thesis"
        assert get_result.bucket_status is not None
        assert get_result.bucket_status.pending_group_id == result.group_id
        assert len(get_result.member_review) == 2
        assert get_result.member_review[0].current_edge_count == 0

    def test_member_review_includes_current_edge_delta(
        self,
        instance: CruxibleInstance,
    ) -> None:
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={
                    "verified": False,
                    "source": "catalog",
                },
                metadata=RelationshipMetadata(
                    assertion=RelationshipAssertion(
                        review=RelationshipReviewState(status="pending")
                    )
                ),
            )
        )
        instance.save_graph(graph)
        result = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1", {"verified": True, "source": "agent"})],
            thesis_facts={"k": "v"},
        )

        get_result = service_get_group(instance, result.group_id)
        review = get_result.member_review[0]

        assert review.proposed_tuple["from_id"] == "BP-1"
        assert review.current_edge_count == 1
        assert review.current_edge_key is not None
        assert review.current_review_status == "pending"
        assert review.current_properties is not None
        assert review.current_properties["source"] == "catalog"
        assert review.property_delta.changed == ["source", "verified"]
        assert review.property_delta.removed == []

    def test_resolution_populated_as_full_dict(self, instance: CruxibleInstance) -> None:
        result = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_facts={"k": "v"},
        )
        service_resolve_group(instance, result.group_id, "approve", expected_pending_version=1)
        get_result = service_get_group(instance, result.group_id)
        assert get_result.resolution is not None
        assert get_result.resolution.trust_status is not None
        assert get_result.resolution.trust_reason is not None
        assert get_result.resolution.confirmed is not None
        assert get_result.resolution.resolution_id is not None

    def test_not_found(self, instance: CruxibleInstance) -> None:
        with pytest.raises(GroupNotFoundError):
            service_get_group(instance, "GRP-nonexistent")


# ---------------------------------------------------------------------------
# service_list_groups
# ---------------------------------------------------------------------------


class TestListGroups:
    def test_list_all(self, instance: CruxibleInstance) -> None:
        service_propose_group(instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"a": 1})
        service_propose_group(instance, "fits", [_member("BP-2", "V-2")], thesis_facts={"a": 2})
        result = service_list_groups(instance)
        assert result.total == 2
        assert len(result.groups) == 2

    def test_filter_by_status(self, instance: CruxibleInstance) -> None:
        pr = service_propose_group(
            instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"a": 1}
        )
        service_propose_group(instance, "fits", [_member("BP-2", "V-2")], thesis_facts={"a": 2})
        service_resolve_group(instance, pr.group_id, "reject", expected_pending_version=1)
        pending = service_list_groups(instance, status="pending_review")
        assert pending.total == 1
        resolved = service_list_groups(instance, status="resolved")
        assert resolved.total == 1

    def test_filter_by_relationship_type(self, instance: CruxibleInstance) -> None:
        service_propose_group(instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"a": 1})
        fits = service_list_groups(instance, relationship_type="fits")
        assert fits.total == 1
        replaces = service_list_groups(instance, relationship_type="replaces")
        assert replaces.total == 0

    def test_sorted_by_priority(self, instance: CruxibleInstance) -> None:
        """Critical groups should appear before review and normal."""
        # Create a group (no prior → review priority)
        service_propose_group(instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"a": 1})
        # Create a group with invalidated prior → critical priority
        facts2 = {"b": 2}
        sig = compute_group_signature("fits", facts2)
        store = instance.get_group_store()
        try:
            with store.transaction():
                store.save_resolution(
                    "fits",
                    sig,
                    "approve",
                    "",
                    "",
                    facts2,
                    {},
                    "human",
                    trust_status="invalidated",
                    confirmed=True,
                )
        finally:
            store.close()
        service_propose_group(instance, "fits", [_member("BP-2", "V-2")], thesis_facts=facts2)

        result = service_list_groups(instance)
        assert result.groups[0].review_priority == "critical"
        assert result.groups[1].review_priority == "review"

    def test_limit(self, instance: CruxibleInstance) -> None:
        for i in range(5):
            service_propose_group(
                instance,
                "fits",
                [_member(f"BP-{i + 1}", f"V-{i + 1}")],
                thesis_facts={"i": i},
            )
        result = service_list_groups(instance, limit=2)
        assert len(result.groups) == 2
        assert result.total == 5


# ---------------------------------------------------------------------------
# service_list_resolutions
# ---------------------------------------------------------------------------


class TestListResolutions:
    def test_returns_analysis_state_and_thesis(self, instance: CruxibleInstance) -> None:
        pr = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="the thesis",
            thesis_facts={"k": "v"},
            analysis_state={"centroid": [0.1, 0.2]},
        )
        service_resolve_group(
            instance,
            pr.group_id,
            "approve",
            rationale="good",
            expected_pending_version=1,
        )
        result = service_list_resolutions(instance)
        assert result.total == 1
        r = result.resolutions[0]
        assert r.analysis_state == {"centroid": [0.1, 0.2]}
        assert r.thesis_facts == {"k": "v"}
        assert r.trust_status == "watch"
        assert r.trust_reason is not None

    def test_filter_by_relationship_type(self, instance: CruxibleInstance) -> None:
        pr = service_propose_group(
            instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"k": "v"}
        )
        service_resolve_group(instance, pr.group_id, "approve", expected_pending_version=1)
        fits = service_list_resolutions(instance, relationship_type="fits")
        assert fits.total == 1
        replaces = service_list_resolutions(instance, relationship_type="replaces")
        assert replaces.total == 0

    def test_filter_by_action(self, instance: CruxibleInstance) -> None:
        pr1 = service_propose_group(
            instance, "fits", [_member("BP-1", "V-1")], thesis_facts={"a": 1}
        )
        service_resolve_group(instance, pr1.group_id, "approve", expected_pending_version=1)
        pr2 = service_propose_group(
            instance, "fits", [_member("BP-2", "V-2")], thesis_facts={"a": 2}
        )
        service_resolve_group(instance, pr2.group_id, "reject", expected_pending_version=1)
        approvals = service_list_resolutions(instance, action="approve")
        assert approvals.total == 1
        rejects = service_list_resolutions(instance, action="reject")
        assert rejects.total == 1

    def test_limit(self, instance: CruxibleInstance) -> None:
        for i in range(3):
            pr = service_propose_group(
                instance,
                "fits",
                [_member(f"BP-{i + 1}", f"V-{i + 1}")],
                thesis_facts={"i": i},
            )
            service_resolve_group(instance, pr.group_id, "reject", expected_pending_version=1)
        result = service_list_resolutions(instance, limit=2)
        assert len(result.resolutions) == 2


class TestGroupStatus:
    def test_pending_thesis_text_takes_precedence(self, instance: CruxibleInstance) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        approved = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="approved thesis",
            thesis_facts=facts,
        )
        service_resolve_group(instance, approved.group_id, "approve", expected_pending_version=1)

        pending = service_propose_group(
            instance,
            "fits",
            [_member("BP-2", "V-2")],
            thesis_text="pending thesis",
            thesis_facts=facts,
        )

        status = service_group_status(instance, signature=pending.signature)
        assert status.thesis_text == "pending thesis"
        assert status.pending_group_id == pending.group_id
        assert status.pending_delta_count == 1

    def test_latest_resolution_thesis_text_used_without_pending(
        self, instance: CruxibleInstance
    ) -> None:
        facts = {"rule_id": "fit_rule", "rule_version": 1}
        first = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="first thesis",
            thesis_facts=facts,
        )
        service_resolve_group(instance, first.group_id, "approve", expected_pending_version=1)

        second = service_propose_group(
            instance,
            "fits",
            [_member("BP-2", "V-2")],
            thesis_text="latest thesis",
            thesis_facts=facts,
        )
        service_resolve_group(instance, second.group_id, "approve", expected_pending_version=1)

        status = service_group_status(instance, signature=second.signature)
        assert status.pending_group_id is None
        assert status.thesis_text == "latest thesis"
        assert status.latest_approved_resolution_id is not None

    def test_reference_group_thesis_text_used_without_pending_or_resolution(
        self, instance: CruxibleInstance
    ) -> None:
        signature = compute_group_signature("fits", {"rule_id": "manual", "rule_version": 1})
        group = CandidateGroup(
            group_id="GRP-reference001",
            relationship_type="fits",
            signature=signature,
            status="applying",
            thesis_text="reference thesis",
            thesis_facts={"rule_id": "manual", "rule_version": 1},
            proposed_by="agent",
            member_count=1,
            created_at=datetime.now(timezone.utc),
        )
        store = instance.get_group_store()
        try:
            with store.transaction():
                store.save_group(group)
        finally:
            store.close()

        status = service_group_status(instance, group_id=group.group_id)
        assert status.thesis_text == "reference thesis"
        assert status.pending_group_id is None
        assert status.latest_approved_resolution_id is None
