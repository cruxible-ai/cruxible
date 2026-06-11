"""Focused coverage for governance transition seams."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import MutationError, OwnershipError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    GroupResolution,
)
from cruxible_core.receipt.types import Receipt
from cruxible_core.service import (
    service_propose_group,
    service_resolve_group,
    service_update_trust_status,
)
from cruxible_core.snapshot.types import UpstreamMetadata

CONFIG_YAML = """\
version: "1.0"
name: group_state_transition_tests
description: Governance transition seam coverage

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

relationships:
  - name: fits
    from: Part
    to: Vehicle
    proposal_identity: relationship_tuple
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
    for index in range(1, 5):
        graph.add_entity(
            EntityInstance(
                entity_type="Part",
                entity_id=f"BP-{index}",
                properties={
                    "part_number": f"BP-{index}",
                    "name": f"Part {index}",
                    "category": "brakes",
                },
            )
        )
        graph.add_entity(
            EntityInstance(
                entity_type="Vehicle",
                entity_id=f"V-{index}",
                properties={
                    "vehicle_id": f"V-{index}",
                    "year": 2024,
                    "make": "Honda",
                    "model": "Civic",
                },
            )
        )
    inst.save_graph(graph)
    return inst


def _member(from_id: str = "BP-1", to_id: str = "V-1") -> CandidateMember:
    return CandidateMember(
        from_type="Part",
        from_id=from_id,
        to_type="Vehicle",
        to_id=to_id,
        relationship_type="fits",
        signals=[CandidateSignal(signal_source="check_v1", signal="support")],
    )


def _require_group_id(group_id: str | None) -> str:
    assert group_id is not None
    return group_id


def _require_resolution_id(resolution_id: str | None) -> str:
    assert resolution_id is not None
    return resolution_id


def _stored_group(instance: CruxibleInstance, group_id: str) -> CandidateGroup:
    store = instance.get_group_store()
    try:
        group = store.get_group(group_id)
        assert group is not None
        return group
    finally:
        store.close()


def _stored_resolution(
    instance: CruxibleInstance,
    resolution_id: str,
) -> GroupResolution:
    store = instance.get_group_store()
    try:
        resolution = store.get_resolution(resolution_id)
        assert resolution is not None
        return resolution
    finally:
        store.close()


def _stored_receipt(instance: CruxibleInstance, receipt_id: str) -> Receipt:
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
        assert receipt is not None
        return receipt
    finally:
        store.close()


def test_approval_transition_applies_edge_group_resolution_and_receipt(
    instance: CruxibleInstance,
) -> None:
    proposal = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "approval"},
    )

    result = service_resolve_group(
        instance,
        _require_group_id(proposal.group_id),
        "approve",
        expected_pending_version=1,
    )

    assert result.group_id == proposal.group_id
    assert result.action == "approve"
    assert result.edges_created == 1
    assert result.edges_skipped == 0
    assert result.resolution_id is not None
    assert result.receipt_id is not None

    group = _stored_group(instance, _require_group_id(proposal.group_id))
    assert group.status == "resolved"
    assert group.resolution_id == result.resolution_id
    resolution = _stored_resolution(instance, _require_resolution_id(result.resolution_id))
    assert resolution.action == "approve"
    assert resolution.confirmed is True
    receipt = _stored_receipt(instance, result.receipt_id)
    assert receipt.operation_type == "group_resolve"
    assert receipt.committed is True
    assert (
        instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        is not None
    )


def test_rejection_transition_resolves_without_edge_and_records_watch_trust(
    instance: CruxibleInstance,
) -> None:
    proposal = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "reject"},
    )

    result = service_resolve_group(
        instance,
        _require_group_id(proposal.group_id),
        "reject",
        rationale="not enough evidence",
        expected_pending_version=1,
    )

    assert result.action == "reject"
    assert result.edges_created == 0
    assert result.edges_skipped == 0
    group = _stored_group(instance, _require_group_id(proposal.group_id))
    assert group.status == "resolved"
    assert group.resolution_id == result.resolution_id
    resolution = _stored_resolution(instance, _require_resolution_id(result.resolution_id))
    assert resolution.trust_status == "watch"
    assert (
        instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        is None
    )


def test_suppression_transition_keeps_public_shape_and_reasons(
    instance: CruxibleInstance,
) -> None:
    first = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "suppress"},
    )
    second = service_propose_group(
        instance,
        "fits",
        [_member(), _member("BP-2", "V-2")],
        thesis_facts={"scope": "suppress-other"},
    )

    assert second.group_id != first.group_id
    assert second.status == "pending_review"
    assert second.suppressed is False
    assert len(second.suppressed_members) == 1
    suppressed = second.suppressed_members[0]
    assert suppressed.reason == "pending_proposal"
    assert suppressed.existing_group_id == first.group_id
    assert suppressed.existing_group_status == "pending_review"
    assert suppressed.existing_signature == first.signature


def test_trust_update_transition_receipt_and_downstream_auto_resolve(
    instance: CruxibleInstance,
) -> None:
    first = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "trusted"},
    )
    approved = service_resolve_group(
        instance,
        _require_group_id(first.group_id),
        "approve",
        expected_pending_version=1,
    )

    updated = service_update_trust_status(
        instance,
        _require_resolution_id(approved.resolution_id),
        "trusted",
        "manual review",
    )

    assert updated.receipt_id is not None
    assert updated.trust_status == "trusted"
    receipt = _stored_receipt(instance, updated.receipt_id)
    validation = next(node for node in receipt.nodes if node.node_type == "validation")
    assert validation.detail["previous_trust_status"] == "watch"
    assert validation.detail["new_trust_status"] == "trusted"
    assert validation.detail["reason"] == "manual review"

    second = service_propose_group(
        instance,
        "fits",
        [_member("BP-2", "V-2")],
        thesis_facts={"scope": "trusted"},
    )
    assert second.status == "auto_resolved"
    assert second.prior_resolution is not None
    assert second.prior_resolution.trust_status == "trusted"


def test_approval_transition_rolls_back_group_resolution_graph_and_success_receipt(
    instance: CruxibleInstance,
) -> None:
    proposal = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "rollback"},
    )

    with (
        patch(
            "cruxible_core.service.group_transitions._apply_resolved_relationships",
            side_effect=RuntimeError("graph replay failed"),
        ),
        pytest.raises(MutationError, match="Unexpected failure: graph replay failed"),
    ):
        service_resolve_group(
            instance,
            _require_group_id(proposal.group_id),
            "approve",
            expected_pending_version=1,
        )

    group = _stored_group(instance, _require_group_id(proposal.group_id))
    assert group.status == "pending_review"
    assert group.resolution_id is None
    store = instance.get_group_store()
    try:
        assert store.list_resolutions(relationship_type="fits") == []
    finally:
        store.close()
    assert (
        instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        is None
    )
    receipt_store = instance.get_receipt_store()
    try:
        receipts = receipt_store.list_receipts(operation_type="group_resolve")
        assert len(receipts) == 1
        receipt = receipt_store.get_receipt(receipts[0]["receipt_id"])
        assert receipt is not None
        assert receipt.committed is False
    finally:
        receipt_store.close()


def test_approval_rejects_upstream_owned_relationship_type_and_rolls_back(
    instance: CruxibleInstance,
) -> None:
    instance.set_upstream_metadata(
        UpstreamMetadata(
            transport_ref="file:///tmp/reference-world",
            state_id="reference-world",
            release_id="v1",
            snapshot_id="snap-1",
            compatibility="data_only",
            owned_relationship_types=["fits"],
        )
    )
    proposal = service_propose_group(
        instance,
        "fits",
        [_member()],
        thesis_facts={"scope": "ownership"},
    )

    with pytest.raises(OwnershipError, match="upstream-owned relationship types: fits"):
        service_resolve_group(
            instance,
            _require_group_id(proposal.group_id),
            "approve",
            expected_pending_version=1,
        )

    group = _stored_group(instance, _require_group_id(proposal.group_id))
    assert group.status == "pending_review"
    assert group.resolution_id is None
    store = instance.get_group_store()
    try:
        assert store.list_resolutions(relationship_type="fits") == []
    finally:
        store.close()
    assert (
        instance.load_graph().get_relationship("Part", "BP-1", "Vehicle", "V-1", "fits")
        is None
    )
    receipt_store = instance.get_receipt_store()
    try:
        receipts = receipt_store.list_receipts(operation_type="group_resolve")
        assert len(receipts) == 1
        receipt = receipt_store.get_receipt(receipts[0]["receipt_id"])
        assert receipt is not None
        assert receipt.committed is False
    finally:
        receipt_store.close()
