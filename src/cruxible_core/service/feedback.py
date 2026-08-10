"""Feedback and outcome service functions."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, get_args

from pydantic import ValidationError

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    validate_property_payload,
)
from cruxible_core.config.schema import (
    CoreConfig,
    FeedbackProfileSchema,
    FeedbackRemediationHint,
    OutcomeProfileSchema,
    OutcomeRemediationHint,
)
from cruxible_core.deprecation import (
    GROUP_OVERRIDE,
    LEGACY_OUTCOME_PROFILE,
    LEGACY_OUTCOME_RECORD,
    emit_python_deprecation,
)
from cruxible_core.errors import (
    ConfigError,
    DataValidationError,
    DirectWriteRefusedError,
    ReceiptNotFoundError,
    RelationshipAmbiguityError,
)
from cruxible_core.feedback.applier import apply_feedback
from cruxible_core.feedback.types import (
    FeedbackAction,
    FeedbackBatchItem,
    FeedbackRecord,
    OutcomeRecord,
)
from cruxible_core.governance.actors import (
    DerivedActorKind,
    GovernedActorContext,
    derived_actor_kind,
)
from cruxible_core.graph.claim_target import ClaimTargetConflictError, resolve_claim_target
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.group.types import CandidateGroup, GroupResolution
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.types import Receipt
from cruxible_core.runtime.permissions import (
    FEEDBACK_ACTION_PERMISSIONS,
    FEEDBACK_ADJUDICATION_OPERATION,
    PermissionMode,
    check_permission,
)
from cruxible_core.service.direct_write_policy import env_refuses_feedback_acceptance
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.queries import service_get_receipt
from cruxible_core.service.types import (
    FeedbackBatchServiceResult,
    FeedbackItemInput,
    FeedbackServiceResult,
    OutcomeServiceResult,
    RelationshipTargetInput,
)

_VALID_ACTIONS = get_args(FeedbackAction)
"""The WRITE vocabulary, derived from the type so the two cannot drift.

Deliberately NOT ``StoredFeedbackAction``: history may contain ``flag`` rows
that read fine but can never be written again.
"""
_VALID_OUTCOMES = ("correct", "incorrect", "partial", "unknown")


def _validate_feedback_request_values(
    *,
    action: str,
    corrections: Any,
) -> None:
    """Validate the basic feedback payload before loading external state.

    THE service seam for payload-shape refusals: every entry point
    (``service_feedback``, ``service_feedback_from_query_result``,
    ``service_feedback_batch`` per item, and ``_normalize_feedback_record``)
    routes through here, so the CLI, MCP, and HTTP surfaces all inherit these
    refusals without each restating them.
    """
    if action not in _VALID_ACTIONS:
        raise ConfigError(f"Invalid action '{action}'. Use: {', '.join(_VALID_ACTIONS)}")

    if corrections is not None and not isinstance(corrections, dict):
        raise ConfigError("corrections must be an object")

    # ``correct`` with nothing to correct promoted the edge to ``approved``
    # anyway -- an approval wearing a correction's name, at the correction's
    # tier, with no record of what was corrected. If the intent is approval,
    # say accept; if it is a doubt, attest a contradiction.
    if action == "correct" and not corrections:
        raise ConfigError(
            "Feedback action 'correct' requires a non-empty 'corrections' object naming "
            "the properties to change. An empty correction still promoted the edge to "
            "'approved' while recording nothing that was corrected. Use action 'accept' "
            "to accept the claim as it stands, or "
            "'cruxible attest record --stance contradict' "
            "to record a doubt without adjudicating."
        )


def _normalize_feedback_record(
    *,
    config: CoreConfig,
    graph: EntityGraph,
    receipt: Receipt | None,
    receipt_id: str | None,
    action: FeedbackAction,
    target: RelationshipInstance,
    reason: str,
    reason_code: str | None,
    scope_hints: dict[str, Any] | None,
    corrections: dict[str, Any] | None,
    group_override: bool,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackRecord:
    """Validate and normalize one feedback request into a record.

    The target is resolved ONCE here, honouring the disambiguator precedence
    (``claim_id`` wins; disagreeing disambiguators refuse), and the resolved
    claim's identity is stamped onto the recorded target. Resolution semantics
    are otherwise unchanged: tuple-first, and an unresolvable target still
    records -- feedback about an edge that has since gone is still feedback.
    """
    _validate_feedback_request_values(
        action=action,
        corrections=corrections,
    )

    try:
        resolved_target = resolve_claim_target(
            graph,
            relationship_type=target.relationship_type,
            from_type=target.from_type,
            from_id=target.from_id,
            to_type=target.to_type,
            to_id=target.to_id,
            claim_id=target.claim_id,
            edge_key=target.edge_key,
        ).relationship
    except ClaimTargetConflictError as exc:
        raise ConfigError(str(exc)) from exc
    if resolved_target is not None:
        # Record-time stamp from the RESOLVED claim, not from the request.
        target = target.model_copy(update={"claim_id": resolved_target.claim_id})

    normalized_corrections = corrections or {}
    if action == "correct" and normalized_corrections:
        rel_schema = config.get_relationship(target.relationship_type)
        if rel_schema is None:
            raise DataValidationError(
                f"relationship '{target.relationship_type}' not found in config"
            )
        validation = validate_property_payload(
            config,
            rel_schema.properties,
            normalized_corrections,
            require_required=False,
        )
        if validation.errors:
            raise DataValidationError(
                "feedback corrections failed property validation",
                errors=validation.errors,
            )
        normalized_corrections = validation.properties
    else:
        normalized_corrections = dict(normalized_corrections)
    normalized_scope_hints = dict(scope_hints or {})

    if group_override:
        if resolved_target is None:
            raise ConfigError("group_override requires the edge to exist in the graph")
        if target.edge_key is None and target.claim_id is None:
            count = graph.relationship_count_between(
                target.from_type,
                target.from_id,
                target.to_type,
                target.to_id,
                target.relationship_type,
            )
            if count > 1:
                raise RelationshipAmbiguityError(
                    from_type=target.from_type,
                    from_id=target.from_id,
                    to_type=target.to_type,
                    to_id=target.to_id,
                    relationship_type=target.relationship_type,
                )

    profile = config.get_feedback_profile(target.relationship_type)
    reason_remediation_hint: FeedbackRemediationHint | None = None
    if profile is not None:
        _validate_feedback_inputs(
            profile=profile,
            relationship_type=target.relationship_type,
            reason_code=reason_code,
            scope_hints=normalized_scope_hints,
            actor_kind=derived_actor_kind(actor_context),
        )
        if reason_code is not None:
            reason_schema = profile.reason_codes[reason_code]
            reason_remediation_hint = reason_schema.remediation_hint

    decision_context = _build_decision_context(receipt)
    context_snapshot = _build_context_snapshot(
        config=config,
        graph=graph,
        profile=profile,
        target=target,
        decision_context=decision_context,
    )

    return FeedbackRecord(
        receipt_id=receipt_id,
        action=action,
        target=target,
        reason=reason,
        reason_code=reason_code,
        reason_remediation_hint=reason_remediation_hint,
        scope_hints=normalized_scope_hints,
        feedback_profile_key=target.relationship_type if profile is not None else None,
        feedback_profile_version=profile.version if profile is not None else None,
        feedback_profile_digest=profile.body_digest() if profile is not None else None,
        decision_context=decision_context,
        context_snapshot=context_snapshot,
        corrections=normalized_corrections,
        actor_context=actor_context,
    )


def _load_receipts(instance: InstanceProtocol, receipt_ids: Iterable[str]) -> dict[str, Receipt]:
    """Load receipt objects, failing if any referenced receipt IDs do not exist."""
    receipt_store = instance.get_receipt_store()
    receipts: dict[str, Receipt] = {}
    try:
        for receipt_id in receipt_ids:
            receipt = receipt_store.get_receipt(receipt_id)
            if receipt is None:
                raise ReceiptNotFoundError(receipt_id)
            receipts[receipt_id] = receipt
    finally:
        receipt_store.close()
    return receipts


def _validate_feedback_inputs(
    *,
    profile: FeedbackProfileSchema,
    relationship_type: str,
    reason_code: str | None,
    scope_hints: dict[str, Any],
    actor_kind: DerivedActorKind,
) -> None:
    """Validate feedback inputs against the configured feedback profile.

    The reason-code requirement keys off the DERIVED actor kind, never a
    caller-declared one. It used to read a ``source`` field the caller set
    itself and defaulted to ``"human"`` — so the one rule that holds
    non-human writers to a structured, analyzable reason was skippable by
    the writers it was written for, simply by claiming to be a person.

    Anything that is not a resolved human must give a reason code, and that
    includes ``"unknown"``: an unattributed write is not evidence of a human,
    it is absence of evidence, and absence is not grounds for the exemption.
    """
    if actor_kind != "human" and not reason_code:
        raise ConfigError(
            f"Feedback for relationship '{relationship_type}' requires reason_code "
            f"for a '{actor_kind}' actor"
        )

    if reason_code is not None:
        reason_schema = profile.reason_codes.get(reason_code)
        if reason_schema is None:
            raise ConfigError(
                f"Feedback for relationship '{relationship_type}' uses unknown reason_code "
                f"'{reason_code}'"
            )
        missing_scope = [key for key in reason_schema.required_scope_keys if key not in scope_hints]
        if missing_scope:
            missing_str = ", ".join(sorted(missing_scope))
            raise ConfigError(
                f"Feedback reason_code '{reason_code}' requires scope_hints for: {missing_str}"
            )

    unexpected_scope = sorted(set(scope_hints) - set(profile.scope_keys))
    if unexpected_scope:
        unexpected_str = ", ".join(unexpected_scope)
        raise ConfigError(
            f"Feedback for relationship '{relationship_type}' uses undeclared scope_hints: "
            f"{unexpected_str}"
        )


def _build_decision_context(receipt: Receipt | None) -> dict[str, Any]:
    """Derive stable decision-surface metadata from the anchored receipt."""
    if receipt is None:
        return {
            "surface_type": "operation",
            "surface_name": "explicit_feedback",
            "operation_type": "feedback",
            "source_receipt_id": None,
        }
    if receipt.operation_type == "query":
        surface_type = "query"
        surface_name = receipt.query_name
    elif receipt.operation_type == "workflow":
        surface_type = "workflow"
        surface_name = receipt.query_name
    else:
        surface_type = "operation"
        surface_name = receipt.operation_type

    return {
        "surface_type": surface_type,
        "surface_name": surface_name,
        "operation_type": receipt.operation_type,
    }


def _build_context_snapshot(
    *,
    config: CoreConfig,
    graph: EntityGraph,
    profile: FeedbackProfileSchema | None,
    target: RelationshipInstance,
    decision_context: dict[str, Any],
) -> dict[str, Any]:
    """Capture a bounded feedback-time snapshot for deterministic grouping."""
    from_entity = graph.get_entity(target.from_type, target.from_id)
    to_entity = graph.get_entity(target.to_type, target.to_id)
    relationship = graph.get_relationship(
        target.from_type,
        target.from_id,
        target.to_type,
        target.to_id,
        target.relationship_type,
        edge_key=target.edge_key,
    )

    from_props: dict[str, Any] = {}
    to_props: dict[str, Any] = {}
    edge_props: dict[str, Any] = {}
    if profile is not None:
        for path in profile.scope_keys.values():
            side, _, prop_name = path.partition(".")
            if side == "FROM" and from_entity is not None:
                props = entity_properties_with_identity(
                    config,
                    from_entity.entity_type,
                    from_entity.entity_id,
                    from_entity.properties,
                )
                if prop_name in props:
                    from_props[prop_name] = props[prop_name]
            elif side == "TO" and to_entity is not None:
                props = entity_properties_with_identity(
                    config,
                    to_entity.entity_type,
                    to_entity.entity_id,
                    to_entity.properties,
                )
                if prop_name in props:
                    to_props[prop_name] = props[prop_name]
            elif (
                side == "EDGE" and relationship is not None and prop_name in relationship.properties
            ):
                edge_props[prop_name] = relationship.properties[prop_name]

    return {
        "from": {
            "entity_type": target.from_type,
            "entity_id": target.from_id,
            "properties": from_props,
        },
        "to": {
            "entity_type": target.to_type,
            "entity_id": target.to_id,
            "properties": to_props,
        },
        "edge": {
            "relationship": target.relationship_type,
            "edge_key": target.edge_key,
            "claim_id": target.claim_id,
            "properties": edge_props,
        },
        "context": decision_context,
    }


def _validate_outcome_request_values(
    *,
    outcome: str,
    detail: Any,
) -> None:
    """Validate the basic outcome payload before loading external state."""
    if outcome not in _VALID_OUTCOMES:
        raise ConfigError(f"Invalid outcome '{outcome}'. Use: {', '.join(_VALID_OUTCOMES)}")

    if detail is not None and not isinstance(detail, dict):
        raise ConfigError("detail must be an object")


def _resolve_outcome_profile(
    *,
    config: CoreConfig,
    anchor_type: Literal["resolution", "receipt"],
    relationship_type: str | None,
    workflow_name: str | None,
    surface_type: str | None,
    surface_name: str | None,
    outcome_profile_key: str | None,
) -> tuple[str | None, OutcomeProfileSchema | None]:
    """Resolve the applicable outcome profile for one anchored outcome."""
    if outcome_profile_key is not None:
        profile = config.get_outcome_profile(outcome_profile_key)
        if profile is None:
            raise ConfigError(f"Outcome profile '{outcome_profile_key}' not found")
        _validate_outcome_profile_match(
            profile_key=outcome_profile_key,
            profile=profile,
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
        )
        return outcome_profile_key, profile

    matches: list[tuple[str, OutcomeProfileSchema]] = []
    wildcard: list[tuple[str, OutcomeProfileSchema]] = []
    for profile_key, profile in config.outcome_profiles.items():
        if profile.anchor_type != anchor_type:
            continue
        if anchor_type == "resolution":
            if profile.relationship_type != relationship_type:
                continue
            if profile.workflow_name is None:
                wildcard.append((profile_key, profile))
            elif profile.workflow_name == workflow_name:
                matches.append((profile_key, profile))
        else:
            if profile.surface_type == surface_type and profile.surface_name == surface_name:
                matches.append((profile_key, profile))

    if anchor_type == "resolution" and matches:
        if len(matches) > 1:
            names = ", ".join(sorted(name for name, _ in matches))
            raise ConfigError(f"Ambiguous outcome profiles for resolution anchor: {names}")
        return matches[0]

    if not matches and anchor_type == "resolution" and wildcard:
        if len(wildcard) > 1:
            names = ", ".join(sorted(name for name, _ in wildcard))
            raise ConfigError(f"Ambiguous wildcard outcome profiles for resolution anchor: {names}")
        return wildcard[0]

    if len(matches) > 1:
        names = ", ".join(sorted(name for name, _ in matches))
        raise ConfigError(f"Ambiguous outcome profiles for receipt anchor: {names}")

    return matches[0] if matches else (None, None)


def _validate_outcome_profile_match(
    *,
    profile_key: str,
    profile: OutcomeProfileSchema,
    anchor_type: Literal["resolution", "receipt"],
    relationship_type: str | None,
    workflow_name: str | None,
    surface_type: str | None,
    surface_name: str | None,
) -> None:
    """Ensure an explicitly requested outcome profile matches the anchor context."""
    if profile.anchor_type != anchor_type:
        raise ConfigError(
            f"Outcome profile '{profile_key}' requires anchor_type '{profile.anchor_type}', "
            f"not '{anchor_type}'"
        )
    if anchor_type == "resolution":
        if profile.relationship_type != relationship_type:
            raise ConfigError(
                f"Outcome profile '{profile_key}' requires relationship_type "
                f"'{profile.relationship_type}', not '{relationship_type}'"
            )
        if profile.workflow_name is not None and profile.workflow_name != workflow_name:
            raise ConfigError(
                f"Outcome profile '{profile_key}' requires workflow_name "
                f"'{profile.workflow_name}', not '{workflow_name}'"
            )
    else:
        if profile.surface_type != surface_type or profile.surface_name != surface_name:
            raise ConfigError(
                f"Outcome profile '{profile_key}' requires surface "
                f"'{profile.surface_type}:{profile.surface_name}', not "
                f"'{surface_type}:{surface_name}'"
            )


def _validate_outcome_inputs(
    *,
    profile: OutcomeProfileSchema | None,
    profile_key: str | None,
    outcome_code: str | None,
    scope_hints: dict[str, Any],
    actor_kind: DerivedActorKind,
) -> None:
    """Validate structured outcome inputs against the configured profile.

    Keys off the derived actor kind for the same reason as
    :func:`_validate_feedback_inputs`.
    """
    if profile is None:
        if outcome_code is not None:
            raise ConfigError("Outcome uses outcome_code but no matching outcome profile exists")
        if scope_hints:
            raise ConfigError("Outcome uses scope_hints but no matching outcome profile exists")
        return

    if actor_kind != "human" and not outcome_code:
        raise ConfigError(
            f"Outcome for profile '{profile_key}' requires outcome_code for a '{actor_kind}' actor"
        )

    if outcome_code is not None:
        code_schema = profile.outcome_codes.get(outcome_code)
        if code_schema is None:
            raise ConfigError(
                f"Outcome for profile '{profile_key}' uses unknown outcome_code '{outcome_code}'"
            )
        missing_scope = [key for key in code_schema.required_scope_keys if key not in scope_hints]
        if missing_scope:
            missing_str = ", ".join(sorted(missing_scope))
            raise ConfigError(
                f"Outcome outcome_code '{outcome_code}' requires scope_hints for: {missing_str}"
            )

    unexpected_scope = sorted(set(scope_hints) - set(profile.scope_keys))
    if unexpected_scope:
        unexpected_str = ", ".join(unexpected_scope)
        raise ConfigError(
            f"Outcome for profile '{profile_key}' uses undeclared scope_hints: {unexpected_str}"
        )


def _build_receipt_trace_summaries(receipt: Receipt) -> list[dict[str, Any]]:
    """Extract bounded trace summaries from workflow receipt plan-step nodes."""
    summaries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for node in receipt.nodes:
        if node.node_type != "plan_step":
            continue
        trace_id = str(node.detail.get("trace_id", "")).strip()
        if not trace_id:
            continue
        provider_name = str(node.detail.get("provider_name", "")).strip()
        step_id = str(node.detail.get("step_id", "")).strip()
        status = str(node.detail.get("status", "success")).strip()
        key = (trace_id, provider_name, step_id, status)
        if key in seen:
            continue
        seen.add(key)
        summaries.append(
            {
                "trace_id": trace_id,
                "provider_name": provider_name,
                "step_id": step_id,
                "status": status,
            }
        )
    return sorted(summaries, key=lambda item: item["trace_id"])


def _load_trace_summaries(
    instance: InstanceProtocol,
    trace_ids: list[str],
    *,
    fallback_receipt: Receipt | None = None,
) -> list[dict[str, Any]]:
    """Load bounded trace summaries from stored traces, falling back to receipt nodes."""
    if not trace_ids:
        return _build_receipt_trace_summaries(fallback_receipt) if fallback_receipt else []

    store = instance.get_receipt_store()
    summaries: list[dict[str, Any]] = []
    try:
        for trace_id in trace_ids:
            trace = store.get_trace(trace_id)
            if trace is None:
                continue
            summaries.append(
                {
                    "trace_id": trace.trace_id,
                    "provider_name": trace.provider_name,
                    "step_id": trace.step_id,
                    "status": trace.status,
                }
            )
    finally:
        store.close()

    if summaries:
        return sorted(summaries, key=lambda item: item["trace_id"])
    return _build_receipt_trace_summaries(fallback_receipt) if fallback_receipt else []


def _iter_thesis_scope_keys(profile: OutcomeProfileSchema | None) -> set[str]:
    """Return THESIS field names referenced by one outcome profile."""
    if profile is None:
        return set()
    return {
        path.partition(".")[2] for path in profile.scope_keys.values() if path.startswith("THESIS.")
    }


def _build_trace_set_snapshot(trace_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build bounded aggregated trace metadata for grouping and debug packages."""
    provider_names = sorted(
        {
            provider
            for provider in (summary.get("provider_name") for summary in trace_summaries)
            if provider
        }
    )
    trace_ids = [summary["trace_id"] for summary in trace_summaries]
    return {
        "trace_ids": trace_ids,
        "provider_names": provider_names,
        "trace_count": len(trace_summaries),
        "summaries": trace_summaries,
    }


def _build_receipt_lineage_snapshot(
    *,
    receipt: Receipt,
    trace_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture a bounded receipt-time lineage snapshot."""
    decision_context = _build_decision_context(receipt)
    return {
        "receipt": {
            "receipt_id": receipt.receipt_id,
            "operation_type": receipt.operation_type,
        },
        "surface": {
            "type": decision_context["surface_type"],
            "name": decision_context["surface_name"],
        },
        "trace_set": _build_trace_set_snapshot(trace_summaries),
    }


def _build_resolution_lineage_snapshot(
    *,
    profile: OutcomeProfileSchema | None,
    resolution: GroupResolution,
    group: CandidateGroup,
    trace_summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Capture a bounded resolution-time lineage snapshot."""
    thesis_keys = _iter_thesis_scope_keys(profile)
    thesis_facts = {
        key: resolution.thesis_facts[key] for key in thesis_keys if key in resolution.thesis_facts
    }
    return {
        "resolution": {
            "resolution_id": resolution.resolution_id,
            "relationship_type": resolution.relationship_type,
            "action": resolution.action,
            "trust_status": resolution.trust_status,
            "resolved_by": derived_actor_kind(resolution.resolved_actor_context),
            "resolution_source": resolution.resolution_source,
        },
        "group": {
            "group_signature": resolution.group_signature,
        },
        "workflow": {
            "name": group.source_workflow_name,
            "receipt_id": group.source_workflow_receipt_id,
            "trace_ids": list(group.source_trace_ids),
        },
        "trace_set": _build_trace_set_snapshot(trace_summaries),
        "thesis": thesis_facts,
    }


def _resolve_receipt_outcome_context(
    instance: InstanceProtocol,
    *,
    receipt_id: str,
) -> tuple[Receipt, dict[str, Any], list[dict[str, Any]]]:
    """Load receipt-anchored lineage context for one outcome record."""
    receipt_store = instance.get_receipt_store()
    try:
        receipt = receipt_store.get_receipt(receipt_id)
        if receipt is None:
            raise ReceiptNotFoundError(receipt_id)
    finally:
        receipt_store.close()

    decision_context = _build_decision_context(receipt)
    trace_summaries = _build_receipt_trace_summaries(receipt)
    return receipt, decision_context, trace_summaries


def _resolve_resolution_outcome_context(
    instance: InstanceProtocol,
    *,
    resolution_id: str,
) -> tuple[GroupResolution, Any, Receipt, dict[str, Any], list[dict[str, Any]]]:
    """Load proposal-resolution lineage context for one outcome record."""
    group_store = instance.get_group_store()
    try:
        resolution = group_store.get_resolution(resolution_id)
        if resolution is None:
            raise ConfigError(f"Resolution '{resolution_id}' not found")
        group = group_store.get_group_by_resolution(resolution_id)
    finally:
        group_store.close()

    if group is None:
        raise ConfigError(f"Resolution '{resolution_id}' is not attached to a candidate group")
    if resolution.action != "approve" or not resolution.confirmed:
        raise ConfigError(
            f"Resolution '{resolution_id}' must be a confirmed approved proposal resolution"
        )
    if not group.source_workflow_name or not group.source_workflow_receipt_id:
        raise ConfigError(
            f"Resolution '{resolution_id}' is not linked to a proposal workflow receipt"
        )

    receipt_store = instance.get_receipt_store()
    try:
        receipt = receipt_store.get_receipt(group.source_workflow_receipt_id)
        if receipt is None:
            raise ReceiptNotFoundError(group.source_workflow_receipt_id)
    finally:
        receipt_store.close()

    decision_context = _build_decision_context(receipt)
    decision_context["relationship_type"] = resolution.relationship_type
    trace_summaries = _load_trace_summaries(
        instance,
        list(group.source_trace_ids),
        fallback_receipt=receipt,
    )
    return resolution, group, receipt, decision_context, trace_summaries


def service_get_outcome_profile(
    instance: InstanceProtocol,
    *,
    anchor_type: Literal["resolution", "receipt"],
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
) -> tuple[str | None, OutcomeProfileSchema | None]:
    """Resolve the focused outcome profile for one anchor context."""
    emit_python_deprecation(LEGACY_OUTCOME_PROFILE)
    config = instance.load_config()
    return _resolve_outcome_profile(
        config=config,
        anchor_type=anchor_type,
        relationship_type=relationship_type,
        workflow_name=workflow_name,
        surface_type=surface_type,
        surface_name=surface_name,
        outcome_profile_key=None,
    )


def service_get_feedback_profile(
    instance: InstanceProtocol,
    relationship_type: str,
) -> FeedbackProfileSchema | None:
    """Return the configured feedback profile for one relationship type."""
    return instance.load_config().get_feedback_profile(relationship_type)


def _feedback_target_label(target: RelationshipInstance) -> str:
    """Return a compact edge label for feedback receipt details."""
    return (
        f"{target.from_type}:{target.from_id}:"
        f"{target.relationship_type}:{target.to_type}:{target.to_id}"
    )


def _relationship_target_from_input(target: RelationshipTargetInput) -> RelationshipInstance:
    return RelationshipInstance(
        from_type=target.from_type,
        from_id=target.from_id,
        relationship_type=target.relationship_type,
        to_type=target.to_type,
        to_id=target.to_id,
        edge_key=target.edge_key,
        claim_id=target.claim_id,
    )


def _feedback_batch_item_from_input(item: FeedbackItemInput) -> FeedbackBatchItem:
    if item.receipt_id is None:
        raise ConfigError("Batch feedback items require receipt_id")
    return FeedbackBatchItem(
        receipt_id=item.receipt_id,
        action=item.action,
        target=_relationship_target_from_input(item.target),
        reason=item.reason,
        reason_code=item.reason_code,
        scope_hints=item.scope_hints or {},
        corrections=item.corrections or {},
        group_override=item.group_override,
    )


def _target_from_query_relationship_mapping(
    value: dict[str, Any],
    *,
    context: str,
) -> RelationshipInstance:
    try:
        target = RelationshipInstance.model_validate(value)
    except ValidationError as exc:
        raise ConfigError(
            f"Selected {context} is missing or has invalid relationship identity"
        ) from exc
    return RelationshipInstance(
        from_type=target.from_type,
        from_id=target.from_id,
        relationship_type=target.relationship_type,
        to_type=target.to_type,
        to_id=target.to_id,
        edge_key=target.edge_key,
        claim_id=target.claim_id,
    )


def _select_query_path_segment(
    row: dict[str, Any],
    *,
    path_index: int | None,
    path_alias: str | None,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    if path_index is not None and path_alias is not None:
        raise ConfigError("Provide either path_index or path_alias, not both")

    path = row.get("path")
    if not isinstance(path, list):
        raise ConfigError("Path query row does not contain relationship path evidence")
    if not path:
        raise ConfigError("Path query row has no selectable relationship segment")

    if path_index is None and path_alias is None:
        if len(path) == 1:
            segment = path[0]
            if not isinstance(segment, dict):
                raise ConfigError("Selected path segment is not an object")
            return 0, segment, {"path_index": 0}
        raise ConfigError("Multi-hop path query row requires path_index or path_alias")

    if path_index is not None:
        if path_index < 0 or path_index >= len(path):
            raise ConfigError(
                f"path_index {path_index} is out of range for path with {len(path)} segment(s)"
            )
        segment = path[path_index]
        if not isinstance(segment, dict):
            raise ConfigError("Selected path segment is not an object")
        return path_index, segment, {"path_index": path_index}

    assert path_alias is not None
    matches = [
        (index, segment)
        for index, segment in enumerate(path)
        if isinstance(segment, dict) and segment.get("alias") == path_alias
    ]
    if not matches:
        raise ConfigError(f"path_alias '{path_alias}' was not found in query result path")
    if len(matches) > 1:
        raise ConfigError(f"path_alias '{path_alias}' is duplicated in query result path")
    index, segment = matches[0]
    return index, segment, {"path_index": index, "path_alias": path_alias}


def _feedback_target_from_query_result(
    receipt: Receipt,
    *,
    result_index: int,
    path_index: int | None,
    path_alias: str | None,
) -> tuple[RelationshipInstance, dict[str, Any]]:
    if result_index < 0 or result_index >= len(receipt.results):
        raise ConfigError(
            f"result_index {result_index} is out of range for receipt "
            f"'{receipt.receipt_id}' with {len(receipt.results)} result(s)"
        )

    row = receipt.results[result_index]
    if not isinstance(row, dict):
        raise ConfigError(f"Query result {result_index} is not an object")
    if "values" in row and "source" in row:
        source = row.get("source")
        if source is None:
            raise ConfigError("Projected query row does not contain source relationship evidence")
        if not isinstance(source, dict):
            raise ConfigError("Projected query row source is not an object")
        row = source

    if "path" in row:
        selected_index, segment, selector = _select_query_path_segment(
            row,
            path_index=path_index,
            path_alias=path_alias,
        )
        target = _target_from_query_relationship_mapping(segment, context="path segment")
        return target, {
            "receipt_id": receipt.receipt_id,
            "result_index": result_index,
            "result_shape": "path",
            **selector,
            "resolved_target": target.model_dump(
                mode="json",
                exclude={"properties", "metadata"},
            ),
            "selected_path_alias": segment.get("alias"),
            "selected_path_index": selected_index,
        }

    if "relationship_type" in row:
        if path_index is not None or path_alias is not None:
            raise ConfigError("Relationship query rows do not accept path_index or path_alias")
        target = _target_from_query_relationship_mapping(row, context="relationship row")
        return target, {
            "receipt_id": receipt.receipt_id,
            "result_index": result_index,
            "result_shape": "relationship",
            "resolved_target": target.model_dump(
                mode="json",
                exclude={"properties", "metadata"},
            ),
        }

    if "entity_type" in row and "entity_id" in row:
        raise ConfigError(
            "Entity query rows do not contain relationship evidence and cannot be used "
            "as feedback targets"
        )

    raise ConfigError(f"Unsupported query result shape at result_index {result_index}")


def _enforce_feedback_governance(records: Iterable[FeedbackRecord]) -> None:
    """Gate the adjudication actions carried by a feedback payload.

    Two governance rails the per-TOOL permission map cannot express, both keyed
    on the payload's ``action`` (wi-feedback-approval-rail):

    1. **Tier.** ``accept``/``reject``/``correct`` adjudicate a claim and
       require GRAPH_WRITE (see ``FEEDBACK_ACTION_PERMISSIONS``). Without this,
       one GOVERNED_WRITE actor could attest an edge into ``pending`` and then
       accept their own proposal — a live approved claim on a proposal_only
       type with no reviewer above them. ``action`` is a closed Literal with no
       action-less variant, and since ``flag`` was removed EVERY action is an
       adjudication, so nothing is left at the tools' GOVERNED_WRITE floor but
       the RECORDING half of an action — which is what a refusal here rolls back
       along with the transition. A governed-tier actor registers a doubt with
       ``cruxible_attest`` (stance ``contradict``) instead.
    2. **Kill-switch.** ``CRUXIBLE_REFUSE_DIRECT_WRITES`` refuses the actions
       that transition an edge INTO accepted state, so freezing live writes
       daemon-wide cannot be walked around through feedback accept.

    Called INSIDE the ``mutation_receipt`` block so a refusal is receipted (and
    the open write transaction rolls back), exactly like a chokepoint refusal.
    That receipted position is the whole reason this is the ONLY feedback tier
    rail: the facade pre-gate it replaced could never bind more tightly than an
    action-level GRAPH_WRITE floor, and its denials landed unreceipted. A batch
    is gated at its strictest member: one adjudication action lifts the whole
    batch's requirement, and the batch stays all-or-nothing.
    """
    materialized = list(records)
    required = max(
        (
            FEEDBACK_ACTION_PERMISSIONS[record.action]
            for record in materialized
            if record.action in FEEDBACK_ACTION_PERMISSIONS
        ),
        default=PermissionMode.GOVERNED_WRITE,
    )
    if required > PermissionMode.GOVERNED_WRITE:
        check_permission(FEEDBACK_ADJUDICATION_OPERATION, required_override=required)

    for record in materialized:
        if env_refuses_feedback_acceptance(record.action):
            raise DirectWriteRefusedError(
                "feedback",
                record.target.relationship_type,
                record.action,
            )


def _apply_feedback_record(
    graph: EntityGraph,
    record: FeedbackRecord,
    *,
    group_override: bool,
) -> bool:
    """Apply one normalized feedback record and any requested group override."""
    applied = apply_feedback(graph, record)
    if group_override:
        target = record.target
        relationship = graph.get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
            edge_key=target.edge_key,
        )
        if relationship is not None:
            assertion = relationship.metadata.assertion.model_copy(update={"group_override": True})
            metadata = relationship.metadata.model_copy(update={"assertion": assertion})
            graph.update_relationship_state(
                target.from_type,
                target.from_id,
                target.to_type,
                target.to_id,
                target.relationship_type,
                metadata=metadata,
                edge_key=target.edge_key,
            )
    return applied


def _feedback_relationship_after_apply(
    graph: EntityGraph,
    target: RelationshipInstance,
) -> RelationshipInstance | None:
    """Return the final relationship row touched by feedback, when present."""
    return graph.get_relationship(
        target.from_type,
        target.from_id,
        target.to_type,
        target.to_id,
        target.relationship_type,
        edge_key=target.edge_key,
    )


def service_feedback_input(
    instance: InstanceProtocol,
    item: FeedbackItemInput,
    *,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackServiceResult:
    """Normalize one feedback input payload, then record edge feedback."""
    return service_feedback(
        instance,
        receipt_id=item.receipt_id,
        action=item.action,
        target=_relationship_target_from_input(item.target),
        reason=item.reason,
        reason_code=item.reason_code,
        scope_hints=item.scope_hints,
        corrections=item.corrections,
        group_override=item.group_override,
        actor_context=actor_context,
    )


def service_feedback_from_query_result(
    instance: InstanceProtocol,
    *,
    receipt_id: str,
    result_index: int,
    action: FeedbackAction,
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
    path_index: int | None = None,
    path_alias: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackServiceResult:
    """Record edge feedback by selecting relationship evidence from a query receipt."""
    _validate_feedback_request_values(
        action=action,
        corrections=corrections,
    )
    receipt = service_get_receipt(instance, receipt_id)
    if receipt.operation_type != "query":
        raise ConfigError(
            f"Receipt '{receipt_id}' has operation_type '{receipt.operation_type}', not 'query'"
        )
    target, query_selection = _feedback_target_from_query_result(
        receipt,
        result_index=result_index,
        path_index=path_index,
        path_alias=path_alias,
    )
    graph = instance.load_graph()
    if (
        graph.get_relationship(
            target.from_type,
            target.from_id,
            target.to_type,
            target.to_id,
            target.relationship_type,
            edge_key=target.edge_key,
        )
        is None
    ):
        raise ConfigError("Selected query relationship target was not found in the graph")
    return service_feedback(
        instance,
        receipt_id=receipt_id,
        action=action,
        target=target,
        reason=reason,
        reason_code=reason_code,
        scope_hints=scope_hints,
        corrections=corrections,
        group_override=group_override,
        _feedback_from_query={
            **query_selection,
            "action": action,
            "actor_kind": derived_actor_kind(actor_context),
            "reason": reason,
            "reason_code": reason_code,
            "scope_hints": scope_hints or {},
        },
        actor_context=actor_context,
    )


def service_feedback(
    instance: InstanceProtocol,
    receipt_id: str | None,
    action: FeedbackAction,
    target: RelationshipInstance,
    reason: str = "",
    reason_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    corrections: dict[str, Any] | None = None,
    group_override: bool = False,
    _feedback_from_query: dict[str, Any] | None = None,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackServiceResult:
    """Record feedback on an edge.

    Validates corrections, checks receipt existence, persists feedback,
    and applies to the graph. If group_override=True, marks the edge assertion
    metadata as a group override after applying feedback.
    """
    if group_override:
        emit_python_deprecation(GROUP_OVERRIDE)
    _validate_feedback_request_values(
        action=action,
        corrections=corrections,
    )
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[target.relationship_type],
    )
    config = instance.load_config()
    graph = instance.load_graph()
    receipt = _load_receipts(instance, [receipt_id])[receipt_id] if receipt_id is not None else None
    record = _normalize_feedback_record(
        config=config,
        graph=graph,
        receipt=receipt,
        receipt_id=receipt_id,
        action=action,
        target=target,
        reason=reason,
        reason_code=reason_code,
        scope_hints=scope_hints,
        corrections=corrections,
        group_override=group_override,
        actor_context=actor_context,
    )

    receipt_parameters: dict[str, Any] = {
        "source_receipt_id": receipt_id,
        "action": action,
        "actor_kind": derived_actor_kind(actor_context),
        "target": _feedback_target_label(target),
    }
    if _feedback_from_query is not None:
        receipt_parameters["feedback_from_query"] = {
            **_feedback_from_query,
            "action": action,
        }

    with mutation_receipt(
        instance,
        "feedback",
        receipt_parameters,
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        _enforce_feedback_governance([record])
        ctx.uow.feedback.save_feedback_batch([record])

        applied = _apply_feedback_record(
            graph,
            record,
            group_override=group_override,
        )
        ctx.builder.record_feedback_applied(
            _feedback_target_label(record.target),
            action,
            applied,
        )

        touched = _feedback_relationship_after_apply(graph, record.target)
        save_graph_for_mutation(
            instance,
            graph,
            entities=[],
            relationships=[touched] if touched is not None else [],
            uow=ctx.uow,
        )

        ctx.set_result(FeedbackServiceResult(feedback_id=record.feedback_id, applied=applied))

    result = ctx.result
    assert isinstance(result, FeedbackServiceResult)
    return result


def service_feedback_batch_inputs(
    instance: InstanceProtocol,
    items: list[FeedbackItemInput],
    *,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackBatchServiceResult:
    """Normalize batch feedback input payloads, then record them together."""
    return service_feedback_batch(
        instance,
        [_feedback_batch_item_from_input(item) for item in items],
        actor_context=actor_context,
    )


def service_feedback_batch(
    instance: InstanceProtocol,
    items: list[FeedbackBatchItem],
    *,
    actor_context: GovernedActorContext | None = None,
) -> FeedbackBatchServiceResult:
    """Record a batch of edge feedback with one top-level receipt."""
    if not items:
        raise ConfigError("Batch feedback items must not be empty")
    if any(item.group_override for item in items):
        emit_python_deprecation(GROUP_OVERRIDE)
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[item.target.relationship_type for item in items],
    )

    for item in items:
        _validate_feedback_request_values(
            action=item.action,
            corrections=item.corrections,
        )

    graph = instance.load_graph()
    config = instance.load_config()
    receipts = _load_receipts(instance, {item.receipt_id for item in items})

    records = [
        _normalize_feedback_record(
            config=config,
            graph=graph,
            receipt=receipts[item.receipt_id],
            receipt_id=item.receipt_id,
            action=item.action,
            target=item.target,
            reason=item.reason,
            reason_code=item.reason_code,
            scope_hints=item.scope_hints,
            corrections=item.corrections,
            group_override=item.group_override,
            actor_context=actor_context,
        )
        for item in items
    ]

    with mutation_receipt(
        instance,
        "feedback_batch",
        {"count": len(items), "actor_kind": derived_actor_kind(actor_context)},
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        _enforce_feedback_governance(records)
        for index, record in enumerate(records, start=1):
            ctx.builder.record_validation(
                passed=True,
                detail={
                    "index": index,
                    "receipt_id": record.receipt_id,
                    "action": record.action,
                },
            )

        ctx.uow.feedback.save_feedback_batch(records)

        applied_count = 0
        touched_relationships: list[RelationshipInstance] = []
        for record, item in zip(records, items, strict=True):
            applied = _apply_feedback_record(
                graph,
                record,
                group_override=item.group_override,
            )
            if applied:
                applied_count += 1
            ctx.builder.record_feedback_applied(
                _feedback_target_label(record.target),
                record.action,
                applied,
            )
            touched = _feedback_relationship_after_apply(graph, record.target)
            if touched is not None:
                touched_relationships.append(touched)

        save_graph_for_mutation(
            instance,
            graph,
            entities=[],
            relationships=touched_relationships,
            uow=ctx.uow,
        )

        ctx.set_result(
            FeedbackBatchServiceResult(
                feedback_ids=[record.feedback_id for record in records],
                applied_count=applied_count,
                total=len(records),
            )
        )

    result = ctx.result
    assert isinstance(result, FeedbackBatchServiceResult)
    return result


def service_outcome(
    instance: InstanceProtocol,
    outcome: Literal["correct", "incorrect", "partial", "unknown"],
    receipt_id: str | None = None,
    *,
    anchor_type: Literal["resolution", "receipt"] = "receipt",
    anchor_id: str | None = None,
    outcome_code: str | None = None,
    scope_hints: dict[str, Any] | None = None,
    outcome_profile_key: str | None = None,
    detail: dict[str, Any] | None = None,
    actor_context: GovernedActorContext | None = None,
) -> OutcomeServiceResult:
    """Record an anchored outcome for a prior receipt or proposal resolution.

    Validates the anchor, resolves an outcome profile when available,
    and persists a bounded lineage snapshot for later analysis.
    """
    emit_python_deprecation(LEGACY_OUTCOME_RECORD)
    _validate_outcome_request_values(
        outcome=outcome,
        detail=detail,
    )
    normalized_scope_hints = dict(scope_hints or {})
    config = instance.load_config()
    normalized_receipt_id: str
    normalized_anchor_id: str | None

    if anchor_type == "receipt":
        receipt_id_candidate = anchor_id or receipt_id
        if not receipt_id_candidate:
            raise ConfigError("Receipt outcomes require receipt_id or anchor_id")
        normalized_receipt_id = receipt_id_candidate
        receipt, decision_context, trace_summaries = _resolve_receipt_outcome_context(
            instance,
            receipt_id=normalized_receipt_id,
        )
        resolved_profile_key, profile = _resolve_outcome_profile(
            config=config,
            anchor_type="receipt",
            relationship_type=None,
            workflow_name=None,
            surface_type=str(decision_context.get("surface_type") or ""),
            surface_name=str(decision_context.get("surface_name") or ""),
            outcome_profile_key=outcome_profile_key,
        )
        _validate_outcome_inputs(
            profile=profile,
            profile_key=resolved_profile_key,
            outcome_code=outcome_code,
            scope_hints=normalized_scope_hints,
            actor_kind=derived_actor_kind(actor_context),
        )
        lineage_snapshot = _build_receipt_lineage_snapshot(
            receipt=receipt,
            trace_summaries=trace_summaries,
        )
        relationship_type = None
        normalized_anchor_id = normalized_receipt_id
        normalized_receipt_id = receipt.receipt_id
    else:
        normalized_anchor_id = anchor_id
        if not normalized_anchor_id:
            raise ConfigError("Resolution outcomes require anchor_id")
        resolution, group, receipt, decision_context, trace_summaries = (
            _resolve_resolution_outcome_context(instance, resolution_id=normalized_anchor_id)
        )
        resolved_profile_key, profile = _resolve_outcome_profile(
            config=config,
            anchor_type="resolution",
            relationship_type=resolution.relationship_type,
            workflow_name=group.source_workflow_name,
            surface_type=None,
            surface_name=None,
            outcome_profile_key=outcome_profile_key,
        )
        _validate_outcome_inputs(
            profile=profile,
            profile_key=resolved_profile_key,
            outcome_code=outcome_code,
            scope_hints=normalized_scope_hints,
            actor_kind=derived_actor_kind(actor_context),
        )
        lineage_snapshot = _build_resolution_lineage_snapshot(
            profile=profile,
            resolution=resolution,
            group=group,
            trace_summaries=trace_summaries,
        )
        relationship_type = resolution.relationship_type
        normalized_receipt_id = receipt.receipt_id

    outcome_remediation_hint: OutcomeRemediationHint | None = None
    if profile is not None and outcome_code is not None:
        outcome_remediation_hint = profile.outcome_codes[outcome_code].remediation_hint

    record = OutcomeRecord(
        receipt_id=normalized_receipt_id,
        anchor_type=anchor_type,
        anchor_id=normalized_anchor_id,
        outcome=outcome,
        outcome_code=outcome_code,
        outcome_remediation_hint=outcome_remediation_hint,
        scope_hints=normalized_scope_hints,
        outcome_profile_key=resolved_profile_key,
        outcome_profile_version=profile.version if profile is not None else None,
        outcome_profile_digest=profile.body_digest() if profile is not None else None,
        decision_context=decision_context,
        lineage_snapshot=lineage_snapshot,
        relationship_type=relationship_type,
        detail=detail or {},
        actor_context=actor_context,
    )
    with instance.write_transaction() as uow:
        uow.feedback.save_outcome(record)

    return OutcomeServiceResult(outcome_id=record.outcome_id)
