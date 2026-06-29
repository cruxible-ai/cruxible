"""Read-only assembly for public governance service result models."""

from __future__ import annotations

from typing import Any, Literal

from cruxible_core.errors import ConfigError, GroupNotFoundError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.property_diffs import property_delta as build_property_delta
from cruxible_core.service.types import (
    GetGroupResult,
    GroupMemberReviewResult,
    GroupStatusHistoryItem,
    GroupStatusResult,
    ListGroupsResult,
    ListResolutionsResult,
    PropertyDeltaResult,
)


def get_group_read_model(
    instance: InstanceProtocol,
    group_id: str,
) -> GetGroupResult:
    """Load a candidate group with its members and resolution read model."""
    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(group_id)
        if group is None:
            raise GroupNotFoundError(group_id)
        members = group_store.get_members(group_id)
        resolution: GroupResolution | None = None
        if group.resolution_id is not None:
            resolution = group_store.get_resolution(group.resolution_id)
    finally:
        group_store.close()

    bucket_status = group_status_read_model(instance, group_id=group_id)
    graph = instance.load_graph()
    return GetGroupResult(
        group=group,
        members=members,
        resolution=resolution,
        bucket_status=bucket_status,
        member_review=[_member_review_state(graph, member) for member in members],
    )


def group_status_read_model(
    instance: InstanceProtocol,
    *,
    group_id: str | None = None,
    signature: str | None = None,
) -> GroupStatusResult:
    """Return bucket-level lifecycle status for a concrete group or signature."""
    if group_id is None and signature is None:
        raise ConfigError("Provide group_id or signature")

    group_store = instance.get_group_store()
    try:
        reference_group: CandidateGroup | None = None
        if group_id is not None:
            reference_group = group_store.get_group(group_id)
            if reference_group is None:
                raise GroupNotFoundError(group_id)
            signature = reference_group.signature

        assert signature is not None
        pending = None
        if reference_group is not None and reference_group.status == "pending_review":
            pending = reference_group
        else:
            if reference_group is not None:
                pending = group_store.find_pending_group(
                    reference_group.relationship_type,
                    signature,
                )
            if pending is None:
                pending_groups = group_store.list_groups(
                    signature=signature,
                    status="pending_review",
                    limit=1,
                )
                if pending_groups:
                    pending = pending_groups[0]
                    if reference_group is None:
                        reference_group = pending_groups[0]

        resolutions = group_store.list_resolutions(
            signature=signature,
            action="approve",
            confirmed=True,
            limit=200,
        )
        if reference_group is None:
            groups = group_store.list_groups(signature=signature, limit=1)
            if groups:
                reference_group = groups[0]
        if reference_group is None and resolutions:
            resolution_group = group_store.get_group_by_resolution(resolutions[0].resolution_id)
            if resolution_group is not None:
                reference_group = resolution_group
        if reference_group is None and not resolutions:
            raise ConfigError(f"No group or resolution found for signature '{signature}'")

        relationship_type = (
            reference_group.relationship_type
            if reference_group is not None
            else resolutions[0].relationship_type
        )
        accepted_store_tuples = group_store.list_approved_relationship_tuples(
            relationship_type,
            signature,
        )
        history: list[GroupStatusHistoryItem] = []
        for resolution in resolutions:
            resolution_group = group_store.get_group_by_resolution(resolution.resolution_id)
            tuple_count = resolution_group.member_count if resolution_group is not None else 0
            history.append(
                GroupStatusHistoryItem(
                    resolution_id=resolution.resolution_id,
                    action=resolution.action,
                    trust_status=resolution.trust_status,
                    confirmed=resolution.confirmed,
                    resolved_at=str(resolution.resolved_at),
                    tuple_count=tuple_count,
                )
            )

        thesis_text = ""
        thesis_facts: dict[str, Any] = {}
        if pending is not None:
            thesis_text = pending.thesis_text
            thesis_facts = pending.thesis_facts
        elif resolutions:
            thesis_text = resolutions[0].thesis_text
            thesis_facts = resolutions[0].thesis_facts
        elif reference_group is not None:
            thesis_text = reference_group.thesis_text
            thesis_facts = reference_group.thesis_facts

        latest_approved = resolutions[0] if resolutions else None
        return GroupStatusResult(
            signature=signature,
            relationship_type=relationship_type,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            latest_trust_status=latest_approved.trust_status if latest_approved else None,
            accepted_tuple_count=len(accepted_store_tuples),
            pending_delta_count=pending.member_count if pending is not None else 0,
            pending_group_id=pending.group_id if pending is not None else None,
            pending_version=pending.pending_version if pending is not None else None,
            latest_approved_resolution_id=(
                latest_approved.resolution_id if latest_approved is not None else None
            ),
            approved_history=history,
        )
    finally:
        group_store.close()


def list_groups_read_model(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    status: (Literal["pending_review", "auto_resolved", "applying", "resolved"] | None) = None,
    limit: int = 50,
    offset: int = 0,
) -> ListGroupsResult:
    """List candidate groups with optional filters, sorted by review priority."""
    valid_statuses = ("pending_review", "auto_resolved", "applying", "resolved")
    if status is not None and status not in valid_statuses:
        raise ConfigError(f"Invalid status '{status}'. Use: {', '.join(valid_statuses)}")

    group_store = instance.get_group_store()
    try:
        groups = group_store.list_groups(
            relationship_type=relationship_type,
            status=status,
            limit=limit,
            offset=offset,
            order_by="review_priority",
        )
        total = group_store.count_groups(
            relationship_type=relationship_type,
            status=status,
        )
        return ListGroupsResult(items=groups, total=total)
    finally:
        group_store.close()


def list_resolutions_read_model(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    action: Literal["approve", "reject"] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ListResolutionsResult:
    """List resolutions for agents querying prior analysis state."""
    group_store = instance.get_group_store()
    try:
        resolutions = group_store.list_resolutions(
            relationship_type=relationship_type,
            action=action,
            limit=limit,
            offset=offset,
        )
        total = group_store.count_resolutions(
            relationship_type=relationship_type,
            action=action,
        )
        return ListResolutionsResult(items=resolutions, total=total)
    finally:
        group_store.close()


def _current_edges_for_member(
    graph: EntityGraph,
    member: CandidateMember,
) -> list[dict[str, Any]]:
    relationship = member.as_relationship()
    current_edges: list[dict[str, Any]] = []
    for edge in graph.iter_edges(relationship_type=relationship.relationship_type):
        edge_relationship = RelationshipInstance.model_validate(edge)
        if edge_relationship.identity_tuple() == relationship.identity_tuple():
            current_edges.append(edge)
    return current_edges


def _member_review_state(
    graph: EntityGraph,
    member: CandidateMember,
) -> GroupMemberReviewResult:
    current_edges = _current_edges_for_member(graph, member)
    proposed_properties = dict(member.properties)
    current_properties: dict[str, Any] | None = None
    current_edge_key: int | None = None
    current_review_status: str | None = None
    if len(current_edges) == 1:
        current = current_edges[0]
        current_properties = dict(current["properties"])
        raw_edge_key = current.get("edge_key")
        current_edge_key = raw_edge_key if isinstance(raw_edge_key, int) else None
        current_review_status = (
            current.get("metadata", {}).get("assertion", {}).get("review", {}).get("status")
        )
        property_delta = build_property_delta(proposed_properties, current_properties)
    elif not current_edges:
        property_delta = build_property_delta(proposed_properties, {})
    else:
        property_delta = PropertyDeltaResult()

    return GroupMemberReviewResult(
        proposed_tuple=member.as_relationship().identity_payload(),
        proposed_properties=proposed_properties,
        current_edge_count=len(current_edges),
        current_edge_key=current_edge_key,
        current_properties=current_properties,
        current_review_status=current_review_status,
        property_delta=property_delta,
    )
