"""Group service functions — propose, resolve, list, trust."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from cruxible_core.config.schema import ProposalPolicySchema
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.group.governance import (
    apply_workflow_policies,
    build_agent_proposal_signature_facts,
    filter_relationship_conflicts,
    member_changes,
    member_signal_sources,
    member_signature_scope,
    members_have_active_override,
    merge_pending_members,
    merge_suppressed_members,
    relationship_tuples_summary,
    review_priority_for_members,
    should_auto_resolve,
    suppressed_member_from_relationship,
    validate_group_proposal_inputs,
    validate_proposal_signals,
)
from cruxible_core.group.governance import (
    build_workflow_proposal_signature_facts as build_workflow_proposal_signature_facts,
)
from cruxible_core.group.governance import (
    derive_review_priority as derive_review_priority,
)
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    CandidateSignal,
    GroupResolution,
    GroupStatus,
    QuerySourceEvidence,
    ReviewPriority,
    SignalBucketBasis,
)
from cruxible_core.instance_protocol import GroupStoreProtocol, InstanceProtocol
from cruxible_core.primitives import canonical_json, new_id, ordered_unique
from cruxible_core.service.evidence import resolve_evidence_refs
from cruxible_core.service.group_read_models import (
    get_group_read_model,
    group_status_read_model,
    list_groups_read_model,
    list_resolutions_read_model,
)
from cruxible_core.service.group_transitions import (
    resolve_group_transition,
    update_trust_status_transition,
)
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import (
    GetGroupResult,
    GroupMemberInput,
    GroupSignalInput,
    GroupStatusResult,
    ListGroupsResult,
    ListResolutionsResult,
    ProposeGroupResult,
    ResolveGroupResult,
    SuppressedProposalMember,
    UpdateTrustStatusResult,
)
from cruxible_core.storage import StorageIntegrityError
from cruxible_core.temporal import utc_now


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
    source_query_receipt_ids: list[str]
    source_trace_ids: list[str]
    source_step_ids: list[str]
    proposed_actor_context: GovernedActorContext | None


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
        "source_query_receipt_ids": metadata.source_query_receipt_ids,
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
        source_query_receipt_ids=metadata.source_query_receipt_ids,
        source_trace_ids=metadata.source_trace_ids,
        source_step_ids=metadata.source_step_ids,
        proposed_actor_context=metadata.proposed_actor_context,
        created_at=utc_now(),
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
        assert ctx.uow is not None
        write_group_store = ctx.uow.groups
        ctx.builder.record_validation(
            passed=True,
            detail={
                "group_id": pending_group.group_id,
                "signature": signature,
                "final_version_before_clear": pending_group.pending_version,
                "cleared_tuples": relationship_tuples_summary(old_members),
            },
        )
        write_group_store.delete_group(pending_group.group_id)
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
    assert isinstance(result, ProposeGroupResult)
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
    changes = member_changes(old_members, pending_members)
    metadata = _metadata_with_source_query_receipts(
        metadata,
        _source_query_receipt_ids_from_members(pending_members),
    )
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
        assert ctx.uow is not None
        write_group_store = ctx.uow.groups
        ctx.builder.record_validation(
            passed=True,
            detail={
                "group_id": pending_group.group_id,
                "signature": signature,
                "prior_version": pending_group.pending_version,
                "new_version": group.pending_version,
                "added_tuples": relationship_tuples_summary(changes.added),
                "removed_tuples": relationship_tuples_summary(changes.removed),
            },
        )
        write_group_store.save_group(group)
        write_group_store.replace_members(group.group_id, pending_members)
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
    assert isinstance(result, ProposeGroupResult)
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
    with mutation_receipt(
        instance,
        "group_propose",
        {
            "group_id": group.group_id,
            "signature": signature,
            "pending_version": 1,
            "member_count": len(pending_members),
            "member_tuples": relationship_tuples_summary(pending_members),
        },
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        write_group_store = ctx.uow.groups
        if has_override:
            ctx.builder.record_validation(
                passed=False,
                detail={"reason": "held_for_review_due_to_override"},
            )
        try:
            write_group_store.save_group(group)
            write_group_store.save_members(group.group_id, pending_members)
        except StorageIntegrityError:
            concurrent_pending = write_group_store.find_pending_group(
                relationship_type,
                signature,
            )
            if concurrent_pending is None:
                raise
            concurrent_members = write_group_store.get_members(concurrent_pending.group_id)
            rewritten_members = pending_members
            if pending_refresh_mode == "retain_missing":
                rewritten_members = merge_pending_members(
                    concurrent_members,
                    delta_members,
                )
            rewritten_review_priority = review_priority_for_members(
                graph=graph,
                members=rewritten_members,
                proposal_policy=proposal_policy,
                prior_resolution=prior_resolution,
                force_review=force_review,
            )
            changes = member_changes(concurrent_members, rewritten_members)
            rewritten_metadata = _metadata_with_source_query_receipts(
                metadata,
                _source_query_receipt_ids_from_members(rewritten_members),
            )
            rewritten = concurrent_pending.model_copy(
                update={
                    **_group_update_fields(
                        rewritten_metadata,
                        member_count=len(rewritten_members),
                        review_priority=rewritten_review_priority,
                    ),
                    "pending_version": concurrent_pending.pending_version + 1,
                }
            )
            write_group_store.save_group(rewritten)
            write_group_store.replace_members(rewritten.group_id, rewritten_members)
            ctx.builder.record_validation(
                passed=True,
                detail={
                    "race_resolved_as_rewrite": True,
                    "group_id": rewritten.group_id,
                    "prior_version": concurrent_pending.pending_version,
                    "new_version": rewritten.pending_version,
                    "added_tuples": relationship_tuples_summary(changes.added),
                    "removed_tuples": relationship_tuples_summary(changes.removed),
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
    assert isinstance(result, ProposeGroupResult)
    return result


def _metadata_with_source_query_receipts(
    metadata: _ProposalMetadata,
    *receipt_id_groups: list[str],
) -> _ProposalMetadata:
    receipt_ids = ordered_unique(
        [
            *(receipt_id for group in receipt_id_groups for receipt_id in group),
            *metadata.source_query_receipt_ids,
        ]
    )
    if receipt_ids == metadata.source_query_receipt_ids:
        return metadata
    return replace(metadata, source_query_receipt_ids=receipt_ids)


def _source_query_receipt_ids_from_members(members: list[CandidateMember]) -> list[str]:
    receipt_ids: list[str] = []
    for member in members:
        for evidence in member.source_query_evidence:
            receipt_ids.append(evidence.query_receipt_id)
    return list(ordered_unique(receipt_ids))


def _candidate_signal_from_input(
    instance: InstanceProtocol,
    signal: GroupSignalInput,
    *,
    actor_context: GovernedActorContext | None = None,
) -> CandidateSignal:
    return CandidateSignal(
        signal_source=signal.signal_source,
        signal=signal.signal,
        evidence=signal.evidence,
        evidence_refs=resolve_evidence_refs(
            instance,
            evidence_refs=signal.evidence_refs,
            source_evidence=signal.source_evidence,
            actor_context=actor_context,
        ),
        basis=(
            SignalBucketBasis.model_validate(signal.basis) if signal.basis is not None else None
        ),
    )


def _candidate_member_from_input(
    instance: InstanceProtocol,
    member: GroupMemberInput,
    *,
    actor_context: GovernedActorContext | None = None,
) -> CandidateMember:
    return CandidateMember(
        from_type=member.from_type,
        from_id=member.from_id,
        to_type=member.to_type,
        to_id=member.to_id,
        relationship_type=member.relationship_type,
        signals=[
            _candidate_signal_from_input(
                instance,
                signal,
                actor_context=actor_context,
            )
            for signal in member.signals
        ],
        source_query_evidence=_query_source_evidence_from_input(member.source_query_evidence),
        evidence_refs=resolve_evidence_refs(
            instance,
            evidence_refs=member.evidence_refs,
            source_evidence=member.source_evidence,
            actor_context=actor_context,
        ),
        evidence_rationale=member.evidence_rationale,
        properties=member.properties,
    )


def _query_source_evidence_from_input(
    evidence: list[QuerySourceEvidence | dict[str, Any]],
) -> list[QuerySourceEvidence]:
    return [QuerySourceEvidence.model_validate(item) for item in evidence]


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
    source_query_receipt_ids: list[str] | None = None,
    source_trace_ids: list[str] | None = None,
    source_step_ids: list[str] | None = None,
    actor_context: GovernedActorContext | None = None,
) -> ProposeGroupResult:
    """Normalize proposal input payloads, then propose a candidate group."""
    return service_propose_group(
        instance,
        relationship_type,
        [
            _candidate_member_from_input(
                instance,
                member,
                actor_context=actor_context,
            )
            for member in members
        ],
        thesis_text=thesis_text,
        thesis_facts=thesis_facts,
        pending_refresh_mode=pending_refresh_mode,
        analysis_state=analysis_state,
        signal_sources_used=signal_sources_used,
        proposed_by=proposed_by,
        suggested_priority=suggested_priority,
        source_workflow_name=source_workflow_name,
        source_workflow_receipt_id=source_workflow_receipt_id,
        source_query_receipt_ids=source_query_receipt_ids,
        source_trace_ids=source_trace_ids,
        source_step_ids=source_step_ids,
        actor_context=actor_context,
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
    source_query_receipt_ids: list[str] | None = None,
    source_trace_ids: list[str] | None = None,
    source_step_ids: list[str] | None = None,
    actor_context: GovernedActorContext | None = None,
) -> ProposeGroupResult:
    """Propose a group of candidate edges for batch review/approval."""
    config = instance.load_config()
    caller_thesis_facts = thesis_facts or {}
    analysis_state = analysis_state or {}
    caller_signal_sources_used = ordered_unique(signal_sources_used or [])
    source_query_receipt_ids = ordered_unique(source_query_receipt_ids or [])
    source_trace_ids = source_trace_ids or []
    source_step_ids = source_step_ids or []
    policy_summary: dict[str, int] = {}
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise ConfigError(f"Relationship type '{relationship_type}' not found in config")

    try:
        canonical_json(caller_thesis_facts)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"thesis_facts must be JSON-serializable: {exc}") from exc

    if source_workflow_name is None:
        member_sources = member_signal_sources(members)
        unknown_caller_sources = [
            source for source in caller_signal_sources_used if source not in member_sources
        ]
        if unknown_caller_sources:
            raise ConfigError(
                "signal_sources_used contains sources not attached to any member "
                f"signal: {', '.join(unknown_caller_sources)}"
            )
        signal_sources_used = member_sources
        thesis_facts = build_agent_proposal_signature_facts(
            rel_schema=rel_schema,
            relationship_type=relationship_type,
            signal_sources_used=signal_sources_used,
            agent_scope=caller_thesis_facts,
            member_scope=member_signature_scope(members),
        )
    else:
        signal_sources_used = caller_signal_sources_used
        thesis_facts = caller_thesis_facts
    metadata = _ProposalMetadata(
        thesis_text=thesis_text,
        thesis_facts=thesis_facts,
        analysis_state=analysis_state,
        signal_sources_used=signal_sources_used,
        proposed_by=proposed_by,
        suggested_priority=suggested_priority,
        source_workflow_name=source_workflow_name,
        source_workflow_receipt_id=source_workflow_receipt_id,
        source_query_receipt_ids=source_query_receipt_ids,
        source_trace_ids=source_trace_ids,
        source_step_ids=source_step_ids,
        proposed_actor_context=actor_context,
    )

    validate_group_proposal_inputs(
        rel_schema=rel_schema,
        relationship_type=relationship_type,
        members=members,
        thesis_facts=thesis_facts,
    )

    graph = instance.load_graph()
    # Workflow policy accounting intentionally reflects the original proposal set.
    # Tuple-identity filtering below may remove members before the review group is stored.
    members, force_review = apply_workflow_policies(
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
            group_store.get_members(pending_group.group_id) if pending_group is not None else []
        )
        suppressed_members: list[SuppressedProposalMember] = []
        if rel_schema.proposal_identity == "relationship_tuple":
            members, suppressed_members = filter_relationship_conflicts(
                graph=graph,
                group_store=group_store,
                relationship_type=relationship_type,
                members=members,
                exclude_group_id=(pending_group.group_id if pending_group is not None else None),
            )

        validate_proposal_signals(
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
        delta_members = [
            member
            for member in members
            if member.as_relationship().identity_tuple() not in approved_store_tuples
        ]
        if rel_schema.proposal_identity == "relationship_tuple":
            suppressed_members = merge_suppressed_members(
                suppressed_members,
                [
                    suppressed_member_from_relationship(
                        member.as_relationship(), reason="existing_edge"
                    )
                    for member in members
                    if member.as_relationship().identity_tuple() in approved_store_tuples
                ],
            )
        pending_members = delta_members
        if pending_group is not None and pending_refresh_mode == "retain_missing":
            pending_members = merge_pending_members(old_members, delta_members)

        if not delta_members:
            if suppressed_members:
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

        review_priority = review_priority_for_members(
            graph=graph,
            members=pending_members,
            proposal_policy=proposal_policy,
            prior_resolution=prior,
            force_review=force_review,
        )
        has_override = members_have_active_override(graph, delta_members)
        auto_resolve = pending_group is None and should_auto_resolve(
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
        metadata = _metadata_with_source_query_receipts(
            metadata,
            _source_query_receipt_ids_from_members(pending_members),
        )
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


def service_resolve_group(
    instance: InstanceProtocol,
    group_id: str,
    action: Literal["approve", "reject"],
    rationale: str = "",
    resolved_by: Literal["human", "agent"] = "human",
    expected_pending_version: int | None = None,
    actor_context: GovernedActorContext | None = None,
) -> ResolveGroupResult:
    """Resolve a candidate group — approve creates edges, reject records decision."""
    return resolve_group_transition(
        instance,
        group_id,
        action,
        rationale=rationale,
        resolved_by=resolved_by,
        expected_pending_version=expected_pending_version,
        actor_context=actor_context,
    )


def service_get_group(
    instance: InstanceProtocol,
    group_id: str,
) -> GetGroupResult:
    """Load a candidate group with its members and resolution details."""
    return get_group_read_model(instance, group_id)


def service_group_status(
    instance: InstanceProtocol,
    *,
    group_id: str | None = None,
    signature: str | None = None,
) -> GroupStatusResult:
    """Return bucket-level lifecycle status for a concrete group or signature."""
    return group_status_read_model(instance, group_id=group_id, signature=signature)


def service_list_groups(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    status: (Literal["pending_review", "auto_resolved", "applying", "resolved"] | None) = None,
    limit: int = 50,
    offset: int = 0,
) -> ListGroupsResult:
    """List candidate groups with optional filters, sorted by review_priority."""
    return list_groups_read_model(
        instance,
        relationship_type=relationship_type,
        status=status,
        limit=limit,
        offset=offset,
    )


def service_list_resolutions(
    instance: InstanceProtocol,
    relationship_type: str | None = None,
    action: Literal["approve", "reject"] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ListResolutionsResult:
    """List resolutions — the reuse interface for agents querying prior analysis_state."""
    return list_resolutions_read_model(
        instance,
        relationship_type=relationship_type,
        action=action,
        limit=limit,
        offset=offset,
    )


def service_update_trust_status(
    instance: InstanceProtocol,
    resolution_id: str,
    trust_status: Literal["trusted", "watch", "invalidated"],
    reason: str = "",
    actor_context: GovernedActorContext | None = None,
) -> UpdateTrustStatusResult:
    """Update trust_status on a confirmed approved resolution (thesis-scoped)."""
    return update_trust_status_transition(
        instance,
        resolution_id,
        trust_status,
        reason=reason,
        actor_context=actor_context,
    )
