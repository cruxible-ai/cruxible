"""Group service functions — propose, resolve, list, trust."""

from __future__ import annotations

import json as _json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from cruxible_core.config.property_validation import entity_properties_with_identity
from cruxible_core.config.schema import MatchingSchema
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    GroupNotFoundError,
)
from cruxible_core.graph.operations import validate_relationship
from cruxible_core.graph.types import REJECTED_STATUSES, RelationshipInstance
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateGroup, CandidateMember, GroupResolution
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.service._helpers import MutationReceiptContext, mutation_receipt
from cruxible_core.service.mutations import service_add_relationships
from cruxible_core.service.types import (
    GetGroupResult,
    GroupStatusHistoryItem,
    GroupStatusResult,
    ListGroupsResult,
    ListResolutionsResult,
    ProposeGroupResult,
    ResolveGroupResult,
)

RelationshipTuple = tuple[str, str, str, str, str]


def derive_review_priority(
    members: list[CandidateMember],
    matching: MatchingSchema | None,
    prior_resolution: GroupResolution | None,
) -> str:
    """Derive review_priority mechanically from universal states.

    Returns: "critical", "review", or "normal".
    Highest-severity bucket wins.
    """
    if matching is None:
        # No matching config → default to review (first-time, no guardrails)
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
            icfg = matching.integrations.get(sig.integration)
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


def _relationship_tuple(member: CandidateMember | RelationshipInstance) -> RelationshipTuple:
    return (
        member.from_type,
        member.from_id,
        member.to_type,
        member.to_id,
        member.relationship_type,
    )


def _summarize_tuples(members: list[CandidateMember]) -> list[dict[str, str]]:
    return [
        {
            "from_type": member.from_type,
            "from_id": member.from_id,
            "to_type": member.to_type,
            "to_id": member.to_id,
            "relationship_type": member.relationship_type,
        }
        for member in members
    ]


def _merge_pending_members(
    existing_members: list[CandidateMember],
    current_members: list[CandidateMember],
) -> list[CandidateMember]:
    merged: dict[RelationshipTuple, CandidateMember] = {
        _relationship_tuple(member): member for member in existing_members
    }
    for member in current_members:
        merged[_relationship_tuple(member)] = member
    return list(merged.values())


def _has_active_override(instance: InstanceProtocol, member: CandidateMember) -> bool:
    graph = instance.load_graph()
    relationship = graph.get_relationship(
        member.from_type,
        member.from_id,
        member.to_type,
        member.to_id,
        member.relationship_type,
    )
    if relationship is None:
        return False
    review_status = relationship.properties.get("review_status")
    if relationship.properties.get("group_override") is True:
        return True
    if review_status == "pending_review":
        return True
    return review_status in REJECTED_STATUSES


def service_propose_group(
    instance: InstanceProtocol,
    relationship_type: str,
    members: list[CandidateMember],
    thesis_text: str = "",
    thesis_facts: dict[str, Any] | None = None,
    pending_refresh_mode: Literal["replace", "retain_missing"] = "replace",
    analysis_state: dict[str, Any] | None = None,
    integrations_used: list[str] | None = None,
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
    integrations_used = integrations_used or []
    source_trace_ids = source_trace_ids or []
    source_step_ids = source_step_ids or []
    policy_summary: dict[str, int] = {}

    # 1. Validate relationship_type
    rel_schema = config.get_relationship(relationship_type)
    if rel_schema is None:
        raise ConfigError(f"Relationship type '{relationship_type}' not found in config")

    # 2. Validate members not empty
    if not members:
        raise ConfigError("Members list must not be empty")

    # 3. Validate each member
    for m in members:
        if m.relationship_type != relationship_type:
            raise ConfigError(
                f"Member {m.from_id}\u2192{m.to_id} has relationship_type "
                f"'{m.relationship_type}' but group is for '{relationship_type}'"
            )
        if m.from_type != rel_schema.from_entity:
            raise ConfigError(
                f"Member {m.from_id} from_type '{m.from_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.from_entity}'"
            )
        if m.to_type != rel_schema.to_entity:
            raise ConfigError(
                f"Member {m.to_id} to_type '{m.to_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.to_entity}'"
            )

    # 4. thesis_facts serialization check
    try:
        _json.dumps(thesis_facts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except TypeError as exc:
        raise ConfigError(f"thesis_facts must be JSON-serializable: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"thesis_facts must be JSON-serializable: {exc}") from exc

    # 5. Duplicate member check
    seen_members: set[tuple[str, str, str, str, str]] = set()
    for m in members:
        key = (m.from_type, m.from_id, m.to_type, m.to_id, m.relationship_type)
        if key in seen_members:
            raise ConfigError(
                f"Duplicate member: {m.from_type}:{m.from_id} \u2192 "
                f"{m.to_type}:{m.to_id} via {m.relationship_type}"
            )
        seen_members.add(key)

    graph = instance.load_graph()
    members, force_review = _apply_workflow_policies(
        config=config,
        graph=graph,
        relationship_type=relationship_type,
        members=members,
        workflow_name=source_workflow_name,
        thesis_facts=thesis_facts,
        policy_summary=policy_summary,
    )

    matching = rel_schema.matching

    # 6. Signal validation (all members)
    for m in members:
        seen_integrations: set[str] = set()
        for sig in m.signals:
            if sig.integration in seen_integrations:
                raise ConfigError(
                    f"Member {m.from_id}\u2192{m.to_id} has duplicate signals "
                    f"from integration '{sig.integration}'"
                )
            seen_integrations.add(sig.integration)

            if matching is not None and matching.integrations:
                if sig.integration not in matching.integrations:
                    declared = ", ".join(sorted(matching.integrations.keys()))
                    raise ConfigError(
                        f"Signal from undeclared integration '{sig.integration}'; "
                        f"declared: {declared}"
                    )

    # 7. Config guardrails (if matching section exists)
    if matching is not None and matching.integrations:
        # Blocking + required integrations: every member must have a signal
        for iname, icfg in matching.integrations.items():
            if icfg.role in ("blocking", "required"):
                for m in members:
                    member_integrations = {s.integration for s in m.signals}
                    if iname not in member_integrations:
                        raise ConfigError(
                            f"Member {m.from_id}\u2192{m.to_id} missing signal "
                            f"from {icfg.role} integration '{iname}'"
                        )

        # integrations_used validation
        for iname in integrations_used:
            if iname not in matching.integrations:
                raise ConfigError(f"Integration '{iname}' not declared in matching.integrations")

        # max_group_size
        if len(members) > matching.max_group_size:
            raise ConfigError(
                f"Group size {len(members)} exceeds max_group_size {matching.max_group_size}"
            )

    if not thesis_facts:
        raise ConfigError("Governed proposals require non-empty thesis_facts")

    # 8. Compute signature
    signature = compute_group_signature(relationship_type, thesis_facts)
    group_store = instance.get_group_store()
    try:
        prior = group_store.find_resolution(
            relationship_type, signature, action="approve", confirmed=True
        )
        approved_tuples = group_store.list_approved_relationship_tuples(
            relationship_type,
            signature,
        )
        pending_group = group_store.find_pending_group(relationship_type, signature)
        delta_members = [m for m in members if _relationship_tuple(m) not in approved_tuples]
        old_members = (
            group_store.get_members(pending_group.group_id)
            if pending_group is not None
            else []
        )
        pending_members = delta_members
        if pending_group is not None and pending_refresh_mode == "retain_missing":
            pending_members = _merge_pending_members(old_members, delta_members)

        if not delta_members:
            if pending_group is None:
                return ProposeGroupResult(
                    group_id=None,
                    signature=signature,
                    status="suppressed",
                    review_priority="review",
                    member_count=0,
                    prior_resolution=prior,
                    suppressed=True,
                    policy_summary=policy_summary,
                )

            if pending_refresh_mode == "retain_missing":
                return ProposeGroupResult(
                    group_id=pending_group.group_id,
                    signature=signature,
                    status="pending_review",
                    review_priority=pending_group.review_priority,
                    member_count=pending_group.member_count,
                    prior_resolution=prior,
                    policy_summary=policy_summary,
                )

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
                    ProposeGroupResult(
                        group_id=None,
                        signature=signature,
                        status="suppressed",
                        review_priority="review",
                        member_count=0,
                        prior_resolution=prior,
                        suppressed=True,
                        policy_summary=policy_summary,
                    )
                )
            result = ctx.result
            assert result is not None
            return result

        review_priority = derive_review_priority(pending_members, matching, prior)
        if force_review and review_priority == "normal":
            review_priority = "review"

        has_override = any(_has_active_override(instance, member) for member in delta_members)
        pending_has_override = any(
            _has_active_override(instance, member) for member in pending_members
        )
        if pending_has_override and review_priority == "normal":
            review_priority = "review"
        auto_resolve = False
        if (
            pending_group is None
            and not force_review
            and not has_override
            and prior is not None
            and prior.trust_status != "invalidated"
            and matching is not None
        ):
            trust_ok = False
            if matching.auto_resolve_requires_prior_trust == "trusted_only":
                trust_ok = prior.trust_status == "trusted"
            elif matching.auto_resolve_requires_prior_trust == "trusted_or_watch":
                trust_ok = prior.trust_status in ("trusted", "watch")
            if trust_ok and _check_auto_resolve_signals(delta_members, matching):
                auto_resolve = True

        if force_review or has_override:
            status: Literal["pending_review", "auto_resolved"] = "pending_review"
        elif auto_resolve:
            status = "auto_resolved"
        else:
            status = "pending_review"

        if pending_group is not None:
            old_keys = {_relationship_tuple(member) for member in old_members}
            new_keys = {_relationship_tuple(member) for member in pending_members}
            added_members = [
                member for member in pending_members if _relationship_tuple(member) not in old_keys
            ]
            removed_members = [
                member
                for member in old_members
                if _relationship_tuple(member) not in new_keys
            ]

            group = pending_group.model_copy(
                update={
                    "status": "pending_review",
                    "thesis_text": thesis_text,
                    "thesis_facts": thesis_facts,
                    "analysis_state": analysis_state,
                    "integrations_used": integrations_used,
                    "proposed_by": proposed_by,
                    "member_count": len(pending_members),
                    "pending_version": pending_group.pending_version + 1,
                    "review_priority": review_priority,
                    "suggested_priority": suggested_priority,
                    "source_workflow_name": source_workflow_name,
                    "source_workflow_receipt_id": source_workflow_receipt_id,
                    "source_trace_ids": source_trace_ids,
                    "source_step_ids": source_step_ids,
                    "resolution_id": None,
                }
            )
            ctx = MutationReceiptContext[ProposeGroupResult](builder=None)
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
                        "added_tuples": _summarize_tuples(added_members),
                        "removed_tuples": _summarize_tuples(removed_members),
                    },
                )
                with group_store.transaction():
                    group_store.save_group(group)
                    group_store.replace_members(group.group_id, pending_members)
                ctx.set_result(
                    ProposeGroupResult(
                        group_id=group.group_id,
                        signature=signature,
                        status="pending_review",
                        review_priority=review_priority,
                        member_count=len(pending_members),
                        prior_resolution=prior,
                        policy_summary=policy_summary,
                    )
                )
            result = ctx.result
            assert result is not None
            return result

        group_id = f"GRP-{uuid.uuid4().hex[:12]}"
        group = CandidateGroup(
            group_id=group_id,
            relationship_type=relationship_type,
            signature=signature,
            status=status,
            thesis_text=thesis_text,
            thesis_facts=thesis_facts,
            analysis_state=analysis_state,
            integrations_used=integrations_used,
            proposed_by=proposed_by,
            member_count=len(pending_members),
            pending_version=1,
            review_priority=review_priority,
            suggested_priority=suggested_priority,
            source_workflow_name=source_workflow_name,
            source_workflow_receipt_id=source_workflow_receipt_id,
            source_trace_ids=source_trace_ids,
            source_step_ids=source_step_ids,
            created_at=datetime.now(timezone.utc),
        )

        ctx = MutationReceiptContext[ProposeGroupResult](builder=None)
        with mutation_receipt(
            instance,
            "group_propose",
            {
                "group_id": group_id,
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
                    group_store.save_members(group_id, pending_members)
                except sqlite3.IntegrityError:
                    concurrent_pending = group_store.find_pending_group(
                        relationship_type,
                        signature,
                    )
                    if concurrent_pending is None:
                        raise
                    concurrent_members = group_store.get_members(concurrent_pending.group_id)
                    concurrent_keys = {_relationship_tuple(member) for member in concurrent_members}
                    rewritten_members = pending_members
                    if pending_refresh_mode == "retain_missing":
                        rewritten_members = _merge_pending_members(
                            concurrent_members,
                            delta_members,
                        )
                    rewritten_review_priority = derive_review_priority(
                        rewritten_members,
                        matching,
                        prior,
                    )
                    if force_review and rewritten_review_priority == "normal":
                        rewritten_review_priority = "review"
                    if (
                        any(_has_active_override(instance, member) for member in rewritten_members)
                        and rewritten_review_priority == "normal"
                    ):
                        rewritten_review_priority = "review"
                    added_members = [
                        member
                        for member in rewritten_members
                        if _relationship_tuple(member) not in concurrent_keys
                    ]
                    removed_members = [
                        member
                        for member in concurrent_members
                        if _relationship_tuple(member) not in {
                            _relationship_tuple(item) for item in rewritten_members
                        }
                    ]
                    rewritten = concurrent_pending.model_copy(
                        update={
                            "status": "pending_review",
                            "thesis_text": thesis_text,
                            "thesis_facts": thesis_facts,
                            "analysis_state": analysis_state,
                            "integrations_used": integrations_used,
                            "proposed_by": proposed_by,
                            "member_count": len(rewritten_members),
                            "pending_version": concurrent_pending.pending_version + 1,
                            "review_priority": rewritten_review_priority,
                            "suggested_priority": suggested_priority,
                            "source_workflow_name": source_workflow_name,
                            "source_workflow_receipt_id": source_workflow_receipt_id,
                            "source_trace_ids": source_trace_ids,
                            "source_step_ids": source_step_ids,
                            "resolution_id": None,
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
                            "added_tuples": _summarize_tuples(added_members),
                            "removed_tuples": _summarize_tuples(removed_members),
                        },
                    )
                    ctx.set_result(
                        ProposeGroupResult(
                            group_id=rewritten.group_id,
                            signature=signature,
                            status="pending_review",
                            review_priority=rewritten_review_priority,
                            member_count=len(rewritten_members),
                            prior_resolution=prior,
                            policy_summary=policy_summary,
                        )
                    )
                else:
                    ctx.set_result(
                        ProposeGroupResult(
                            group_id=group_id,
                            signature=signature,
                            status=status,
                            review_priority=review_priority,
                            member_count=len(pending_members),
                            prior_resolution=prior,
                            policy_summary=policy_summary,
                        )
                    )

        result = ctx.result
        assert result is not None
        return result
    finally:
        group_store.close()


def _apply_workflow_policies(
    *,
    config,
    graph,
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
        from_entity = graph.get_entity(member.from_type, member.from_id)
        to_entity = graph.get_entity(member.to_type, member.to_id)
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
    matching: MatchingSchema,
) -> bool:
    """Check if signals meet the auto_resolve_when policy.

    Returns True if auto-resolve is eligible based on signals alone.
    """
    policy = matching.auto_resolve_when

    for m in members:
        for sig in m.signals:
            icfg = matching.integrations.get(sig.integration)
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


def service_resolve_group(
    instance: InstanceProtocol,
    group_id: str,
    action: Literal["approve", "reject"],
    rationale: str = "",
    resolved_by: Literal["human", "agent"] = "human",
    expected_pending_version: int | None = None,
) -> ResolveGroupResult:
    """Resolve a candidate group — approve creates edges, reject records decision."""
    # 1. Validate inputs
    _VALID_ACTIONS = ("approve", "reject")
    if action not in _VALID_ACTIONS:
        raise ConfigError(f"Invalid action '{action}'. Use: {', '.join(_VALID_ACTIONS)}")
    _VALID_SOURCES = ("human", "agent")
    if resolved_by not in _VALID_SOURCES:
        raise ConfigError(f"Invalid resolved_by '{resolved_by}'. Use: {', '.join(_VALID_SOURCES)}")
    if expected_pending_version is None:
        raise ConfigError("Resolve requires expected_pending_version")

    group_store = instance.get_group_store()

    # 2. Load group — close store on failure before builder exists
    try:
        group = group_store.get_group(group_id)
        if group is None:
            raise GroupNotFoundError(group_id)

        # 3. Status guard
        if group.status == "resolved":
            raise ConfigError("Group already resolved")
        if group.status == "applying" and action != "approve":
            raise ConfigError("Group is in applying state from a prior approve — cannot reject")

        is_retry = group.status == "applying"

        # 4. Load members
        members = group_store.get_members(group_id)
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
        if group.pending_version != expected_pending_version:
            raise ConfigError(
                "Group changed during review; expected pending_version "
                f"{expected_pending_version}, found {group.pending_version}"
            )
        # 6. Reject path — no graph mutation
        if action == "reject":
            ctx.builder.record_validation(
                passed=True,
                detail={
                    "action": "reject",
                    "members": len(members),
                    "pending_version_at_resolve": group.pending_version,
                },
            )
            with group_store.transaction():
                res_id = group_store.save_resolution(
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
                group_store.update_group_status(group_id, "resolved", resolution_id=res_id)
            ctx.set_result(
                ResolveGroupResult(
                    group_id=group_id,
                    action="reject",
                    edges_created=0,
                    edges_skipped=0,
                    resolution_id=res_id,
                )
            )
        else:
            # 5. Approve — per-member validation
            instance.invalidate_graph_cache()
            config = instance.load_config()
            graph = instance.load_graph()

            valid_inputs: list[RelationshipInstance] = []
            edges_skipped = 0
            skipped_existing: list[dict[str, str]] = []
            applied_tuples: list[dict[str, str]] = []
            validation_failures = 0
            validation_errors: list[str] = []

            for m in members:
                # 5a. Count-based existence check
                count = graph.relationship_count_between(
                    m.from_type, m.from_id, m.to_type, m.to_id, m.relationship_type
                )
                if count > 0:
                    ctx.builder.record_validation(
                        passed=False,
                        detail={
                            "member": f"{m.from_type}:{m.from_id}->{m.to_type}:{m.to_id}",
                            "reason": "edge_exists",
                        },
                    )
                    edges_skipped += 1
                    skipped_existing.append(
                        {
                            "from_type": m.from_type,
                            "from_id": m.from_id,
                            "to_type": m.to_type,
                            "to_id": m.to_id,
                            "relationship_type": m.relationship_type,
                        }
                    )
                    continue

                # 5b. Validate
                try:
                    validated = validate_relationship(
                        config,
                        graph,
                        m.from_type,
                        m.from_id,
                        m.relationship_type,
                        m.to_type,
                        m.to_id,
                        m.properties,
                    )
                except DataValidationError as exc:
                    ctx.builder.record_validation(
                        passed=False,
                        detail={
                            "member": f"{m.from_type}:{m.from_id}->{m.to_type}:{m.to_id}",
                            "reason": "validation_failed",
                        },
                    )
                    edges_skipped += 1
                    validation_failures += 1
                    detail = "; ".join(exc.errors) if exc.errors else str(exc)
                    validation_errors.append(
                        f"{m.from_type}:{m.from_id} -[{m.relationship_type}]-> "
                        f"{m.to_type}:{m.to_id}: {detail}"
                    )
                    continue

                ctx.builder.record_validation(
                    passed=True,
                    detail={
                        "member": f"{m.from_type}:{m.from_id}->{m.to_type}:{m.to_id}",
                    },
                )

                # 5c. Valid — add to batch
                valid_inputs.append(
                    RelationshipInstance(
                        from_type=m.from_type,
                        from_id=m.from_id,
                        relationship_type=m.relationship_type,
                        to_type=m.to_type,
                        to_id=m.to_id,
                        properties=validated.relationship.properties,
                    )
                )
                applied_tuples.append(
                    {
                        "from_type": m.from_type,
                        "from_id": m.from_id,
                        "to_type": m.to_type,
                        "to_id": m.to_id,
                        "relationship_type": m.relationship_type,
                    }
                )

            # 7. Approve — store-first, then graph
            resolution_id: str
            if not is_retry:
                # 7a. First attempt: create resolution
                if not valid_inputs and not skipped_existing:
                    if validation_errors:
                        raise DataValidationError(
                            "Cannot approve group: candidate validation failed",
                            errors=validation_errors,
                        )
                    raise ConfigError("Cannot approve: no creatable edges")

                # Inherit trust from prior confirmed approval
                prior = group_store.find_resolution(
                    group.relationship_type,
                    group.signature,
                    action="approve",
                    confirmed=True,
                )
                inherited_trust = "watch"
                if prior is not None:
                    prior_trust = prior.trust_status
                    if prior_trust in ("trusted", "watch"):
                        inherited_trust = prior_trust

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
                        trust_status=inherited_trust,
                        confirmed=False,
                    )
                    group_store.update_group_status(
                        group_id, "applying", resolution_id=resolution_id
                    )
            else:
                # 7b. Retry path: reuse existing resolution_id
                resolution_id = group.resolution_id  # type: ignore[assignment]

            # Record relationship_write nodes before inner call
            for inp in valid_inputs:
                ctx.builder.record_relationship_write(
                    from_type=inp.from_type,
                    from_id=inp.from_id,
                    to_type=inp.to_type,
                    to_id=inp.to_id,
                    relationship=inp.relationship_type,
                    is_update=False,
                )

            # 7c. Graph write — suppress inner receipt
            edges_created = 0
            if valid_inputs:
                add_result = service_add_relationships(
                    instance,
                    valid_inputs,
                    source="group_resolve",
                    source_ref=f"group:{group_id}",
                    _create_receipt=False,
                )
                edges_created = add_result.added

            # 7d. Confirm + transition to resolved
            # Revalidate inherited trust
            prior = group_store.find_resolution(
                group.relationship_type,
                group.signature,
                action="approve",
                confirmed=True,
            )
            revalidated_trust: str | None = None
            if prior is not None:
                prior_trust = prior.trust_status
                if prior_trust == "invalidated":
                    revalidated_trust = "watch"

            with group_store.transaction():
                group_store.confirm_resolution(resolution_id, trust_status=revalidated_trust)
                group_store.update_group_status(group_id, "resolved")

            ctx.builder.record_validation(
                passed=validation_failures == 0,
                detail={
                    "pending_version_at_resolve": group.pending_version,
                    "resolution_id": resolution_id,
                    "applied_tuples": applied_tuples,
                    "skipped_tuples_existing_edges": skipped_existing,
                },
            )

            ctx.set_result(
                ResolveGroupResult(
                    group_id=group_id,
                    action="approve",
                    edges_created=edges_created,
                    edges_skipped=edges_skipped,
                    resolution_id=resolution_id,
                )
            )

    result = ctx.result
    assert result is not None
    return result


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
        return GetGroupResult(group=group, members=members, resolution=resolution)
    finally:
        group_store.close()


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
        accepted_tuples = group_store.list_approved_relationship_tuples(
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
            accepted_tuple_count=len(accepted_tuples),
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
) -> None:
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

        # 5. Update
        with group_store.transaction():
            group_store.update_resolution_trust_status(resolution_id, trust_status, reason)
    finally:
        group_store.close()
