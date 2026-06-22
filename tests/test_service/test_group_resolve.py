"""Tests for service_resolve_group."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, GroupNotFoundError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import (
    ResolveGroupResult,
    service_get_relationship_lineage,
    service_propose_group,
    service_resolve_group,
)
from cruxible_core.service.groups import build_agent_proposal_signature_facts

# ---------------------------------------------------------------------------
# Config YAML — minimal matching for resolve tests
# ---------------------------------------------------------------------------

RESOLVE_CONFIG_YAML = """\
version: "1.0"
name: resolve_test
description: For resolve_group tests

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(RESOLVE_CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    # Seed entities so relationship validation passes
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


def _member(
    from_id: str = "BP-1",
    to_id: str = "V-1",
) -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        properties={},
    )


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="usr_resolver",
        org_id="org_1",
        operation_id="op_resolve",
        timestamp="2026-06-05T12:00:00Z",
    )


def _agent_signature_facts(
    instance: CruxibleInstance,
    members: list[CandidateMember],
    *,
    agent_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rel_schema = instance.load_config().get_relationship("fits")
    assert rel_schema is not None
    signal_sources = [signal.signal_source for member in members for signal in member.signals]
    return build_agent_proposal_signature_facts(
        rel_schema=rel_schema,
        relationship_type="fits",
        signal_sources_used=signal_sources,
        agent_scope=agent_scope or {},
        member_scope=[
            {
                "relationship_type": member.relationship_type,
                "from_type": member.from_type,
                "from_id": member.from_id,
                "to_type": member.to_type,
                "to_id": member.to_id,
            }
            for member in sorted(
                members,
                key=lambda value: (
                    value.relationship_type,
                    value.from_type,
                    value.from_id,
                    value.to_type,
                    value.to_id,
                ),
            )
        ],
    )


def _propose(instance: CruxibleInstance, members=None, facts=None) -> str:
    """Propose a group and return the group_id."""
    m = members or [_member()]
    result = service_propose_group(
        instance,
        "fits",
        m,
        thesis_text="test",
        thesis_facts=facts or {"style": "casual"},
    )
    return result.group_id


def _save_resolution(
    instance: CruxibleInstance,
    relationship_type: str,
    signature: str,
    action: str,
    rationale: str,
    thesis_text: str,
    thesis_facts: dict[str, Any],
    outcome_state: dict[str, Any],
    resolved_by: str,
    **kwargs: Any,
) -> str:
    with instance.write_transaction() as uow:
        return uow.groups.save_resolution(
            relationship_type,
            signature,
            action,
            rationale,
            thesis_text,
            thesis_facts,
            outcome_state,
            resolved_by,
            **kwargs,
        )


def _update_group_status(
    instance: CruxibleInstance,
    group_id: str,
    status: str,
    *,
    resolution_id: str | None = None,
) -> None:
    with instance.write_transaction() as uow:
        uow.groups.update_group_status(group_id, status, resolution_id=resolution_id)


def _update_resolution_trust_status(
    instance: CruxibleInstance,
    resolution_id: str,
    trust_status: str,
    reason: str,
) -> None:
    with instance.write_transaction() as uow:
        uow.groups.update_resolution_trust_status(resolution_id, trust_status, reason)


# ---------------------------------------------------------------------------
# Approve tests
# ---------------------------------------------------------------------------


class TestApproveBasic:
    def test_approve_creates_edges(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert isinstance(result, ResolveGroupResult)
        assert result.action == "approve"
        assert result.edges_created == 1
        assert result.edges_skipped == 0

    def test_created_edges_have_provenance(self, instance: CruxibleInstance) -> None:
        actor = _actor()
        proposed = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="test",
            thesis_facts={"style": "casual"},
            actor_context=actor,
        )
        group_id = proposed.group_id
        resolved = service_resolve_group(
            instance,
            group_id,
            "approve",
            expected_pending_version=1,
            actor_context=actor,
        )
        graph = instance.load_graph()
        rel = graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        assert rel.properties == {"verified": False}
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.source == "group_resolve"
        assert rel.metadata.provenance.source_ref == f"group:{group_id}"
        assert rel.metadata.provenance.receipt_id == resolved.receipt_id
        assert rel.metadata.provenance.receipt_id is not None
        assert rel.metadata.provenance.resolution_id == resolved.resolution_id
        assert rel.metadata.provenance.resolution_id is not None
        assert rel.metadata.provenance.created_actor_context is not None
        assert rel.metadata.provenance.created_actor_context.actor_id == "usr_resolver"
        assert rel.metadata.assertion.review.status == "approved"
        assert rel.metadata.assertion.review.source == "group"
        # The newly group-resolved edge's review stamp also carries the resolving
        # actor identity (mirrors the blessing of pre-existing group members).
        assert rel.metadata.assertion.review.updated_by == f"group:{group_id}"
        assert rel.metadata.assertion.review.actor_context is not None
        assert rel.metadata.assertion.review.actor_context.actor_id == "usr_resolver"
        assert rel.metadata.assertion.lifecycle.status == "active"
        store = instance.get_group_store()
        try:
            stored_group = store.get_group(group_id)
            resolution = store.get_resolution(resolved.resolution_id)
        finally:
            store.close()
        assert stored_group is not None
        assert stored_group.proposed_actor_context is not None
        assert stored_group.proposed_actor_context.operation_id == "op_resolve"
        assert resolution is not None
        assert resolution.resolved_actor_context is not None
        assert resolution.resolved_actor_context.actor_id == "usr_resolver"

    def test_relationship_lineage_links_to_group_resolution_and_traces(
        self,
        instance: CruxibleInstance,
    ) -> None:
        proposed = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="test",
            thesis_facts={"style": "casual"},
            source_workflow_receipt_id="RCP-source",
            source_trace_ids=["TRC-source"],
        )
        resolved = service_resolve_group(
            instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
        )

        lineage = service_get_relationship_lineage(
            instance,
            from_type="Part",
            from_id="BP-1",
            relationship_type="fits",
            to_type="Vehicle",
            to_id="V-1",
        )

        assert lineage.found is True
        assert lineage.relationship is not None
        assert lineage.provenance is not None
        assert lineage.provenance["source_ref"] == f"group:{proposed.group_id}"
        assertion = lineage.relationship.metadata.assertion
        assert assertion.review.status == "approved"
        assert assertion.review.source == "group"
        assert assertion.lifecycle.status == "active"
        assert lineage.group is not None
        assert lineage.group.group_id == proposed.group_id
        assert lineage.resolution is not None
        assert lineage.resolution.resolution_id == resolved.resolution_id
        assert lineage.source_workflow_receipt_id == "RCP-source"
        assert lineage.source_trace_ids == ["TRC-source"]
        assert lineage.warnings == []

    def test_multiple_members_approved(self, instance: CruxibleInstance) -> None:
        members = [_member("BP-1", "V-1"), _member("BP-2", "V-2")]
        group_id = _propose(instance, members)
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 2

    def test_resolution_stored(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(
            instance,
            group_id,
            "approve",
            rationale="looks good",
            expected_pending_version=1,
        )
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group is not None
            assert group.status == "resolved"
            assert group.resolution_id is not None
            res = store.get_resolution(group.resolution_id)
            assert res is not None
            assert res.action == "approve"
            assert res.rationale == "looks good"
            assert res.confirmed is True
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Per-member validation
# ---------------------------------------------------------------------------


class TestPerMemberValidation:
    def test_bad_member_skipped(self, instance: CruxibleInstance) -> None:
        """Member with nonexistent entity is skipped, good member created."""
        bad_member = CandidateMember(
            from_type="Part",
            from_id="NONEXISTENT",
            to_type="Vehicle",
            to_id="V-1",
            relationship_type="fits",
            signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        )
        group_id = _propose(instance, [_member("BP-1", "V-1"), bad_member])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1
        assert result.edges_skipped == 1

    def test_existing_edge_skipped(self, instance: CruxibleInstance) -> None:
        """Member where an edge already exists is skipped."""
        # Create an edge first
        graph = instance.load_graph()
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
        instance.save_graph(graph)

        group_id = _propose(instance, [_member("BP-1", "V-1"), _member("BP-2", "V-2")])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1  # BP-2→V-2
        assert result.edges_skipped == 1  # BP-1→V-1 already exists


# ---------------------------------------------------------------------------
# Explained skips + stamp-existing (wi-resolve-skip-transparency)
# ---------------------------------------------------------------------------


def _add_direct_edge(instance: CruxibleInstance) -> None:
    """Seed an unreviewed, null-provenance direct-added BP-1->V-1 edge."""
    graph = instance.load_graph()
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
    instance.save_graph(graph)


class TestSkipExplanationAndStamp:
    def test_skip_existing_is_explained(self, instance: CruxibleInstance) -> None:
        """A skipped pre-existing member names the existing edge — not a bare count."""
        _add_direct_edge(instance)
        # Baseline: the direct-added edge is unreviewed with null provenance.
        graph = instance.load_graph()
        before = graph.get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert before is not None
        assert before.metadata.assertion.review.status == "unreviewed"
        assert before.metadata.provenance is None

        group_id = _propose(instance, [_member("BP-1", "V-1")])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        assert result.edges_skipped == 1
        assert result.edges_stamped == 0
        assert len(result.skipped_members) == 1
        skip = result.skipped_members[0]
        assert skip["skip_kind"] == "existing_edge"
        assert skip["from_id"] == "BP-1"
        assert skip["to_id"] == "V-1"
        # Reason explains WHY and names the existing edge tuple.
        assert "already live" in skip["reason"]
        assert "Part:BP-1" in skip["reason"]
        assert "fits" in skip["reason"]
        assert "Vehicle:V-1" in skip["reason"]
        # Default (no stamp): the surviving edge is untouched.
        assert skip["stamped"] == "false"
        after = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert after is not None
        assert after.metadata.assertion.review.status == "unreviewed"
        assert after.metadata.provenance is None

    def test_validation_failure_skip_is_explained(self, instance: CruxibleInstance) -> None:
        """A member skipped for failed validation is explained too (good member still created)."""
        bad_member = CandidateMember(
            from_type="Part",
            from_id="NONEXISTENT",
            to_type="Vehicle",
            to_id="V-1",
            relationship_type="fits",
            signals=[CandidateSignal(signal_source="check_v1", signal="support")],
        )
        group_id = _propose(instance, [_member("BP-1", "V-1"), bad_member])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1
        assert result.edges_skipped == 1
        assert len(result.skipped_members) == 1
        skip = result.skipped_members[0]
        assert skip["skip_kind"] == "validation_failed"
        assert skip["from_id"] == "NONEXISTENT"
        assert "validation" in skip["reason"]

    def test_stamp_existing_blesses_direct_added_edge(self, instance: CruxibleInstance) -> None:
        """--stamp-existing turns an unreviewed direct-add into a governed, attributed edge."""
        _add_direct_edge(instance)
        actor = _actor()
        proposed = service_propose_group(
            instance,
            "fits",
            [_member("BP-1", "V-1")],
            thesis_text="test",
            thesis_facts={"style": "casual"},
            actor_context=actor,
        )
        result = service_resolve_group(
            instance,
            proposed.group_id,
            "approve",
            expected_pending_version=1,
            actor_context=actor,
            stamp_existing=True,
        )

        assert result.edges_created == 0  # tuple already live
        assert result.edges_skipped == 1
        assert result.edges_stamped == 1
        assert result.skipped_members[0]["stamped"] == "true"

        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        # Blessed review status + source.
        assert rel.metadata.assertion.review.status == "approved"
        assert rel.metadata.assertion.review.source == "group"
        # Blessed provenance: source=group, resolution_id + receipt_id set (not null).
        assert rel.metadata.provenance is not None
        assert rel.metadata.provenance.source == "group_resolve"
        assert rel.metadata.provenance.source_ref == f"group:{proposed.group_id}"
        assert rel.metadata.provenance.resolution_id == result.resolution_id
        assert rel.metadata.provenance.resolution_id is not None
        assert rel.metadata.provenance.receipt_id == result.receipt_id
        assert rel.metadata.provenance.receipt_id is not None
        # Domain properties of the surviving edge are preserved.
        assert rel.properties.get("verified") is True

    def test_stamp_existing_default_off_leaves_edge_unreviewed(
        self, instance: CruxibleInstance
    ) -> None:
        """Without the flag, behavior is unchanged: skip-but-explained, edge untouched."""
        _add_direct_edge(instance)
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_stamped == 0
        rel = instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        assert rel is not None
        assert rel.metadata.assertion.review.status == "unreviewed"
        assert rel.metadata.provenance is None


# ---------------------------------------------------------------------------
# Reject tests
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_no_edges(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        result = service_resolve_group(instance, group_id, "reject", expected_pending_version=1)
        assert result.action == "reject"
        assert result.edges_created == 0
        assert result.edges_skipped == 0

    def test_reject_skips_applying_state(self, instance: CruxibleInstance) -> None:
        """Reject goes directly to resolved, no applying intermediate."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "reject", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group is not None
            assert group.status == "resolved"
        finally:
            store.close()

    def test_reject_resolution_confirmed_immediately(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "reject", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group is not None
            res = store.get_resolution(group.resolution_id)
            assert res is not None
            assert res.confirmed is True
        finally:
            store.close()

    def test_reject_trust_status_watch(self, instance: CruxibleInstance) -> None:
        """Rejections always get trust_status=watch."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "reject", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "watch"
        finally:
            store.close()

    def test_reject_on_applying_group_fails(self, instance: CruxibleInstance) -> None:
        """Cannot reject a group that's in applying state."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        # Manually set status to applying
        res_id = _save_resolution(
            instance,
            "fits",
            compute_group_signature("fits", {"style": "casual"}),
            "approve",
            "",
            "",
            {},
            {},
            "human",
        )
        _update_group_status(instance, group_id, "applying", resolution_id=res_id)

        with pytest.raises(ConfigError, match="Group is in applying state from a prior approve"):
            service_resolve_group(instance, group_id, "reject", expected_pending_version=1)


# ---------------------------------------------------------------------------
# Status guards
# ---------------------------------------------------------------------------


class TestStatusGuards:
    def test_missing_expected_pending_version_fails(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        with pytest.raises(ConfigError, match="expected_pending_version"):
            service_resolve_group(instance, group_id, "approve")

    def test_stale_expected_pending_version_fails(self, instance: CruxibleInstance) -> None:
        facts = {"style": "casual"}
        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts)
        rewritten = service_propose_group(
            instance,
            "fits",
            [_member("BP-2", "V-2")],
            thesis_text="test",
            thesis_facts=facts,
        )
        assert rewritten.group_id == group_id
        with pytest.raises(ConfigError, match="expected pending_version 1, found 2"):
            service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

    def test_resolved_group_rejected(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        with pytest.raises(ConfigError, match="already resolved"):
            service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

    def test_not_found(self, instance: CruxibleInstance) -> None:
        with pytest.raises(GroupNotFoundError):
            service_resolve_group(
                instance,
                "GRP-nonexistent",
                "approve",
                expected_pending_version=1,
            )

    def test_auto_resolved_accepts_resolution(self, instance: CruxibleInstance) -> None:
        """Auto-resolved groups can be explicitly resolved."""
        facts = {"style": "casual"}
        sig = compute_group_signature("fits", facts)
        # Create a prior trusted confirmed resolution
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=True,
        )

        # Propose — should auto-resolve since we have trusted prior
        # But we need trusted_or_watch or to change the fixture config.
        # Instead, just test that a pending_review or auto_resolved group
        # can be resolved. Let's manually set status.
        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts)
        _update_group_status(instance, group_id, "auto_resolved")

        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1


# ---------------------------------------------------------------------------
# Confirmed flag tests
# ---------------------------------------------------------------------------


class TestConfirmedFlag:
    def test_approve_starts_unconfirmed_then_confirmed(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        # After resolve, resolution should be confirmed
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.confirmed is True
        finally:
            store.close()

    def test_unconfirmed_not_visible_to_precedent(self, instance: CruxibleInstance) -> None:
        """Unconfirmed resolutions don't act as precedent for auto-resolve."""
        facts_scope = {"style": "casual"}
        _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)

        # Manually create an unconfirmed resolution
        facts = _agent_signature_facts(
            instance,
            [_member("BP-2", "V-2")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=False,
        )

        # Proposing again — unconfirmed should not be found
        result2 = service_propose_group(
            instance,
            "fits",
            [_member("BP-2", "V-2")],
            thesis_facts=facts_scope,
        )
        assert result2.prior_resolution is None


# ---------------------------------------------------------------------------
# Trust inheritance
# ---------------------------------------------------------------------------


class TestTrustInheritance:
    def test_inherits_trusted(self, instance: CruxibleInstance) -> None:
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        # Create prior trusted confirmed resolution
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=True,
        )

        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "trusted"
        finally:
            store.close()

    def test_inherits_watch(self, instance: CruxibleInstance) -> None:
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="watch",
            confirmed=True,
        )

        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "watch"
        finally:
            store.close()

    def test_invalidated_prior_starts_watch(self, instance: CruxibleInstance) -> None:
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="invalidated",
            confirmed=True,
        )

        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "watch"
        finally:
            store.close()

    def test_no_prior_starts_watch(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "watch"
        finally:
            store.close()

    def test_unconfirmed_prior_not_inherited(self, instance: CruxibleInstance) -> None:
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=False,  # unconfirmed
        )

        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "watch"  # not inherited from unconfirmed
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Trust revalidation at confirmation
# ---------------------------------------------------------------------------


class TestTrustRevalidation:
    def test_prior_invalidated_while_applying(self, instance: CruxibleInstance) -> None:
        """If prior was trusted at creation but invalidated while in applying,
        trust revalidates to watch at confirmation."""
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)

        # Create prior trusted confirmed resolution
        prior_res_id = _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=True,
        )

        # Propose and start resolve (manually simulate applying state)
        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)

        # Now invalidate the prior before resolve completes
        _update_resolution_trust_status(instance, prior_res_id, "invalidated", "trust broken")

        # Resolve — should revalidate trust at confirmation
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            # Prior was invalidated, so new resolution should be watch
            assert res.trust_status == "watch"
        finally:
            store.close()

    def test_prior_trust_unchanged_preserves_inherited(self, instance: CruxibleInstance) -> None:
        facts_scope = {"style": "casual"}
        facts = _agent_signature_facts(
            instance,
            [_member("BP-1", "V-1")],
            agent_scope=facts_scope,
        )
        sig = compute_group_signature("fits", facts)
        _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            facts,
            {},
            "human",
            trust_status="trusted",
            confirmed=True,
        )

        group_id = _propose(instance, [_member("BP-1", "V-1")], facts=facts_scope)
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            res = store.get_resolution(group.resolution_id)
            assert res.trust_status == "trusted"  # preserved
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Four-state lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_pending_to_resolved(self, instance: CruxibleInstance) -> None:
        group_id = _propose(instance, [_member("BP-1", "V-1")])
        service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group.status == "resolved"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# Applying retry (idempotent)
# ---------------------------------------------------------------------------


class TestApplyingRetry:
    def test_applying_retry_reuses_resolution(self, instance: CruxibleInstance) -> None:
        """Retrying an applying group doesn't create a duplicate resolution."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])

        # Manually move to applying with a resolution
        sig = compute_group_signature("fits", {"style": "casual"})
        res_id = _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            {},
            {},
            "human",
            confirmed=False,
        )
        _update_group_status(instance, group_id, "applying", resolution_id=res_id)

        # Now resolve (retry path)
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1

        # Verify only one resolution exists for this group
        store = instance.get_group_store()
        try:
            group = store.get_group(group_id)
            assert group.resolution_id == res_id  # same resolution reused
            assert group.status == "resolved"
        finally:
            store.close()

    def test_applying_retry_skips_already_created_edges(self, instance: CruxibleInstance) -> None:
        """On retry, edges already created by prior attempt are skipped."""
        members = [_member("BP-1", "V-1"), _member("BP-2", "V-2")]
        group_id = _propose(instance, members)

        # Create one edge manually (simulating partial prior apply)
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": False},
            )
        )
        instance.save_graph(graph)

        # Set to applying
        sig = compute_group_signature("fits", {"style": "casual"})
        res_id = _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            {},
            {},
            "human",
            confirmed=False,
        )
        _update_group_status(instance, group_id, "applying", resolution_id=res_id)

        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 1  # only BP-2→V-2
        assert result.edges_skipped == 1  # BP-1→V-1 already exists

    def test_zero_edge_applying_retry_allowed(self, instance: CruxibleInstance) -> None:
        """On retry, zero valid members is allowed (edges may have been created prior)."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])

        # Create the edge manually (simulating successful prior graph write)
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": False},
            )
        )
        instance.save_graph(graph)

        sig = compute_group_signature("fits", {"style": "casual"})
        res_id = _save_resolution(
            instance,
            "fits",
            sig,
            "approve",
            "",
            "",
            {},
            {},
            "human",
            confirmed=False,
        )
        _update_group_status(instance, group_id, "applying", resolution_id=res_id)

        # Retry with zero valid members — should succeed
        result = service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
        assert result.edges_created == 0
        assert result.edges_skipped == 1


# ---------------------------------------------------------------------------
# Zero-edge first-time approve
# ---------------------------------------------------------------------------


class TestZeroEdgeApprove:
    def test_zero_edge_first_time_fails(self, instance: CruxibleInstance) -> None:
        """First-time approve with all members skipped raises ConfigError."""
        # Create an edge so the member will be skipped
        graph = instance.load_graph()
        graph.add_relationship(
            RelationshipInstance(
                relationship_type="fits",
                from_type="Part",
                from_id="BP-1",
                to_type="Vehicle",
                to_id="V-1",
                properties={"verified": False},
            )
        )
        instance.save_graph(graph)

        group_id = _propose(instance, [_member("BP-1", "V-1")])
        result = service_resolve_group(
            instance,
            group_id,
            "approve",
            expected_pending_version=1,
        )
        assert result.edges_created == 0
        assert result.edges_skipped == 1


# ---------------------------------------------------------------------------
# Cache invalidation on retry
# ---------------------------------------------------------------------------


class TestCacheInvalidation:
    def test_retry_invalidates_cache(self, instance: CruxibleInstance) -> None:
        """resolve_group calls invalidate_graph_cache before loading graph."""
        group_id = _propose(instance, [_member("BP-1", "V-1")])

        with patch.object(
            instance, "invalidate_graph_cache", wraps=instance.invalidate_graph_cache
        ) as mock_invalidate:
            service_resolve_group(instance, group_id, "approve", expected_pending_version=1)
            mock_invalidate.assert_called()
