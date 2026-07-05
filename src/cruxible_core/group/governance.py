"""Side-effect-free governance policy and proposal analysis helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cruxible_core.config.property_validation import entity_properties_with_identity
from cruxible_core.config.schema import CoreConfig, ProposalPolicySchema, RelationshipSchema
from cruxible_core.errors import ConfigError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    GroupResolution,
    ReviewPriority,
    is_unevidenced_support_signal,
)
from cruxible_core.instance_protocol import GroupStoreProtocol
from cruxible_core.primitives import canonical_json, ordered_unique
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.service.types import SuppressedProposalMember
from cruxible_core.temporal import is_expired


def suppressed_member_from_relationship(
    relationship: RelationshipInstance,
    *,
    reason: Literal["existing_edge", "pending_proposal"],
    group: CandidateGroup | None = None,
) -> SuppressedProposalMember:
    """Build a public suppression payload from a relationship tuple."""
    existing_group_id = None
    existing_group_status = None
    existing_signature = None
    source_workflow_name = None
    if group is not None:
        existing_group_id = group.group_id
        existing_group_status = group.status
        existing_signature = group.signature
        source_workflow_name = group.source_workflow_name
    return SuppressedProposalMember(
        relationship_type=relationship.relationship_type,
        from_type=relationship.from_type,
        from_id=relationship.from_id,
        to_type=relationship.to_type,
        to_id=relationship.to_id,
        reason=reason,
        existing_group_id=existing_group_id,
        existing_group_status=existing_group_status,
        existing_signature=existing_signature,
        source_workflow_name=source_workflow_name,
    )


@dataclass(frozen=True)
class MemberChanges:
    """Added/removed tuple delta when an existing pending group is rewritten."""

    added: list[CandidateMember]
    removed: list[CandidateMember]


def member_signal_sources(members: list[CandidateMember]) -> list[str]:
    """Return ordered unique signal sources attached to proposal members."""
    return list(
        ordered_unique(signal.signal_source for member in members for signal in member.signals)
    )


def member_signature_scope(members: list[CandidateMember]) -> list[dict[str, str]]:
    """Return deterministic member tuple scope for agent-authored signatures."""
    return [
        relationship.identity_payload()
        for relationship in sorted(
            (member.as_relationship() for member in members),
            key=lambda value: value.identity_tuple(),
        )
    ]


def _policy_signal_sources(
    proposal_policy: ProposalPolicySchema | None,
    *,
    role: Literal["required", "blocking"],
) -> list[str]:
    if proposal_policy is None:
        return []
    return sorted(
        source
        for source, signal_policy in proposal_policy.signals.items()
        if signal_policy.role == role
    )


def build_workflow_proposal_signature_facts(
    *,
    rel_schema: RelationshipSchema,
    relationship_type: str,
    workflow_name: str,
    step_id: str,
    proposal_logic_digest: str,
    candidates_from: str,
    signal_sources_used: list[str],
) -> dict[str, Any]:
    """Build non-configurable signature facts for workflow-authored proposals."""
    proposal_policy = rel_schema.proposal_policy
    return {
        "origin": {
            "kind": "workflow",
            "evidence_mode": "workflow_generated",
            "workflow_name": workflow_name,
            "step_id": step_id,
            "proposal_logic_digest": proposal_logic_digest,
        },
        "relationship": {
            "type": relationship_type,
            "from_type": rel_schema.from_entity,
            "to_type": rel_schema.to_entity,
        },
        "candidates": {"from_alias": candidates_from},
        "signals": {
            "used": sorted(ordered_unique(signal_sources_used)),
            "required": _policy_signal_sources(proposal_policy, role="required"),
            "blocking": _policy_signal_sources(proposal_policy, role="blocking"),
        },
        "policy": {
            "auto_resolve_when": (
                proposal_policy.auto_resolve_when if proposal_policy is not None else None
            ),
            "proposal_identity": rel_schema.proposal_identity,
        },
    }


def build_agent_proposal_signature_facts(
    *,
    rel_schema: RelationshipSchema,
    relationship_type: str,
    signal_sources_used: list[str],
    agent_scope: dict[str, Any],
    member_scope: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build signature facts for direct proposals from agent-supplied signals."""
    facts: dict[str, Any] = {
        "origin": {
            "kind": "agent",
            "evidence_mode": "agent_supplied",
        },
        "relationship": {
            "type": relationship_type,
            "from_type": rel_schema.from_entity,
            "to_type": rel_schema.to_entity,
        },
        "signals": {
            "used": sorted(ordered_unique(signal_sources_used)),
            "supplied_by": "agent",
        },
        "agent_scope": agent_scope,
    }
    if not agent_scope:
        facts["member_scope"] = member_scope or []
    return facts


def relationship_tuples_summary(members: list[CandidateMember]) -> list[dict[str, str]]:
    """Return deterministic public tuple payloads for receipt details."""
    return [member.as_relationship().identity_payload() for member in members]


def merge_suppressed_members(
    existing: list[SuppressedProposalMember],
    additions: list[SuppressedProposalMember],
) -> list[SuppressedProposalMember]:
    """Merge suppression entries without duplicating tuple/reason pairs."""
    merged: dict[tuple[tuple[str, str, str, str, str], str], SuppressedProposalMember] = {}
    for item in existing:
        relationship = RelationshipInstance(
            relationship_type=item.relationship_type,
            from_type=item.from_type,
            from_id=item.from_id,
            to_type=item.to_type,
            to_id=item.to_id,
        )
        merged[(relationship.identity_tuple(), item.reason)] = item
    for item in additions:
        relationship = RelationshipInstance(
            relationship_type=item.relationship_type,
            from_type=item.from_type,
            from_id=item.from_id,
            to_type=item.to_type,
            to_id=item.to_id,
        )
        merged.setdefault((relationship.identity_tuple(), item.reason), item)
    return list(merged.values())


def filter_relationship_conflicts(
    *,
    graph: EntityGraph,
    group_store: GroupStoreProtocol,
    relationship_type: str,
    members: list[CandidateMember],
    exclude_group_id: str | None,
) -> tuple[list[CandidateMember], list[SuppressedProposalMember]]:
    """Suppress members whose relationship tuple is already live or pending."""
    if not members:
        return [], []

    conflicts = group_store.find_pending_groups_for_tuples(
        relationship_type,
        [member.as_relationship().identity_tuple() for member in members],
        exclude_group_id=exclude_group_id,
        statuses=("pending_review", "applying"),
    )
    retained: list[CandidateMember] = []
    suppressed: list[SuppressedProposalMember] = []
    for member in members:
        relationship = member.as_relationship()
        if graph.has_live_relationship(
            relationship.from_type,
            relationship.from_id,
            relationship.to_type,
            relationship.to_id,
            relationship.relationship_type,
        ):
            suppressed.append(
                suppressed_member_from_relationship(
                    relationship,
                    reason="existing_edge",
                )
            )
            continue
        pending_group = conflicts.get(relationship.identity_tuple())
        if pending_group is not None:
            suppressed.append(
                suppressed_member_from_relationship(
                    relationship,
                    reason="pending_proposal",
                    group=pending_group,
                )
            )
            continue
        retained.append(member)
    return retained, suppressed


def merge_pending_members(
    existing_members: list[CandidateMember],
    current_members: list[CandidateMember],
) -> list[CandidateMember]:
    """Merge retained pending group members by relationship tuple identity."""
    merged: dict[tuple[str, str, str, str, str], CandidateMember] = {
        member.as_relationship().identity_tuple(): member for member in existing_members
    }
    for member in current_members:
        merged[member.as_relationship().identity_tuple()] = member
    return list(merged.values())


def member_changes(
    existing_members: list[CandidateMember],
    current_members: list[CandidateMember],
) -> MemberChanges:
    """Compute added and removed relationship members for pending rewrites."""
    existing_keys = {member.as_relationship().identity_tuple() for member in existing_members}
    current_keys = {member.as_relationship().identity_tuple() for member in current_members}
    return MemberChanges(
        added=[
            member
            for member in current_members
            if member.as_relationship().identity_tuple() not in existing_keys
        ],
        removed=[
            member
            for member in existing_members
            if member.as_relationship().identity_tuple() not in current_keys
        ],
    )


def members_have_active_override(graph: EntityGraph, members: list[CandidateMember]) -> bool:
    """Return true when an existing tuple has active override review state."""
    for member in members:
        candidate_relationship = member.as_relationship()
        relationship = graph.get_relationship(
            candidate_relationship.from_type,
            candidate_relationship.from_id,
            candidate_relationship.to_type,
            candidate_relationship.to_id,
            candidate_relationship.relationship_type,
        )
        if relationship is None:
            continue
        if relationship.metadata.assertion.group_override is True:
            return True
        review = relationship.metadata.assertion.review
        if review.status in ("pending", "rejected"):
            return True
    return False


def derive_review_priority(
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
) -> ReviewPriority:
    """Derive review priority mechanically from policy signals and prior trust."""
    if proposal_policy is None:
        return "review" if prior_resolution is None else "normal"

    has_critical = False
    has_review = False

    if prior_resolution is not None:
        if prior_resolution.trust_status == "invalidated":
            has_critical = True
        elif prior_resolution.trust_status == "watch":
            has_review = True

    for member in members:
        for signal in member.signals:
            signal_config = proposal_policy.signals.get(signal.signal_source)
            if signal_config is None or signal_config.role == "advisory":
                continue

            if signal.signal == "contradict" and signal_config.role == "blocking":
                has_critical = True
            elif signal.signal == "unsure":
                if signal_config.always_review_on_unsure:
                    has_review = True
                if signal_config.role in ("blocking", "required"):
                    has_review = True
            elif signal_config.require_evidence_on_support and is_unevidenced_support_signal(
                signal
            ):
                has_review = True

    if prior_resolution is None:
        has_review = True

    if has_critical:
        return "critical"
    if has_review:
        return "review"
    return "normal"


def review_priority_for_members(
    *,
    graph: EntityGraph,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
    force_review: bool,
) -> ReviewPriority:
    """Apply explicit review forcing and active overrides to policy priority."""
    review_priority = derive_review_priority(members, proposal_policy, prior_resolution)
    if force_review and review_priority == "normal":
        review_priority = "review"
    if members_have_active_override(graph, members) and review_priority == "normal":
        review_priority = "review"
    return review_priority


def should_auto_resolve(
    *,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    prior_resolution: GroupResolution | None,
    force_review: bool,
    has_override: bool,
) -> bool:
    """Return whether prior trust and current signals permit auto-resolution."""
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
    return trust_ok and check_auto_resolve_signals(members, proposal_policy)


def validate_group_proposal_inputs(
    *,
    rel_schema: RelationshipSchema,
    relationship_type: str,
    members: list[CandidateMember],
    thesis_facts: dict[str, Any],
) -> None:
    """Validate tuple identity and thesis facts for a governed proposal."""
    if not members:
        raise ConfigError("Members list must not be empty")

    for member in members:
        relationship = member.as_relationship()
        if relationship.relationship_type != relationship_type:
            raise ConfigError(
                f"Member {relationship.from_id}\u2192{relationship.to_id} has relationship_type "
                f"'{relationship.relationship_type}' but group is for '{relationship_type}'"
            )
        if relationship.from_type != rel_schema.from_entity:
            raise ConfigError(
                f"Member {relationship.from_id} from_type "
                f"'{relationship.from_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.from_entity}'"
            )
        if relationship.to_type != rel_schema.to_entity:
            raise ConfigError(
                f"Member {relationship.to_id} to_type '{relationship.to_type}' does not match "
                f"relationship '{relationship_type}' which expects '{rel_schema.to_entity}'"
            )

    try:
        canonical_json(thesis_facts)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"thesis_facts must be JSON-serializable: {exc}") from exc

    seen_members: set[tuple[str, str, str, str, str]] = set()
    for member in members:
        relationship = member.as_relationship()
        identity = relationship.identity_tuple()
        if identity in seen_members:
            raise ConfigError(
                f"Duplicate member: {relationship.from_type}:{relationship.from_id} \u2192 "
                f"{relationship.to_type}:{relationship.to_id} via {relationship.relationship_type}"
            )
        seen_members.add(identity)


def validate_proposal_signals(
    *,
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema | None,
    signal_sources_used: list[str],
) -> None:
    """Validate member signal uniqueness and proposal-policy coverage."""
    for member in members:
        relationship = member.as_relationship()
        seen_signal_sources: set[str] = set()
        for signal in member.signals:
            if signal.signal_source in seen_signal_sources:
                raise ConfigError(
                    f"Member {relationship.from_id}\u2192{relationship.to_id} "
                    "has duplicate signals "
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
            relationship = member.as_relationship()
            signal_sources = {signal.signal_source for signal in member.signals}
            if source_name not in signal_sources:
                raise ConfigError(
                    f"Member {relationship.from_id}\u2192{relationship.to_id} missing signal "
                    f"from {signal_config.role} signal source '{source_name}'"
                )

    for source_name in signal_sources_used:
        if source_name not in proposal_policy.signals:
            raise ConfigError(
                f"Signal source '{source_name}' not declared in proposal_policy.signals"
            )

    if len(members) > proposal_policy.max_group_size:
        raise ConfigError(
            f"Group size {len(members)} exceeds max_group_size {proposal_policy.max_group_size}"
        )


def apply_workflow_policies(
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
        and not is_expired(policy.expires_at)
    ]
    if not policies:
        return members, False

    kept: list[CandidateMember] = []
    force_review = False
    for member in members:
        relationship = member.as_relationship()
        from_entity = graph.get_entity(relationship.from_type, relationship.from_id)
        to_entity = graph.get_entity(relationship.to_type, relationship.to_id)
        matched_effects: list[str] = []
        for policy in policies:
            if from_entity is None or to_entity is None:
                continue
            from_props = entity_properties_with_identity(
                config,
                from_entity.entity_type,
                from_entity.entity_id,
                from_entity.properties,
            )
            to_props = entity_properties_with_identity(
                config,
                to_entity.entity_type,
                to_entity.entity_id,
                to_entity.properties,
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


def check_auto_resolve_signals(
    members: list[CandidateMember],
    proposal_policy: ProposalPolicySchema,
) -> bool:
    """Return whether member signals satisfy the auto-resolve policy."""
    policy = proposal_policy.auto_resolve_when

    for member in members:
        for signal in member.signals:
            signal_config = proposal_policy.signals.get(signal.signal_source)
            if signal_config is None or signal_config.role == "advisory":
                continue

            if signal.signal == "unsure" and signal_config.always_review_on_unsure:
                return False

            if signal_config.require_evidence_on_support and is_unevidenced_support_signal(
                signal
            ):
                return False

            if policy == "all_support":
                if signal.signal != "support":
                    return False
            elif policy == "no_contradict":
                if signal.signal == "contradict" and signal_config.role == "blocking":
                    return False

    return True
