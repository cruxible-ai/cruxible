"""Tests for service_update_trust_status."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateMember, CandidateSignal
from cruxible_core.service import (
    service_propose_group,
    service_resolve_group,
    service_update_trust_status,
)

CONFIG_YAML = """\
version: "1.0"
name: trust_tests
description: For trust status tests

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

constraints: []
"""


@pytest.fixture
def instance(tmp_path: Path) -> CruxibleInstance:
    (tmp_path / "config.yaml").write_text(CONFIG_YAML)
    inst = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = inst.load_graph()
    for i in range(1, 6):
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id=f"BP-{i}",
                properties={
                    "part_number": f"BP-{i}",
                    "name": f"Part {i}",
                    "category": "brakes",
                },
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id=f"V-{i}",
                properties={
                    "vehicle_id": f"V-{i}",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
    inst.save_graph(graph)
    return inst


def _member(from_id="BP-1", to_id="V-1"):
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
    )


def _propose_and_approve(instance, from_id="BP-1", to_id="V-1", facts=None):
    """Propose and approve a group, returning the resolution_id."""
    f = facts or {"style": "casual"}
    pr = service_propose_group(instance, "fits", [_member(from_id, to_id)], thesis_facts=f)
    service_resolve_group(instance, pr.group_id, "approve", expected_pending_version=1)
    store = instance.get_group_store()
    try:
        group = store.get_group(pr.group_id)
        return group.resolution_id
    finally:
        store.close()


class TestUpdateTrustStatus:
    def test_watch_to_trusted(self, instance: CruxibleInstance) -> None:
        res_id = _propose_and_approve(instance)
        result = service_update_trust_status(instance, res_id, "trusted", "earned by review")
        assert result.resolution_id == res_id
        assert result.trust_status == "trusted"
        assert result.receipt_id is not None
        store = instance.get_group_store()
        try:
            res = store.get_resolution(res_id)
            assert res.trust_status == "trusted"
            assert res.trust_reason == "earned by review"
        finally:
            store.close()
        receipt_store = instance.get_receipt_store()
        try:
            receipt = receipt_store.get_receipt(result.receipt_id)
        finally:
            receipt_store.close()
        assert receipt is not None
        assert receipt.operation_type == "group_trust_update"
        assert receipt.committed is True
        validation = next(node for node in receipt.nodes if node.node_type == "validation")
        assert validation.detail["resolution_id"] == res_id
        assert validation.detail["previous_trust_status"] == "watch"
        assert validation.detail["new_trust_status"] == "trusted"
        assert validation.detail["reason"] == "earned by review"

    def test_trusted_to_invalidated(self, instance: CruxibleInstance) -> None:
        res_id = _propose_and_approve(instance)
        service_update_trust_status(instance, res_id, "trusted")
        service_update_trust_status(instance, res_id, "invalidated", "bad data found")
        store = instance.get_group_store()
        try:
            res = store.get_resolution(res_id)
            assert res.trust_status == "invalidated"
            assert res.trust_reason == "bad data found"
        finally:
            store.close()

    def test_not_found(self, instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="not found"):
            service_update_trust_status(instance, "RES-nonexistent", "trusted")

    def test_invalid_trust_status(self, instance: CruxibleInstance) -> None:
        with pytest.raises(ConfigError, match="Invalid trust_status"):
            service_update_trust_status(instance, "whatever", "bogus")

    def test_rejected_resolution_fails(self, instance: CruxibleInstance) -> None:
        """Cannot update trust on rejected resolutions."""
        pr = service_propose_group(instance, "fits", [_member()], thesis_facts={"k": "v"})
        service_resolve_group(instance, pr.group_id, "reject", expected_pending_version=1)
        store = instance.get_group_store()
        try:
            group = store.get_group(pr.group_id)
            res_id = group.resolution_id
        finally:
            store.close()

        with pytest.raises(ConfigError, match="approved resolutions"):
            service_update_trust_status(instance, res_id, "trusted")

    def test_unconfirmed_resolution_fails(self, instance: CruxibleInstance) -> None:
        """Cannot update trust on unconfirmed resolutions."""
        facts = {"k": "v"}
        sig = compute_group_signature("fits", facts)
        with instance.write_transaction() as uow:
            res_id = uow.groups.save_resolution(
                "fits",
                sig,
                "approve",
                "",
                "",
                facts,
                {},
                "human",
                confirmed=False,
            )

        with pytest.raises(ConfigError, match="confirmed resolutions"):
            service_update_trust_status(instance, res_id, "trusted")

    def test_non_latest_approval_fails(self, instance: CruxibleInstance) -> None:
        """Cannot update trust on an older approval for the same signature."""
        facts = {"style": "casual"}
        # First approval
        res_id_1 = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        # Second approval (same signature, different group)
        res_id_2 = _propose_and_approve(instance, "BP-2", "V-2", facts=facts)

        with pytest.raises(ConfigError, match="latest confirmed approval"):
            service_update_trust_status(instance, res_id_1, "trusted")

        # But latest is fine
        service_update_trust_status(instance, res_id_2, "trusted")

    def test_latest_guard_ignores_rejections(self, instance: CruxibleInstance) -> None:
        """A rejection between two approvals doesn't affect latest-approval check."""
        facts = {"style": "casual"}
        res_id_1 = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        # Reject with same signature
        pr2 = service_propose_group(instance, "fits", [_member("BP-2", "V-2")], thesis_facts=facts)
        service_resolve_group(instance, pr2.group_id, "reject", expected_pending_version=1)
        # res_id_1 is still the latest confirmed approval
        service_update_trust_status(instance, res_id_1, "trusted")
        store = instance.get_group_store()
        try:
            res = store.get_resolution(res_id_1)
            assert res.trust_status == "trusted"
        finally:
            store.close()


class TestTrustEffects:
    def test_invalidated_blocks_auto_resolve(self, instance: CruxibleInstance) -> None:
        """After invalidation, future proposals with same signature don't auto-resolve."""
        facts = {"style": "casual"}
        res_id = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        service_update_trust_status(instance, res_id, "trusted")
        service_update_trust_status(instance, res_id, "invalidated", "bad")

        pr = service_propose_group(instance, "fits", [_member("BP-3", "V-3")], thesis_facts=facts)
        assert pr.status == "pending_review"
        assert pr.review_priority == "critical"

    def test_promoted_enables_auto_resolve(self, instance: CruxibleInstance) -> None:
        """After promoting to trusted, future proposals auto-resolve."""
        facts = {"style": "casual"}
        res_id = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        service_update_trust_status(instance, res_id, "trusted", "reviewed")

        pr = service_propose_group(instance, "fits", [_member("BP-3", "V-3")], thesis_facts=facts)
        assert pr.status == "auto_resolved"

    def test_trust_compounds(self, instance: CruxibleInstance) -> None:
        """Promote to trusted, auto-resolve + approve second batch → inherits trusted."""
        facts = {"style": "casual"}
        res_id_1 = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        service_update_trust_status(instance, res_id_1, "trusted")

        # Second batch — will auto-resolve
        pr2 = service_propose_group(instance, "fits", [_member("BP-3", "V-3")], thesis_facts=facts)
        assert pr2.status == "auto_resolved"
        service_resolve_group(instance, pr2.group_id, "approve", expected_pending_version=1)

        store = instance.get_group_store()
        try:
            group2 = store.get_group(pr2.group_id)
            res2 = store.get_resolution(group2.resolution_id)
            assert res2.trust_status == "trusted"  # inherited
        finally:
            store.close()

    def test_trust_history_current_state_only(self, instance: CruxibleInstance) -> None:
        """watch → trusted → invalidated overwrites, intermediate states not preserved."""
        facts = {"style": "casual"}
        res_id = _propose_and_approve(instance, "BP-1", "V-1", facts=facts)
        service_update_trust_status(instance, res_id, "trusted", "initial review")
        service_update_trust_status(instance, res_id, "invalidated", "data issue")

        store = instance.get_group_store()
        try:
            res = store.get_resolution(res_id)
            assert res.trust_status == "invalidated"
            assert res.trust_reason == "data issue"
            # No trace of "trusted" or "initial review"
        finally:
            store.close()
