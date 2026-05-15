"""Group service functions — propose, resolve, list, trust."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast

from cruxible_core.config.property_validation import entity_properties_with_identity
from cruxible_core.config.schema import CoreConfig, ProposalPolicySchema, RelationshipSchema
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    GroupNotFoundError,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import validate_relationship
from cruxible_core.graph.types import (
    REJECTED_STATUSES,
    SYSTEM_OWNED_PROPERTIES,
    RelationshipInstance,
)
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    GroupResolution,
    GroupStatus,
    ReviewPriority,
    SignalBucketBasis,
    TrustStatus,
)
from cruxible_core.instance_protocol import GroupStoreProtocol, InstanceProtocol
from cruxible_core.primitives import canonical_json, new_id
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.service.mutation_receipts import MutationReceiptContext, mutation_receipt
from cruxible_core.service.mutations import service_add_relationships
from cruxible_core.service.types import (
    GetGroupResult,
    GroupMemberInput,
    GroupMemberReviewResult,
    GroupSignalInput,
    GroupStatusHistoryItem,
    GroupStatusResult,
    ListGroupsResult,
    ListResolutionsResult,
    PropertyDeltaResult,
    ProposeGroupResult,
    ResolveGroupResult,
    SuppressedProposalMember,
    UpdateTrustStatusResult,
)


@dataclass(frozen=True)
class _RelationshipKey:
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relationship_type: str

    @classmethod
    def from_member(cls, member: CandidateMember | RelationshipInstance) -> _RelationshipKey:
        return cls(
            from_type=member.from_type,
            from_id=member.from_id,
            to_type=member.to_type,
            to_id=member.to_id,
            relationship_type=member.relationship_type,
        )

    @classmethod
    def from_edge(cls, edge: dict[str, Any]) -> _RelationshipKey:
        return cls(
            from_type=edge["from_type"],
            from_id=edge["from_id"],
            to_type=edge["to_type"],
            to_id=edge["to_id"],
            relationship_type=edge["relationship_type"],
        )

    @classmethod
    def from_suppressed_member(
        cls,
        member: SuppressedProposalMember,
    ) -> _RelationshipKey:
        return cls(
            from_type=member.from_type,
            from_id=member.from_id,
            to_type=member.to_type,
            to_id=member.to_id,
            relationship_type=member.relationship_type,
        )

    @classmethod
    def from_store_tuple(
        cls,
        value: tuple[str, str, str, str, str],
    ) -> _RelationshipKey:
        from_type, from_id, to_type, to_id, relationship_type = value
        return cls(
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            relationship_type=relationship_type,
        )

    def as_store_tuple(self) -> tuple[str, str, str, str, str]:
        return (
            self.from_type,
            self.from_id,
            self.to_type,
            self.to_id,
            self.relationship_type,
        )

    def payload(self) -> dict[str, str]:
        return {
            "from_type": self.from_type,
            "from_id": self.from_id,
            "to_type": self.to_type,
            "to_id": self.to_id,
            "relationship_type": self.relationship_type,
        }

    def validation_args(self) -> dict[str, str]:
        return {
            "from_type": self.from_type,
            "from_id": self.from_id,
            "relationship": self.relationship_type,
            "to_type": self.to_type,
            "to_id": self.to_id,
        }

    def to_relationship_instance(self, *, properties: dict[str, Any]) -> RelationshipInstance:
        return RelationshipInstance(**self.payload(), properties=properties)

    def to_suppressed_member(
        self,
        *,
        reason: Literal["existing_edge", "pending_proposal"],
        group: CandidateGroup | None = None,
    ) -> SuppressedProposalMember:
        group_context: dict[str, str | None] = {}
        if group is not None:
            group_context = {
                "existing_group_id": group.group_id,
                "existing_group_status": group.status,
                "existing_signature": group.signature,
                "source_workflow_name": group.source_workflow_name,
            }
        return SuppressedProposalMember(
            **self.payload(),
            reason=reason,
            **group_context,
        )

    def label(self) -> str:
        return f"{self.from_type}:{self.from_id}->{self.to_type}:{self.to_id}"

    def relationship_label(self) -> str:
        return (
            f"{self.from_type}:{self.from_id} -[{self.relationship_type}]-> "
            f"{self.to_type}:{self.to_id}"
        )


@dataclass(frozen=True)
class _SuppressedMemberKey:
    relationship: _RelationshipKey
    reason: str

    @classmethod
    def from_member(cls, member: SuppressedProposalMember) -> _SuppressedMemberKey:
        return cls(
            relationship=_RelationshipKey.from_suppressed_member(member),
            reason=member.reason,
        )


@dataclass(frozen=True)
class _ProposalMetadata:
    thesis_text: str
    thesis_facts: dict[str, Any]
    analysis_state: dict[str, Any]
    signal_sources_used: list[str]
    proposed_by: Literal["human", "agent"]
    suggested_priority: str | None
    source_workflow_name: str | None
    source_workflow_receipt_id: str | None
    source_trace_ids: list[str]
    source_step_ids: list[str]


@dataclass(frozen=True)
class _MemberChanges:
    added: list[CandidateMember]
    removed: list[CandidateMember]


@dataclass(frozen=True)
class _ResolveTarget:
    group: CandidateGroup
    members: list[CandidateMember]
    is_retry: bool


@dataclass(frozen=True)
class _ApprovalValidation:
    valid_inputs: list[RelationshipInstance]
    edges_skipped: int
    skipped_existing: list[dict[str, str]]
    applied_tuples: list[dict[str, str]]
    validation_failures: int
    validation_errors: list[str]


_VALID_RESOLVE_ACTIONS = ("approve", "reject")
_VALID_RESOLVE_SOURCES = ("human", "agent")


def derive_review_priority(
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
) -> ReviewPriority:
    """Derive review_priority mechanically from universal states.

    Returns: "critical", "review", or "normal".
    Highest-severity bucket wins.
    """
    if proposal_policy is None:
        # No proposal policy → default to review (first-time, no guardrails).
        return "review" if prior_resolution is None else "normal"

    has_critical = False
    has_review = False

    # Check prior resolution trust
    if prior_resolution is not None:
        if prior_resolution.trust_status == "invalidated":
            has_critical = True
        elif prior_resolution.trust_status == "watch":
            has_review = True

    # Check signals on members
    for m in members:
        for sig in m.signals:
            icfg = proposal_policy.signals.get(sig.signal_source)
            if icfg is None:
                continue
            if icfg.role == "advisory":
                continue  # Advisory signals ignored for priority

            if sig.signal == "contradict" and icfg.role == "blocking":
                has_critical = True
            elif sig.signal == "unsure":
                if icfg.always_review_on_unsure:
                    has_review = True
                if icfg.role in ("blocking", "required"):
                    has_review = True

    # No prior approved resolution → review
    if prior_resolution is None:
        has_review = True

    if has_critical:
        return "critical"
    if has_review:
        return "review"
    return "normal"


def _relationship_key(member: CandidateMember | RelationshipInstance) -> _RelationshipKey:
    return _RelationshipKey.from_member(member)


def _relationship_payload(member: CandidateMember | RelationshipInstance) -> dict[str, str]:
    return _relationship_key(member).payload()


def _summarize_tuples(members: list[CandidateMember]) -> list[dict[str, str]]:
    return [_relationship_payload(member) for member in members]


def _member_label(member: CandidateMember | RelationshipInstance) -> str:
    return _relationship_key(member).label()


def _member_relationship_label(member: CandidateMember | RelationshipInstance) -> str:
    return _relationship_key(member).relationship_label()


def _suppressed_member_for_existing_edge(member: CandidateMember) -> SuppressedProposalMember:
    return _relationship_key(member).to_suppressed_member(reason="existing_edge")


def _suppressed_member_for_pending_group(
    member: CandidateMember,
    group: CandidateGroup,
) -> SuppressedProposalMember:
    return _relationship_key(member).to_suppressed_member(
        reason="pending_proposal",
        group=group,
    )


def _merge_suppressed_members(
    existing: list[SuppressedProposalMember],
    additions: list[SuppressedProposalMember],
) -> list[SuppressedProposalMember]:
    merged = {_SuppressedMemberKey.from_member(item): item for item in existing}
    for item in additions:
        merged.setdefault(_SuppressedMemberKey.from_member(item), item)
    return list(merged.values())


def _has_live_relationship(graph: EntityGraph, member: CandidateMember) -> bool:
    return graph.has_live_relationship(**_relationship_key(member).payload())


def _filter_relationship_key_conflicts(
    *,
    graph: EntityGraph,
    group_store: GroupStoreProtocol,
    relationship_type: str,
    members: list[CandidateMember],
    exclude_group_id: str | None,
) -> tuple[list[CandidateMember], list[SuppressedProposalMember]]:
    if not members:
        return [], []

    conflicts_by_store_key = group_store.find_pending_groups_for_tuples(
        relationship_type,
        [_relationship_key(member).as_store_tuple() for member in members],
        exclude_group_id=exclude_group_id,
        statuses=("pending_review", "applying"),
    )
    conflicts = {
        _RelationshipKey.from_store_tuple(key): group
        for key, group in conflicts_by_store_key.items()
    }
    retained: list[CandidateMember] = []
    suppressed: list[SuppressedProposalMember] = []
    for member in members:
        if _has_live_relationship(graph, member):
            suppressed.append(_suppressed_member_for_existing_edge(member))
            continue
        pending_group = conflicts.get(_relationship_key(member))
        if pending_group is not None:
            suppressed.append(_suppressed_member_for_pending_group(member, pending_group))
            continue
        retained.append(member)
    return retained, suppressed


def _merge_pending_members(
    existing_members: list[CandidateMember],
    current_members: list[CandidateMember],
) -> list[CandidateMember]:
    merged: dict[_RelationshipKey, CandidateMember] = {
        _relationship_key(member): member for member in existing_members
    }
    for member in current_members:
        merged[_relationship_key(member)] = member
    return list(merged.values())


def _member_changes(
    existing_members: list[CandidateMember],
    current_members: list[CandidateMember],
) -> _MemberChanges:
    existing_keys = {_relationship_key(member) for member in existing_members}
    current_keys = {_relationship_key(member) for member in current_members}
    return _MemberChanges(
        added=[
            member for member in current_members if _relationship_key(member) not in existing_keys
        ],
        removed=[
            member for member in existing_members if _relationship_key(member) not in current_keys
        ],
    )


def _has_active_override(graph: EntityGraph, member: CandidateMember) -> bool:
    relationship = graph.get_relationship(**_relationship_key(member).payload())
    if relationship is None:
        return False
    review_status = relationship.properties.get("review_status")
    if relationship.properties.get("group_override") is True:
        return True
    if review_status == "pending_review":
        return True
    return review_status in REJECTED_STATUSES


def _members_have_active_override(graph: EntityGraph, members: list[CandidateMember]) -> bool:
    return any(_has_active_override(graph, member) for member in members)


def _proposal_result(
    *,
    group_id: str | None,
    signature: str,
    status: GroupStatus | Literal["suppressed"],
    review_priority: ReviewPriority,
    member_count: int,
    prior_resolution: GroupResolution | None,
    suppressed_members: list[SuppressedProposalMember],
    policy_summary: dict[str, int],
    suppressed: bool = False,
) -> ProposeGroupResult:
    return ProposeGroupResult(
        group_id=group_id,
        signature=signature,
        status=status,
        review_priority=review_priority,
        member_count=member_count,
        prior_resolution=prior_resolution,
        suppressed=suppressed,
        suppressed_members=suppressed_members,
        policy_summary=policy_summary,
    )


def _group_update_fields(
    metadata: _ProposalMetadata,
    *,
    member_count: int,
    review_priority: ReviewPriority,
) -> dict[str, Any]:
    return {
        "status": "pending_review",
        "thesis_text": metadata.thesis_text,
        "thesis_facts": metadata.thesis_facts,
        "analysis_state": metadata.analysis_state,
        "signal_sources_used": metadata.signal_sources_used,
        "proposed_by": metadata.proposed_by,
        "member_count": member_count,
        "review_priority": review_priority,
        "suggested_priority": metadata.suggested_priority,
        "source_workflow_name": metadata.source_workflow_name,
        "source_workflow_receipt_id": metadata.source_workflow_receipt_id,
        "source_trace_ids": metadata.source_trace_ids,
        "source_step_ids": metadata.source_step_ids,
        "resolution_id": None,
    }


def _new_candidate_group(
    *,
    group_id: str,
    relationship_type: str,
    signature: str,
    status: GroupStatus,
    metadata: _ProposalMetadata,
    member_count: int,
    review_priority: ReviewPriority,
) -> CandidateGroup:
    return CandidateGroup(
        group_id=group_id,
        relationship_type=relationship_type,
        signature=signature,
        status=status,
        thesis_text=metadata.thesis_text,
        thesis_facts=metadata.thesis_facts,
        analysis_state=metadata.analysis_state,
        signal_sources_used=metadata.signal_sources_used,
        proposed_by=metadata.proposed_by,
        member_count=member_count,
        pending_version=1,
        review_priority=review_priority,
        suggested_priority=metadata.suggested_priority,
        source_workflow_name=metadata.source_workflow_name,
        source_workflow_receipt_id=metadata.source_workflow_receipt_id,
        source_trace_ids=metadata.source_trace_ids,
        source_step_ids=metadata.source_step_ids,
        created_at=datetime.now(timezone.utc),
    )


def _review_priority_for_members(
    *,
    graph: EntityGraph,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
    force_review: bool,
) -> ReviewPriority:
    review_priority = derive_review_priority(members, proposal_policy, prior_resolution)
    if force_review and review_priority == "normal":
        review_priority = "review"
    if _members_have_active_override(graph, members) and review_priority == "normal":
        review_priority = "review"
    return review_priority


def _should_auto_resolve(
    *,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
    force_review: bool,
    has_override: bool,
) -> bool:
    if force_review or has_override:
        return False
    if prior_resolution is None or prior_resolution.trust_status == "invalidated":
        return False
    if proposal_policy is None:
        return False

    trust_requirement = proposal_policy.auto_resolve_requires_prior_trust
    if trust_requirement == "trusted_only":
        trust_ok = prior_resolution.trust_status == "trusted"
    elif trust_requirement == "trusted_or_watch":
        trust_ok = prior_resolution.trust_status in ("trusted", "watch")
    else:
        trust_ok = False
    return trust_ok and _check_auto_resolve_signals(members, proposal_policy)


def _validate_group_proposal_inputs(
    *,
    rel_schema: RelationshipSchema,
    relationship_type: str,
    members: list[CandidateMember],
    thesis_facts: dict[str, Any],
) -> None:
    if not members:
        raise ConfigError("Members list must not be empty")

    for member in members:
        key = _relationship_key(member)
        if key.relationship_type != relationship_type:
            raise ConfigError(
                f"Member {key.from_id}\u2192{key.to_id} has relationship_type "
                f"'{key.relationship_type}' but group is for '{relationship_type}'"
            )
        if key.from_type != rel_schema.from_entity:
            raise ConfigError(
                f"Member {key.from_id} from_type '{key.from_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.from_entity}'"
            )
        if key.to_type != rel_schema.to_entity:
            raise ConfigError(
                f"Member {key.to_id} to_type '{key.to_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.to_entity}'"
            )

    try:
        canonical_json(thesis_facts)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"thesis_facts must be JSON-serializable: {exc}") from exc

    seen_members: set[_RelationshipKey] = set()
    for member in members:
        key = _relationship_key(member)
        if key in seen_members:
            raise ConfigError(
                f"Duplicate member: {key.from_type}:{key.from_id} \u2192 "
                f"{key.to_type}:{key.to_id} via {key.relationship_type}"
            )
        seen_members.add(key)


def _validate_proposal_signals(
    *,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    signal_sources_used: list[str],
) -> None:
    for member in members:
        key = _relationship_key(member)
        seen_signal_sources: set[str] = set()
        for signal in member.signals:
            if signal.signal_source in seen_signal_sources:
                raise ConfigError(
                    f"Member {key.from_id}\u2192{key.to_id} has duplicate signals "
                    f"from signal source '{signal.signal_source}'"
                )
            seen_signal_sources.add(signal.signal_source)

            if proposal_policy is not None and proposal_policy.signals:
                if signal.signal_source not in proposal_policy.signals:
                    declared = ", ".join(sorted(proposal_policy.signals.keys()))
                    raise ConfigError(
                        f"Signal from undeclared signal source '{signal.signal_source}'; "
                        f"declared: {declared}"
                    )

    if proposal_policy is None or not proposal_policy.signals:
        return

    for source_name, signal_config in proposal_policy.signals.items():
        if signal_config.role not in ("blocking", "required"):
            continue
        for member in members:
            key = _relationship_key(member)
            member_signal_sources = {signal.signal_source for signal in member.signals}
            if source_name not in member_signal_sources:
                raise ConfigError(
                    f"Member {key.from_id}\u2192{key.to_id} missing signal "
                    f"from {signal_config.role} signal source '{source_name}'"
                )

    for source_name in signal_sources_used:
        if source_name not in proposal_policy.signals:
            raise ConfigError(
                f"Signal source '{source_name}' not declared in proposal_policy.signals"
            )

    if len(members) > proposal_policy.max_group_size:
        raise ConfigError(
            f"Group size {len(members)} exceeds max_group_size "
            f"{proposal_policy.max_group_size}"
        )


def _clear_pending_group(
    *,
    instance: InstanceProtocol,
    group_store: GroupStoreProtocol,
    pending_group: CandidateGroup,
    old_members: list[CandidateMember],
    signature: str,
    prior_resolution: GroupResolution | None,
    suppressed_members: list[SuppressedProposalMember],
    policy_summary: dict[str, int],
) -> ProposeGroupResult:
    ctx: MutationReceiptContext[ProposeGroupResult]
    with mutation_receipt(
        instance,
        "group_clear",
        {
            "group_id": pending_group.group_id,
            "signature": signature,
            "final_version_before_clear": pending_group.pending_version,
        },
    ) as ctx:
        assert ctx.builder is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "group_id": pending_group.group_id,
                "signature": signature,
                "final_version_before_clear": pending_group.pending_version,
                "cleared_tuples": _summarize_tuples(old_members),
            },
        )
        with group_store.transaction():
            group_store.delete_group(pending_group.group_id)
        ctx.set_result(
            _proposal_result(
                group_id=None,
                signature=signature,
                status="suppressed",
                review_priority="review",
                member_count=0,
                prior_resolution=prior_resolution,
                suppressed=True,
                suppressed_members=suppressed_members,
                policy_summary=policy_summary,
            )
        )
    result = ctx.result
    assert result is not None
    return result


def _rewrite_pending_group(
    *,
    instance: InstanceProtocol,
    group_store: GroupStoreProtocol,
    pending_group: CandidateGroup,
    old_members: list[CandidateMember],
    pending_members: list[CandidateMember],
    metadata: _ProposalMetadata,
    signature: str,
    review_priority: ReviewPriority,
    prior_resolution: GroupResolution | None,
    suppressed_members: list[SuppressedProposalMember],
    policy_summary: dict[str, int],
) -> ProposeGroupResult:
    changes = _member_changes(old_members, pending_members)
    group = pending_group.model_copy(
        update={
            **_group_update_fields(
                metadata,
                member_count=len(pending_members),
                review_priority=review_priority,
            ),
            "pending_version": pending_group.pending_version + 1,
        }
    )

    ctx: MutationReceiptContext[ProposeGroupResult]
    with mutation_receipt(
        instance,
        "group_rewrite",
        {
            "group_id": pending_group.group_id,
            "signature": signature,
            "prior_version": pending_group.pending_version,
            "new_version": group.pending_version,
        },
    ) as ctx:
        assert ctx.builder is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "group_id": pending_group.group_id,
                "signature": signature,
                "prior_version": pending_group.pending_version,
                "new_version": group.pending_version,
                "added_tuples": _summarize_tuples(changes.added),
                "removed_tuples": _summarize_tuples(changes.removed),
            },
        )
        with group_store.transaction():
            group_store.save_group(group)
            group_store.replace_members(group.group_id, pending_members)
        ctx.set_result(
            _proposal_result(
                group_id=group.group_id,
                signature=signature,
                status="pending_review",
                review_priority=review_priority,
                member_count=len(pending_members),
                prior_resolution=prior_resolution,
                suppressed_members=suppressed_members,
                policy_summary=policy_summary,
            )
        )
    result = ctx.result
    assert result is not None
    return result


def _create_group_or_rewrite_concurrent(
    *,
    instance: InstanceProtocol,
    graph: EntityGraph,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    pending_members: list[CandidateMember],
    delta_members: list[CandidateMember],
    metadata: _ProposalMetadata,
    relationship_type: str,
    signature: str,
    pending_refresh_mode: Literal["replace", "retain_missing"],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
    force_review: bool,
    has_override: bool,
    status: GroupStatus,
    review_priority: ReviewPriority,
    suppressed_members: list[SuppressedProposalMember],
    policy_summary: dict[str, int],
) -> ProposeGroupResult:
    ctx: MutationReceiptContext[ProposeGroupResult]
    with mutation_receipt(
        instance,
        "group_propose",
        {
            "group_id": group.group_id,
            "signature": signature,
            "pending_version": 1,
            "member_count": len(pending_members),
            "member_tuples": _summarize_tuples(pending_members),
        },
    ) as ctx:
        assert ctx.builder is not None
        if has_override:
            ctx.builder.record_validation(
                passed=False,
                detail={"reason": "held_for_review_due_to_override"},
            )
        with group_store.transaction():
            try:
                group_store.save_group(group)
                group_store.save_members(group.group_id, pending_members)
            except sqlite3.IntegrityError:
                concurrent_pending = group_store.find_pending_group(
                    relationship_type,
                    signature,
                )
                if concurrent_pending is None:
                    raise
                concurrent_members = group_store.get_members(concurrent_pending.group_id)
                rewritten_members = pending_members
                if pending_refresh_mode == "retain_missing":
                    rewritten_members = _merge_pending_members(
                        concurrent_members,
                        delta_members,
                    )
                rewritten_review_priority = _review_priority_for_members(
                    graph=graph,
                    members=rewritten_members,
                    proposal_policy=proposal_policy,
                    prior_resolution=prior_resolution,
                    force_review=force_review,
                )
                changes = _member_changes(concurrent_members, rewritten_members)
                rewritten = concurrent_pending.model_copy(
                    update={
                        **_group_update_fields(
                            metadata,
                            member_count=len(rewritten_members),
                            review_priority=rewritten_review_priority,
                        ),
                        "pending_version": concurrent_pending.pending_version + 1,
                    }
                )
                group_store.save_group(rewritten)
                group_store.replace_members(rewritten.group_id, rewritten_members)
                ctx.builder.record_validation(
                    passed=True,
                    detail={
                        "race_resolved_as_rewrite": True,
                        "group_id": rewritten.group_id,
                        "prior_version": concurrent_pending.pending_version,
                        "new_version": rewritten.pending_version,
                        "added_tuples": _summarize_tuples(changes.added),
                        "removed_tuples": _summarize_tuples(changes.removed),
                    },
                )
                ctx.set_result(
                    _proposal_result(
                        group_id=rewritten.group_id,
                        signature=signature,
                        status="pending_review",
                        review_priority=rewritten_review_priority,
                        member_count=len(rewritten_members),
                        prior_resolution=prior_resolution,
                        suppressed_members=suppressed_members,
                        policy_summary=policy_summary,
                    )
                )
            else:
                ctx.set_result(
                    _proposal_result(
                        group_id=group.group_id,
                        signature=signature,
                        status=status,
                        review_priority=review_priority,
                        member_count=len(pending_members),
                        prior_resolution=prior_resolution,
                        suppressed_members=suppressed_members,
                        policy_summary=policy_summary,
                    )
                )

    result = ctx.result
    assert result is not None
    return result


def _candidate_signal_from_input(signal: GroupSignalInput) -> CandidateSignal:
    return CandidateSignal(
        signal_source=signal.signal_source,
        signal=signal.signal,
        evidence=signal.evidence,
        basis=(
            SignalBucketBasis.model_validate(signal.basis)
            if signal.basis is not None
            else None
        ),
    )


def _candidate_member_from_input(member: GroupMemberInput) -> CandidateMember:
    return CandidateMember(
        from_type=member.from_type,
        from_id=member.from_id,
        to_type=member.to_type,
        to_id=member.to_id,
        relationship_type=member.relationship_type,
        signals=[_candidate_signal_from_input(signal) for signal in member.signals],
        properties=member.properties,
    )


def service_propose_group_inputs(
    instance: InstanceProtocol,
    relationship_type: str,
    members: list[GroupMemberInput],
    thesis_text: str = "",
    thesis_facts: dict[str, Any] | None = None,
    pending_refresh_mode: Literal["replace", "retain_missing"] = "replace",
    analysis_state: dict[str, Any] | None = None,
    signal_sources_used: list[str] | None = None,
    proposed_by: Literal["human", "agent"] = "agent",
    suggested_priority: str | None = None,
    source_workflow_name: str | None = None,
    source_workflow_receipt_id: str | None = None,
    source_trace_ids: list[str] | None = None,
    source_step_ids: list[str] | None = None,
) -> ProposeGroupResult:
    """Normalize proposal input payloads, then propose a candidate group."""
    return service_propose_group(
        instance,
        relationship_type,
        [_candidate_member_from_input(member) for member in members],
        thesis_text=thesis_text,
        thesis_facts=thesis_facts,
        pending_refresh_mode=pending_refresh_mode,
        analysis_state=analysis_state,
        signal_sources_used=signal_sources_used,
        proposed_by=proposed_by,
        suggested_priority=suggested_priority,
        source_workflow_name=source_workflow_name,
        source_workflow_receipt_id=source_workflow_receipt_id,
        source_trace_ids=source_trace_ids,
        source_step_ids=source_step_ids,
    )


def service_propose_group(
    instance: InstanceProtocol,
    relationship_type: str,
    members: list[CandidateMember],
    thesis_text: str = "",
    thesis_facts: dict[str, Any] | None = None,
    pending_refresh_mode: Literal["replace", "retain_missing"] = "replace",
    analysis_state: dict[str, Any] | None = None,
    signal_sources_used: list[str] | None = None,
    proposed_by: Literal["human", "agent"] = "agent",
    suggested_priority: str | None = None,
    source_workflow_name: str | None = None,
    source_workflow_receipt_id: str | None = None,
    source_trace_ids: list[str] | None = None,
    source_step_ids: list[str] | None = None,
) -> ProposeGroupResult:
    """Propose a group of candidate edges for batch review/approval."""
    config = instance.load_config()
    thesis_facts = thesis_facts or {}
    analysis_state = analysis_state or {}
    signal_sources_used = signal_sources_used or []
    source_trace_ids = source_trace_ids or []
    source_step_ids = source_step_ids or []
    policy_summary: dict[str, int] = {}
    metadata = _ProposalMetadata(
        thesis_text=thesis_text,
        thesis_facts=thesis_facts,
        analysis_state=analysis_state,
        signal_sources_used=signal_sources_used,
        proposed_by=proposed_by,
        suggested_priority=suggested_priority,
        source_workflow_name=source_workflow_name,
        source_workflow_receipt_id=source_workflow_receipt_id,
        source_trace_ids=source_trace_ids,
        source_step_ids=source_step_ids,
    )

    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise ConfigError(f"Relationship type '{relationship_type}' not found in config")
    _validate_group_proposal_inputs(
        rel_schema=rel_schema,
        relationship_type=relationship_type,
        members=members,
        thesis_facts=thesis_facts,
    )

    graph = instance.load_graph()
    # Workflow policy accounting intentionally reflects the original proposal set.
    # Tuple-identity filtering below may remove members before the review group is stored.
    members, force_review = _apply_workflow_policies(
        config=config,
        graph=graph,
        relationship_type=relationship_type,
        members=members,
        workflow_name=source_workflow_name,
        thesis_facts=thesis_facts,
        policy_summary=policy_summary,
    )

    proposal_policy = rel_schema.proposal_policy

    if not thesis_facts:
        raise ConfigError("Governed proposals require non-empty thesis_facts")

    # 8. Compute signature
    signature = compute_group_signature(relationship_type, thesis_facts)
    group_store = instance.get_group_store()
    try:
        pending_group = group_store.find_pending_group(relationship_type, signature)
        old_members = (
            group_store.get_members(pending_group.group_id)
            if pending_group is not None
            else []
        )
        suppressed_members: list[SuppressedProposalMember] = []
        if rel_schema.proposal_identity == "relationship_tuple":
            members, suppressed_members = _filter_relationship_key_conflicts(
                graph=graph,
                group_store=group_store,
                relationship_type=relationship_type,
                members=members,
                exclude_group_id=(
                    pending_group.group_id if pending_group is not None else None
                ),
            )

        _validate_proposal_signals(
            members=members,
            proposal_policy=proposal_policy,
            signal_sources_used=signal_sources_used,
        )

        prior = group_store.find_resolution(
            relationship_type, signature, action="approve", confirmed=True
        )
        approved_store_tuples = group_store.list_approved_relationship_tuples(
            relationship_type,
            signature,
        )
        approved_keys = {
            _RelationshipKey.from_store_tuple(value) for value in approved_store_tuples
        }
        delta_members = [m for m in members if _relationship_key(m) not in approved_keys]
        if rel_schema.proposal_identity == "relationship_tuple":
            suppressed_members = _merge_suppressed_members(
                suppressed_members,
                [
                    _suppressed_member_for_existing_edge(member)
                    for member in members
                    if _relationship_key(member) in approved_keys
                ],
            )
        pending_members = delta_members
        if pending_group is not None and pending_refresh_mode == "retain_missing":
            pending_members = _merge_pending_members(old_members, delta_members)

        if not delta_members:
            if pending_group is None:
                return _proposal_result(
                    group_id=None,
                    signature=signature,
                    status="suppressed",
                    review_priority="review",
                    member_count=0,
                    prior_resolution=prior,
                    suppressed=True,
                    suppressed_members=suppressed_members,
                    policy_summary=policy_summary,
                )

            if pending_refresh_mode == "retain_missing":
                return _proposal_result(
                    group_id=pending_group.group_id,
                    signature=signature,
                    status="pending_review",
                    review_priority=pending_group.review_priority,
                    member_count=pending_group.member_count,
                    prior_resolution=prior,
                    suppressed_members=suppressed_members,
                    policy_summary=policy_summary,
                )

            return _clear_pending_group(
                instance=instance,
                group_store=group_store,
                pending_group=pending_group,
                old_members=old_members,
                signature=signature,
                prior_resolution=prior,
                suppressed_members=suppressed_members,
                policy_summary=policy_summary,
            )

        review_priority = _review_priority_for_members(
            graph=graph,
            members=pending_members,
            proposal_policy=proposal_policy,
            prior_resolution=prior,
            force_review=force_review,
        )
        has_override = _members_have_active_override(graph, delta_members)
        auto_resolve = pending_group is None and _should_auto_resolve(
            members=delta_members,
            proposal_policy=proposal_policy,
            prior_resolution=prior,
            force_review=force_review,
            has_override=has_override,
        )
        if auto_resolve:
            status: GroupStatus = "auto_resolved"
        else:
            status = "pending_review"

        if pending_group is not None:
            return _rewrite_pending_group(
                instance=instance,
                group_store=group_store,
                pending_group=pending_group,
                old_members=old_members,
                pending_members=pending_members,
                metadata=metadata,
                signature=signature,
                review_priority=review_priority,
                prior_resolution=prior,
                suppressed_members=suppressed_members,
                policy_summary=policy_summary,
            )

        group_id = new_id("GRP")
        group = _new_candidate_group(
            group_id=group_id,
            relationship_type=relationship_type,
            signature=signature,
            status=status,
            metadata=metadata,
            member_count=len(pending_members),
            review_priority=review_priority,
        )
        return _create_group_or_rewrite_concurrent(
            instance=instance,
            graph=graph,
            group_store=group_store,
            group=group,
            pending_members=pending_members,
            delta_members=delta_members,
            metadata=metadata,
            relationship_type=relationship_type,
            signature=signature,
            pending_refresh_mode=pending_refresh_mode,
            proposal_policy=proposal_policy,
            prior_resolution=prior,
            force_review=force_review,
            has_override=has_override,
            status=status,
            review_priority=review_priority,
            suppressed_members=suppressed_members,
            policy_summary=policy_summary,
        )
    finally:
        group_store.close()


def _apply_workflow_policies(
    *,
    config: CoreConfig,
    graph: EntityGraph,
    relationship_type: str,
    members: list[CandidateMember],
    workflow_name: str | None,
    thesis_facts: dict[str, Any],
    policy_summary: dict[str, int],
) -> tuple[list[CandidateMember], bool]:
    """Apply workflow-side decision policies to candidate members."""
    if workflow_name is None:
        return members, False

    policies = [
        policy
        for policy in config.decision_policies
        if policy.applies_to == "workflow"
        and policy.workflow_name == workflow_name
        and policy.relationship_type == relationship_type
        and not _policy_expired(policy.expires_at)
    ]
    if not policies:
        return members, False

    kept: list[CandidateMember] = []
    force_review = False
    for member in members:
        key = _relationship_key(member)
        from_entity = graph.get_entity(key.from_type, key.from_id)
        to_entity = graph.get_entity(key.to_type, key.to_id)
        matched_effects: list[str] = []
        for policy in policies:
            if from_entity is None or to_entity is None:
                continue
            from_props = entity_properties_with_identity(
                config, from_entity.entity_type, from_entity.entity_id, from_entity.properties
            )
            to_props = entity_properties_with_identity(
                config, to_entity.entity_type, to_entity.entity_id, to_entity.properties
            )
            if not matches_exact_filter(from_props, policy.match.from_match):
                continue
            if not matches_exact_filter(to_props, policy.match.to):
                continue
            if not matches_exact_filter(member.properties, policy.match.edge):
                continue
            if not matches_exact_filter(
                {
                    "workflow_name": workflow_name,
                    "relationship_type": relationship_type,
                    **thesis_facts,
                },
                policy.match.context,
            ):
                continue
            policy_summary[policy.name] = policy_summary.get(policy.name, 0) + 1
            matched_effects.append(policy.effect)

        if "suppress" in matched_effects:
            continue
        if "require_review" in matched_effects:
            force_review = True
        kept.append(member)
    return kept, force_review


def _policy_expired(expires_at: str | None) -> bool:
    """Return True when a workflow policy should no longer apply."""
    if not expires_at:
        return False
    try:
        normalized = expires_at.replace("Z", "+00:00")
        expiry = datetime.fromisoformat(normalized)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry < datetime.now(timezone.utc)
    except ValueError:
        return False


def _check_auto_resolve_signals(
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema,
) -> bool:
    """Check if signals meet the auto_resolve_when policy.

    Returns True if auto-resolve is eligible based on signals alone.
    """
    policy = proposal_policy.auto_resolve_when

    for m in members:
        for sig in m.signals:
            icfg = proposal_policy.signals.get(sig.signal_source)
            if icfg is None or icfg.role == "advisory":
                continue

            # always_review_on_unsure override
            if sig.signal == "unsure" and icfg.always_review_on_unsure:
                return False

            if policy == "all_support":
                if sig.signal != "support":
                    return False
            elif policy == "no_contradict":
                if sig.signal == "contradict" and icfg.role == "blocking":
                    return False

    return True


def _validate_resolve_request(
    *,
    action: str,
    resolved_by: str,
    expected_pending_version: int | None,
) -> None:
    if action not in _VALID_RESOLVE_ACTIONS:
        raise ConfigError(
            f"Invalid action '{action}'. Use: {', '.join(_VALID_RESOLVE_ACTIONS)}"
        )
    if resolved_by not in _VALID_RESOLVE_SOURCES:
        raise ConfigError(
            f"Invalid resolved_by '{resolved_by}'. Use: {', '.join(_VALID_RESOLVE_SOURCES)}"
        )
    if expected_pending_version is None:
        raise ConfigError("Resolve requires expected_pending_version")


def _load_group_for_resolve(
    group_store: GroupStoreProtocol,
    *,
    group_id: str,
    action: Literal["approve", "reject"],
) -> _ResolveTarget:
    group = group_store.get_group(group_id)
    if group is None:
        raise GroupNotFoundError(group_id)

    if group.status == "resolved":
        raise ConfigError("Group already resolved")
    if group.status == "applying" and action != "approve":
        raise ConfigError("Group is in applying state from a prior approve — cannot reject")

    return _ResolveTarget(
        group=group,
        members=group_store.get_members(group_id),
        is_retry=group.status == "applying",
    )


def _validate_resolve_pending_version(
    *,
    group: CandidateGroup,
    expected_pending_version: int,
) -> None:
    if group.pending_version != expected_pending_version:
        raise ConfigError(
            "Group changed during review; expected pending_version "
            f"{expected_pending_version}, found {group.pending_version}"
        )


def _reject_group(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    members: list[CandidateMember],
    rationale: str,
    resolved_by: Literal["human", "agent"],
    builder: ReceiptBuilder,
) -> ResolveGroupResult:
    builder.record_validation(
        passed=True,
        detail={
            "action": "reject",
            "members": len(members),
            "pending_version_at_resolve": group.pending_version,
        },
    )
    with group_store.transaction():
        resolution_id = group_store.save_resolution(
            group.relationship_type,
            group.signature,
            "reject",
            rationale,
            group.thesis_text,
            group.thesis_facts,
            group.analysis_state,
            resolved_by,
            trust_status="watch",
            confirmed=True,
        )
        group_store.update_group_status(
            group.group_id,
            "resolved",
            resolution_id=resolution_id,
        )
    return ResolveGroupResult(
        group_id=group.group_id,
        action="reject",
        edges_created=0,
        edges_skipped=0,
        resolution_id=resolution_id,
    )


def _validate_approval_members(
    *,
    config: CoreConfig,
    graph: EntityGraph,
    members: list[CandidateMember],
    builder: ReceiptBuilder,
) -> _ApprovalValidation:
    valid_inputs: list[RelationshipInstance] = []
    edges_skipped = 0
    skipped_existing: list[dict[str, str]] = []
    applied_tuples: list[dict[str, str]] = []
    validation_failures = 0
    validation_errors: list[str] = []

    for member in members:
        key = _relationship_key(member)
        count = graph.relationship_count_between(**key.payload())
        if count > 0:
            builder.record_validation(
                passed=False,
                detail={
                    "member": _member_label(member),
                    "reason": "edge_exists",
                },
            )
            edges_skipped += 1
            skipped_existing.append(_member_tuple_payload(member))
            continue

        try:
            validated = validate_relationship(
                config,
                graph,
                **key.validation_args(),
                properties=member.properties,
            )
        except DataValidationError as exc:
            builder.record_validation(
                passed=False,
                detail={
                    "member": _member_label(member),
                    "reason": "validation_failed",
                },
            )
            edges_skipped += 1
            validation_failures += 1
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            validation_errors.append(f"{_member_relationship_label(member)}: {detail}")
            continue

        builder.record_validation(
            passed=True,
            detail={"member": _member_label(member)},
        )
        valid_inputs.append(
            key.to_relationship_instance(properties=validated.relationship.properties)
        )
        applied_tuples.append(_member_tuple_payload(member))

    return _ApprovalValidation(
        valid_inputs=valid_inputs,
        edges_skipped=edges_skipped,
        skipped_existing=skipped_existing,
        applied_tuples=applied_tuples,
        validation_failures=validation_failures,
        validation_errors=validation_errors,
    )


def _inherited_trust_status(prior: GroupResolution | None) -> TrustStatus:
    if prior is not None and prior.trust_status in ("trusted", "watch"):
        return prior.trust_status
    return "watch"


def _start_approval_resolution(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    rationale: str,
    resolved_by: Literal["human", "agent"],
    is_retry: bool,
    validation: _ApprovalValidation,
) -> str:
    if is_retry:
        return cast(str, group.resolution_id)

    if not validation.valid_inputs and not validation.skipped_existing:
        if validation.validation_errors:
            raise DataValidationError(
                "Cannot approve group: candidate validation failed",
                errors=validation.validation_errors,
            )
        raise ConfigError("Cannot approve: no creatable edges")

    prior = group_store.find_resolution(
        group.relationship_type,
        group.signature,
        action="approve",
        confirmed=True,
    )
    with group_store.transaction():
        resolution_id = group_store.save_resolution(
            group.relationship_type,
            group.signature,
            "approve",
            rationale,
            group.thesis_text,
            group.thesis_facts,
            group.analysis_state,
            resolved_by,
            trust_status=_inherited_trust_status(prior),
            confirmed=False,
        )
        group_store.update_group_status(
            group.group_id,
            "applying",
            resolution_id=resolution_id,
        )
    return resolution_id


def _record_relationship_write_nodes(
    builder: ReceiptBuilder,
    relationships: list[RelationshipInstance],
) -> None:
    for relationship in relationships:
        builder.record_relationship_write(
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            relationship=relationship.relationship_type,
            is_update=False,
        )


def _apply_resolved_relationships(
    *,
    instance: InstanceProtocol,
    group_id: str,
    relationships: list[RelationshipInstance],
) -> int:
    if not relationships:
        return 0
    add_result = service_add_relationships(
        instance,
        relationships,
        source="group_resolve",
        source_ref=f"group:{group_id}",
        _create_receipt=False,
    )
    return add_result.added


def _revalidated_trust_status(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
) -> TrustStatus | None:
    prior = group_store.find_resolution(
        group.relationship_type,
        group.signature,
        action="approve",
        confirmed=True,
    )
    if prior is not None and prior.trust_status == "invalidated":
        return "watch"
    return None


def _confirm_approval_resolution(
    *,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    resolution_id: str,
) -> None:
    revalidated_trust = _revalidated_trust_status(
        group_store=group_store,
        group=group,
    )
    with group_store.transaction():
        group_store.confirm_resolution(
            resolution_id,
            trust_status=revalidated_trust,
        )
        group_store.update_group_status(group.group_id, "resolved")


def _approve_group(
    *,
    instance: InstanceProtocol,
    group_store: GroupStoreProtocol,
    group: CandidateGroup,
    members: list[CandidateMember],
    rationale: str,
    resolved_by: Literal["human", "agent"],
    is_retry: bool,
    builder: ReceiptBuilder,
) -> ResolveGroupResult:
    instance.invalidate_graph_cache()
    config = instance.load_config()
    graph = instance.load_graph()

    validation = _validate_approval_members(
        config=config,
        graph=graph,
        members=members,
        builder=builder,
    )
    resolution_id = _start_approval_resolution(
        group_store=group_store,
        group=group,
        rationale=rationale,
        resolved_by=resolved_by,
        is_retry=is_retry,
        validation=validation,
    )
    _record_relationship_write_nodes(builder, validation.valid_inputs)
    edges_created = _apply_resolved_relationships(
        instance=instance,
        group_id=group.group_id,
        relationships=validation.valid_inputs,
    )
    _confirm_approval_resolution(
        group_store=group_store,
        group=group,
        resolution_id=resolution_id,
    )
    builder.record_validation(
        passed=validation.validation_failures == 0,
        detail={
            "pending_version_at_resolve": group.pending_version,
            "resolution_id": resolution_id,
            "applied_tuples": validation.applied_tuples,
            "skipped_tuples_existing_edges": validation.skipped_existing,
        },
    )
    return ResolveGroupResult(
        group_id=group.group_id,
        action="approve",
        edges_created=edges_created,
        edges_skipped=validation.edges_skipped,
        resolution_id=resolution_id,
    )


def service_resolve_group(
    instance: InstanceProtocol,
    group_id: str,
    action: Literal["approve", "reject"],
    rationale: str = "",
    resolved_by: Literal["human", "agent"] = "human",
    expected_pending_version: int | None = None,
) -> ResolveGroupResult:
    """Resolve a candidate group — approve creates edges, reject records decision."""
    _validate_resolve_request(
        action=action,
        resolved_by=resolved_by,
        expected_pending_version=expected_pending_version,
    )
    assert expected_pending_version is not None

    group_store = instance.get_group_store()
    try:
        target = _load_group_for_resolve(
            group_store,
            group_id=group_id,
            action=action,
        )
    except Exception:
        group_store.close()
        raise

    ctx: MutationReceiptContext[ResolveGroupResult]
    with mutation_receipt(
        instance,
        "group_resolve",
        {
            "group_id": group_id,
            "action": action,
            "expected_pending_version": expected_pending_version,
        },
        store=group_store,
    ) as ctx:
        assert ctx.builder is not None
        _validate_resolve_pending_version(
            group=target.group,
            expected_pending_version=expected_pending_version,
        )
        if action == "reject":
            result = _reject_group(
                group_store=group_store,
                group=target.group,
                members=target.members,
                rationale=rationale,
                resolved_by=resolved_by,
                builder=ctx.builder,
            )
        else:
            result = _approve_group(
                instance=instance,
                group_store=group_store,
                group=target.group,
                members=target.members,
                rationale=rationale,
                resolved_by=resolved_by,
                is_retry=target.is_retry,
                builder=ctx.builder,
            )
        ctx.set_result(result)

    result = ctx.result
    assert result is not None
    return result


def _member_tuple_payload(member: CandidateMember) -> dict[str, str]:
    return _relationship_payload(member)


def _property_delta(
    proposed: dict[str, Any],
    current: dict[str, Any],
) -> PropertyDeltaResult:
    proposed = {
        key: value for key, value in proposed.items() if key not in SYSTEM_OWNED_PROPERTIES
    }
    current = {
        key: value for key, value in current.items() if key not in SYSTEM_OWNED_PROPERTIES
    }
    proposed_keys = set(proposed)
    current_keys = set(current)
    shared = proposed_keys & current_keys
    return PropertyDeltaResult(
        added=sorted(proposed_keys - current_keys),
        removed=sorted(current_keys - proposed_keys),
        changed=sorted(key for key in shared if proposed[key] != current[key]),
        unchanged=sorted(key for key in shared if proposed[key] == current[key]),
    )


def _current_edges_for_member(
    graph: EntityGraph,
    member: CandidateMember,
) -> list[dict[str, Any]]:
    member_key = _relationship_key(member)
    return [
        edge
        for edge in graph.iter_edges(relationship_type=member_key.relationship_type)
        if _RelationshipKey.from_edge(edge) == member_key
    ]


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
        raw_review_status = current_properties.get("review_status")
        current_review_status = (
            raw_review_status if isinstance(raw_review_status, str) else None
        )
        property_delta = _property_delta(proposed_properties, current_properties)
    elif not current_edges:
        property_delta = _property_delta(proposed_properties, {})
    else:
        property_delta = PropertyDeltaResult()

    return GroupMemberReviewResult(
        proposed_tuple=_member_tuple_payload(member),
        proposed_properties=proposed_properties,
        current_edge_count=len(current_edges),
        current_edge_key=current_edge_key,
        current_properties=current_properties,
        current_review_status=current_review_status,
        property_delta=property_delta,
    )


def service_get_group(
    instance: InstanceProtocol,
    group_id: str,
) -> GetGroupResult:
    """Load a candidate group with its members and resolution details."""
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
    bucket_status = service_group_status(instance, group_id=group_id)
    graph = instance.load_graph()
    return GetGroupResult(
        group=group,
        members=members,
        resolution=resolution,
        bucket_status=bucket_status,
        member_review=[_member_review_state(graph, member) for member in members],
    )


def service_group_status(
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


def service_list_groups(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    status: (Literal["pending_review", "auto_resolved", "applying", "resolved"] | None) = None,
    limit: int = 50,
) -> ListGroupsResult:
    """List candidate groups with optional filters, sorted by review_priority."""
    _VALID_STATUSES = ("pending_review", "auto_resolved", "applying", "resolved")
    if status is not None and status not in _VALID_STATUSES:
        raise ConfigError(f"Invalid status '{status}'. Use: {', '.join(_VALID_STATUSES)}")

    group_store = instance.get_group_store()
    try:
        groups = group_store.list_groups(
            relationship_type=relationship_type,
            status=status,
            limit=limit,
        )
        total = group_store.count_groups(
            relationship_type=relationship_type,
            status=status,
        )
        # Sort by review_priority descending (critical > review > normal)
        priority_order = {"critical": 0, "review": 1, "normal": 2}
        groups.sort(key=lambda g: priority_order.get(g.review_priority, 9))
        return ListGroupsResult(groups=groups, total=total)
    finally:
        group_store.close()


def service_list_resolutions(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    action: Literal["approve", "reject"] | None = None,
    limit: int = 50,
) -> ListResolutionsResult:
    """List resolutions — the reuse interface for agents querying prior analysis_state."""
    group_store = instance.get_group_store()
    try:
        resolutions = group_store.list_resolutions(
            relationship_type=relationship_type,
            action=action,
            limit=limit,
        )
        total = len(resolutions)
        return ListResolutionsResult(resolutions=resolutions, total=total)
    finally:
        group_store.close()


def service_update_trust_status(
    instance: InstanceProtocol,
    resolution_id: str,
    trust_status: Literal["trusted", "watch", "invalidated"],
    reason: str = "",
) -> UpdateTrustStatusResult:
    """Update trust_status on a confirmed approved resolution (thesis-scoped)."""
    _VALID = ("trusted", "watch", "invalidated")
    if trust_status not in _VALID:
        raise ConfigError(f"Invalid trust_status '{trust_status}'. Use: {', '.join(_VALID)}")

    group_store = instance.get_group_store()
    try:
        # 1. Load resolution
        res = group_store.get_resolution(resolution_id)
        if res is None:
            raise ConfigError(f"Resolution '{resolution_id}' not found")

        # 2. Approved-only guard
        if res.action != "approve":
            raise ConfigError("Trust status can only be set on approved resolutions")

        # 3. Confirmed guard
        if not res.confirmed:
            raise ConfigError(
                "Trust status can only be set on confirmed resolutions (group must be resolved)"
            )

        # 4. Latest-approval guard
        latest = group_store.find_resolution(
            res.relationship_type,
            res.group_signature,
            action="approve",
            confirmed=True,
        )
        if latest is None or latest.resolution_id != resolution_id:
            latest_id = latest.resolution_id if latest else "none"
            raise ConfigError(
                "Can only update trust on the latest confirmed approval "
                f"for this signature. Latest: {latest_id}"
            )

        ctx: MutationReceiptContext[UpdateTrustStatusResult]
        with mutation_receipt(
            instance,
            "group_trust_update",
            {
                "resolution_id": resolution_id,
                "trust_status": trust_status,
                "reason": reason,
            },
        ) as ctx:
            assert ctx.builder is not None
            ctx.builder.record_validation(
                passed=True,
                detail={
                    "resolution_id": resolution_id,
                    "relationship_type": res.relationship_type,
                    "signature": res.group_signature,
                    "previous_trust_status": res.trust_status,
                    "new_trust_status": trust_status,
                    "reason": reason,
                },
            )
            with group_store.transaction():
                group_store.update_resolution_trust_status(resolution_id, trust_status, reason)
            ctx.set_result(
                UpdateTrustStatusResult(
                    resolution_id=resolution_id,
                    trust_status=trust_status,
                )
            )
    finally:
        group_store.close()

    result = ctx.result
    assert result is not None
    return result
