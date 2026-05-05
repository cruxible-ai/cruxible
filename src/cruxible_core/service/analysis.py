"""Analysis service functions — find_candidates, evaluate, analyze_feedback, lint."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from cruxible_core.config.schema import FeedbackRemediationHint, OutcomeRemediationHint
from cruxible_core.config.validator import validate_config
from cruxible_core.errors import ConfigError
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.query.candidates import CandidateMatch, MatchRule, find_candidates
from cruxible_core.query.evaluate import EvaluationReport, evaluate_graph
from cruxible_core.service.types import (
    AnalyzeFeedbackResult,
    AnalyzeOutcomesResult,
    ConstraintSuggestion,
    DebugPackage,
    DecisionPolicySuggestion,
    FeedbackGroupSummary,
    LintServiceResult,
    LintSummary,
    OutcomeDecisionPolicySuggestion,
    OutcomeGroupSummary,
    OutcomeProviderFixCandidate,
    ProviderFixCandidate,
    QualityCheckCandidate,
    QueryPolicySuggestion,
    TrustAdjustmentSuggestion,
    UncodedFeedbackExample,
    UncodedOutcomeExample,
)


def service_find_candidates(
    instance: InstanceProtocol,
    relationship_type: str,
    strategy: Literal["property_match", "shared_neighbors"],
    match_rules: list[MatchRule] | None = None,
    via_relationship: str | None = None,
    min_overlap: float = 0.5,
    min_confidence: float = 0.5,
    limit: int = 20,
    min_distinct_neighbors: int = 2,
) -> list[CandidateMatch]:
    """Find candidate relationships using a deterministic strategy."""
    _VALID_STRATEGIES = ("property_match", "shared_neighbors")
    if strategy not in _VALID_STRATEGIES:
        raise ConfigError(f"Invalid strategy '{strategy}'. Use: {', '.join(_VALID_STRATEGIES)}")

    if min_distinct_neighbors < 1:
        raise ConfigError("min_distinct_neighbors must be >= 1")

    config = instance.load_config()
    graph = instance.load_graph()

    return find_candidates(
        config,
        graph,
        relationship_type,
        strategy,
        match_rules=match_rules,
        via_relationship=via_relationship,
        min_overlap=min_overlap,
        min_confidence=min_confidence,
        limit=limit,
        min_distinct_neighbors=min_distinct_neighbors,
    )


def service_evaluate(
    instance: InstanceProtocol,
    max_findings: int = 100,
    exclude_orphan_types: list[str] | None = None,
) -> EvaluationReport:
    """Evaluate graph quality with deterministic checks."""
    config = instance.load_config()
    graph = instance.load_graph()
    group_store = instance.get_group_store()
    try:
        return evaluate_graph(
            config,
            graph,
            group_store=group_store,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
        )
    finally:
        group_store.close()


def service_config_compatibility_warnings(instance: InstanceProtocol) -> list[str]:
    """Check whether graph contents still match the active config surface."""
    return _compute_config_compatibility_warnings(
        config=instance.load_config(),
        graph=instance.load_graph(),
    )


def service_lint(
    instance: InstanceProtocol,
    *,
    max_findings: int = 100,
    analysis_limit: int = 200,
    min_support: int = 5,
    exclude_orphan_types: list[str] | None = None,
) -> LintServiceResult:
    """Aggregate deterministic maintenance checks for one instance."""
    config = instance.load_config()
    graph = instance.load_graph()

    try:
        config_warnings = validate_config(config)
    except ConfigError as exc:
        config_warnings = [f"[ERROR] {e}" for e in exc.errors]

    compatibility_warnings = _compute_config_compatibility_warnings(config=config, graph=graph)
    group_store = instance.get_group_store()
    try:
        evaluation = evaluate_graph(
            config,
            graph,
            group_store=group_store,
            max_findings=max_findings,
            exclude_orphan_types=exclude_orphan_types,
        )
    finally:
        group_store.close()

    feedback_reports: list[AnalyzeFeedbackResult] = []
    for relationship in config.relationships:
        report = service_analyze_feedback(
            instance,
            relationship.name,
            limit=analysis_limit,
            min_support=min_support,
        )
        if _feedback_report_has_issues(report):
            feedback_reports.append(report)

    outcome_reports: list[AnalyzeOutcomesResult] = []
    for anchor_type in ("receipt", "resolution"):
        report = service_analyze_outcomes(
            instance,
            anchor_type=anchor_type,
            limit=analysis_limit,
            min_support=min_support,
        )
        if _outcome_report_has_issues(report):
            outcome_reports.append(report)

    summary = LintSummary(
        config_warning_count=len(config_warnings),
        compatibility_warning_count=len(compatibility_warnings),
        evaluation_finding_count=len(evaluation.findings),
        feedback_report_count=len(feedback_reports),
        feedback_issue_count=sum(_feedback_issue_count(report) for report in feedback_reports),
        outcome_report_count=len(outcome_reports),
        outcome_issue_count=sum(_outcome_issue_count(report) for report in outcome_reports),
    )

    has_issues = any(
        (
            summary.config_warning_count,
            summary.compatibility_warning_count,
            summary.evaluation_finding_count,
            summary.feedback_issue_count,
            summary.outcome_issue_count,
        )
    )

    return LintServiceResult(
        config_name=config.name,
        config_warnings=config_warnings,
        compatibility_warnings=compatibility_warnings,
        evaluation=evaluation,
        feedback_reports=feedback_reports,
        outcome_reports=outcome_reports,
        summary=summary,
        has_issues=has_issues,
    )


def service_analyze_feedback(
    instance: InstanceProtocol,
    relationship_type: str,
    *,
    limit: int = 200,
    min_support: int = 5,
    decision_surface_type: str | None = None,
    decision_surface_name: str | None = None,
    property_pairs: list[tuple[str, str]] | None = None,
) -> AnalyzeFeedbackResult:
    """Analyze structured feedback into deterministic remediation suggestions."""
    config = instance.load_config()
    rel = config.get_relationship(relationship_type)
    if rel is None:
        raise ConfigError(f"Relationship type '{relationship_type}' not found in config")

    profile = config.get_feedback_profile(relationship_type)
    store = instance.get_feedback_store()
    try:
        feedback_rows = store.list_feedback(
            relationship_type=relationship_type,
            decision_surface_type=decision_surface_type,
            decision_surface_name=decision_surface_name,
            limit=limit,
        )
    finally:
        store.close()

    action_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    reason_code_counts: dict[str, int] = {}
    warnings: list[str] = []
    warning_keys: set[str] = set()
    coded_groups: dict[
        tuple[str, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]],
        list[FeedbackRecord],
    ] = defaultdict(list)
    uncoded_feedback: list[FeedbackRecord] = []

    for row in feedback_rows:
        action_counts[row.action] = action_counts.get(row.action, 0) + 1
        source_counts[row.source] = source_counts.get(row.source, 0) + 1
        if row.reason_code:
            reason_code_counts[row.reason_code] = reason_code_counts.get(row.reason_code, 0) + 1
        if row.action != "reject":
            continue
        if row.reason_code is None:
            uncoded_feedback.append(row)
            continue
        group_key = (
            row.reason_code,
            _freeze_mapping(row.decision_context),
            _freeze_mapping(row.scope_hints),
        )
        coded_groups[group_key].append(row)

    coded_group_results: list[FeedbackGroupSummary] = []
    decision_policy_suggestions: list[DecisionPolicySuggestion] = []
    quality_check_candidates: list[QualityCheckCandidate] = []
    provider_fix_candidates: list[ProviderFixCandidate] = []
    used_policy_names: set[str] = {policy.name for policy in config.decision_policies}
    constraint_rows: list[FeedbackRecord] = []

    for (reason_code, frozen_context, frozen_scope), rows in coded_groups.items():
        decision_context = dict(frozen_context)
        scope_hints = dict(frozen_scope)
        remediation_hint = _resolve_group_remediation_hint(
            relationship_type=relationship_type,
            profile=profile,
            reason_code=reason_code,
            rows=rows,
            warnings=warnings,
            warning_keys=warning_keys,
        )
        coded_group_results.append(
            FeedbackGroupSummary(
                relationship_type=relationship_type,
                reason_code=reason_code,
                remediation_hint=remediation_hint,
                decision_context=decision_context,
                scope_hints=scope_hints,
                feedback_count=len(rows),
                feedback_ids=[row.feedback_id for row in rows[:5]],
                sample_reasons=[row.reason for row in rows if row.reason][:3],
            )
        )

        if len(rows) < min_support:
            continue
        if remediation_hint == "constraint":
            constraint_rows.extend(rows)
        elif remediation_hint == "decision_policy":
            suggestion = _build_decision_policy_suggestion(
                config=config,
                relationship_type=relationship_type,
                profile=profile,
                used_names=used_policy_names,
                reason_code=reason_code,
                decision_context=decision_context,
                scope_hints=scope_hints,
                rows=rows,
            )
            if suggestion is not None:
                used_policy_names.add(suggestion.name)
                decision_policy_suggestions.append(suggestion)
        elif remediation_hint == "quality_check":
            quality_check_candidates.append(
                QualityCheckCandidate(
                    relationship_type=relationship_type,
                    reason_code=reason_code,
                    support_count=len(rows),
                    description=(
                        f"Repeated rejected feedback for reason_code '{reason_code}' "
                        f"on relationship '{relationship_type}'"
                    ),
                    feedback_ids=[row.feedback_id for row in rows[:5]],
                )
            )
        elif remediation_hint == "provider_fix":
            provider_fix_candidates.append(
                ProviderFixCandidate(
                    relationship_type=relationship_type,
                    reason_code=reason_code,
                    support_count=len(rows),
                    description=(
                        f"Repeated rejected feedback for reason_code '{reason_code}' "
                        f"suggests a provider/workflow normalization issue"
                    ),
                    feedback_ids=[row.feedback_id for row in rows[:5]],
                )
            )

    constraint_suggestions = _build_constraint_suggestions(
        config=config,
        relationship_type=relationship_type,
        rows=constraint_rows,
        property_pairs=property_pairs,
        min_support=min_support,
        warnings=warnings,
        warning_keys=warning_keys,
    )

    uncoded_examples = [
        UncodedFeedbackExample(
            feedback_id=row.feedback_id,
            relationship_type=relationship_type,
            reason=row.reason,
            target=row.target,
            decision_context=row.decision_context,
            scope_hints=row.scope_hints,
        )
        for row in uncoded_feedback[:5]
    ]

    return AnalyzeFeedbackResult(
        relationship_type=relationship_type,
        feedback_count=len(feedback_rows),
        action_counts=action_counts,
        source_counts=source_counts,
        reason_code_counts=reason_code_counts,
        coded_groups=sorted(
            coded_group_results,
            key=lambda item: item.feedback_count,
            reverse=True,
        ),
        uncoded_feedback_count=len(uncoded_feedback),
        uncoded_examples=uncoded_examples,
        constraint_suggestions=constraint_suggestions,
        decision_policy_suggestions=decision_policy_suggestions,
        quality_check_candidates=quality_check_candidates,
        provider_fix_candidates=provider_fix_candidates,
        warnings=warnings,
    )


def service_analyze_outcomes(
    instance: InstanceProtocol,
    *,
    anchor_type: Literal["resolution", "receipt"],
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    query_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
    limit: int = 200,
    min_support: int = 5,
) -> AnalyzeOutcomesResult:
    """Analyze anchored outcomes into trust and debugging suggestions."""
    if anchor_type not in {"resolution", "receipt"}:
        raise ConfigError("anchor_type must be 'resolution' or 'receipt'")

    normalized_surface_type, normalized_surface_name = _normalize_outcome_surface_filters(
        query_name=query_name,
        workflow_name=workflow_name,
        surface_type=surface_type,
        surface_name=surface_name,
    )

    config = instance.load_config()
    store = instance.get_feedback_store()
    try:
        outcome_rows = store.list_outcomes(
            anchor_type=anchor_type,
            relationship_type=relationship_type,
            decision_surface_type=normalized_surface_type,
            decision_surface_name=normalized_surface_name,
            limit=limit,
        )
    finally:
        store.close()

    outcome_counts: dict[str, int] = {}
    outcome_code_counts: dict[str, int] = {}
    warnings: list[str] = []
    warning_keys: set[str] = set()
    coded_groups: dict[
        tuple[str, tuple[tuple[str, Any], ...], tuple[tuple[str, Any], ...]],
        list[OutcomeRecord],
    ] = defaultdict(list)
    uncoded_outcomes: list[OutcomeRecord] = []

    for row in outcome_rows:
        outcome_counts[row.outcome] = outcome_counts.get(row.outcome, 0) + 1
        if row.outcome_code:
            outcome_code_counts[row.outcome_code] = outcome_code_counts.get(row.outcome_code, 0) + 1
            group_key = (
                row.outcome_code,
                _freeze_mapping(row.decision_context),
                _freeze_mapping(row.scope_hints),
            )
            coded_groups[group_key].append(row)
        else:
            uncoded_outcomes.append(row)

    coded_group_results: list[OutcomeGroupSummary] = []
    trust_adjustment_suggestions: list[TrustAdjustmentSuggestion] = []
    workflow_review_policy_suggestions: list[OutcomeDecisionPolicySuggestion] = []
    query_policy_suggestions: list[QueryPolicySuggestion] = []
    provider_fix_candidates: list[OutcomeProviderFixCandidate] = []
    used_policy_names: set[str] = {policy.name for policy in config.decision_policies}

    for (outcome_code, frozen_context, frozen_scope), rows in coded_groups.items():
        decision_context = dict(frozen_context)
        scope_hints = dict(frozen_scope)
        remediation_hint = _resolve_outcome_group_remediation_hint(
            config=config,
            outcome_code=outcome_code,
            rows=rows,
            warnings=warnings,
            warning_keys=warning_keys,
        )
        outcome_breakdown = _count_outcomes(rows)
        coded_group_results.append(
            OutcomeGroupSummary(
                anchor_type=anchor_type,
                outcome_code=outcome_code,
                remediation_hint=remediation_hint,
                decision_context=decision_context,
                scope_hints=scope_hints,
                outcome_count=len(rows),
                outcome_counts=outcome_breakdown,
                outcome_ids=[row.outcome_id for row in rows[:5]],
            )
        )

        if len(rows) < min_support:
            continue

        if anchor_type == "resolution":
            if remediation_hint == "trust_adjustment":
                suggestion = _build_trust_adjustment_suggestion(
                    instance=instance,
                    rows=rows,
                    outcome_code=outcome_code,
                    warnings=warnings,
                    warning_keys=warning_keys,
                )
                if suggestion is not None:
                    trust_adjustment_suggestions.append(suggestion)
            if remediation_hint in {"require_review", "decision_policy"}:
                policy_suggestion = _build_workflow_review_policy_suggestion(
                    used_names=used_policy_names,
                    rows=rows,
                    outcome_code=outcome_code,
                )
                if policy_suggestion is not None:
                    used_policy_names.add(policy_suggestion.name)
                    workflow_review_policy_suggestions.append(policy_suggestion)
        else:
            if remediation_hint == "decision_policy":
                query_suggestion = _build_query_policy_suggestion(
                    rows=rows,
                    outcome_code=outcome_code,
                )
                if query_suggestion is not None:
                    query_policy_suggestions.append(query_suggestion)
            if remediation_hint in {"provider_fix", "workflow_fix"}:
                fix_candidate = _build_outcome_provider_fix_candidate(
                    rows=rows,
                    outcome_code=outcome_code,
                )
                if fix_candidate is not None:
                    provider_fix_candidates.append(fix_candidate)

    uncoded_examples = [
        UncodedOutcomeExample(
            outcome_id=row.outcome_id,
            anchor_type=row.anchor_type,
            anchor_id=row.anchor_id or row.receipt_id,
            outcome=row.outcome,
            detail=row.detail,
            decision_context=row.decision_context,
            scope_hints=row.scope_hints,
        )
        for row in uncoded_outcomes[:5]
    ]

    debug_packages = (
        _build_debug_packages(outcome_rows, min_support=min_support)
        if anchor_type == "resolution"
        else []
    )
    workflow_debug_packages = (
        _build_debug_packages(outcome_rows, min_support=min_support)
        if anchor_type == "receipt"
        else []
    )

    return AnalyzeOutcomesResult(
        anchor_type=anchor_type,
        outcome_count=len(outcome_rows),
        outcome_counts=outcome_counts,
        outcome_code_counts=outcome_code_counts,
        coded_groups=sorted(
            coded_group_results,
            key=lambda item: item.outcome_count,
            reverse=True,
        ),
        uncoded_outcome_count=len(uncoded_outcomes),
        uncoded_examples=uncoded_examples,
        trust_adjustment_suggestions=trust_adjustment_suggestions,
        workflow_review_policy_suggestions=workflow_review_policy_suggestions,
        query_policy_suggestions=query_policy_suggestions,
        provider_fix_candidates=provider_fix_candidates,
        debug_packages=debug_packages,
        workflow_debug_packages=workflow_debug_packages,
        warnings=warnings,
    )


def _freeze_mapping(mapping: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """Build a stable tuple key for exact grouping."""
    return tuple(sorted((key, _freeze_value(value)) for key, value in mapping.items()))


def _compute_config_compatibility_warnings(*, config, graph) -> list[str]:
    """Check if graph contents are compatible with the current config."""
    warnings: list[str] = []

    config_entity_types = set(config.entity_types.keys())
    for graph_type in graph.list_entity_types():
        if graph_type not in config_entity_types:
            count = graph.entity_count(graph_type)
            warnings.append(
                f"Entity type '{graph_type}' exists in graph ({count} entities) "
                "but is missing from config"
            )

    config_rel_types = {relationship.name for relationship in config.relationships}
    for graph_rel in graph.list_relationship_types():
        if graph_rel not in config_rel_types:
            count = graph.edge_count(graph_rel)
            warnings.append(
                f"Relationship type '{graph_rel}' exists in graph ({count} edges) "
                "but is missing from config"
            )

    return warnings


def _feedback_report_has_issues(result: AnalyzeFeedbackResult) -> bool:
    """Return whether a feedback analysis report contains actionable maintenance work."""
    return _feedback_issue_count(result) > 0


def _feedback_issue_count(result: AnalyzeFeedbackResult) -> int:
    """Count actionable items in a feedback analysis report."""
    return (
        len(result.warnings)
        + result.uncoded_feedback_count
        + len(result.constraint_suggestions)
        + len(result.decision_policy_suggestions)
        + len(result.quality_check_candidates)
        + len(result.provider_fix_candidates)
    )


def _outcome_report_has_issues(result: AnalyzeOutcomesResult) -> bool:
    """Return whether an outcome analysis report contains actionable maintenance work."""
    return _outcome_issue_count(result) > 0


def _outcome_issue_count(result: AnalyzeOutcomesResult) -> int:
    """Count actionable items in an outcome analysis report."""
    return (
        len(result.warnings)
        + result.uncoded_outcome_count
        + len(result.trust_adjustment_suggestions)
        + len(result.workflow_review_policy_suggestions)
        + len(result.query_policy_suggestions)
        + len(result.provider_fix_candidates)
        + len(result.debug_packages)
        + len(result.workflow_debug_packages)
    )


def _normalize_outcome_surface_filters(
    *,
    query_name: str | None,
    workflow_name: str | None,
    surface_type: str | None,
    surface_name: str | None,
) -> tuple[str | None, str | None]:
    """Normalize analyze-outcomes surface filters into one exact surface pair."""
    if query_name is not None and workflow_name is not None:
        raise ConfigError("Specify at most one of query_name or workflow_name")

    if query_name is not None:
        if surface_type not in (None, "query"):
            raise ConfigError("query_name requires surface_type='query'")
        if surface_name not in (None, query_name):
            raise ConfigError("surface_name must match query_name when both are provided")
        return "query", query_name

    if workflow_name is not None:
        if surface_type not in (None, "workflow"):
            raise ConfigError("workflow_name requires surface_type='workflow'")
        if surface_name not in (None, workflow_name):
            raise ConfigError("surface_name must match workflow_name when both are provided")
        return "workflow", workflow_name

    return surface_type, surface_name


def _freeze_value(value: Any) -> Any:
    """Normalize nested values into hashable tuples for grouping."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze_value(val)) for key, val in value.items()))
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _build_decision_policy_suggestion(
    *,
    config,
    relationship_type: str,
    profile,
    used_names: set[str],
    reason_code: str,
    decision_context: dict[str, Any],
    scope_hints: dict[str, Any],
    rows: list[FeedbackRecord],
) -> DecisionPolicySuggestion | None:
    """Build a scoped decision policy suggestion from one coded feedback group."""
    if profile is None or not scope_hints:
        return None

    surface_type = decision_context.get("surface_type")
    surface_name = decision_context.get("surface_name")
    if surface_type not in {"query", "workflow"} or not surface_name:
        return None

    match: dict[str, Any] = {"from": {}, "to": {}, "edge": {}, "context": {}}
    for scope_key, value in scope_hints.items():
        path = profile.scope_keys.get(scope_key)
        if path is None:
            continue
        side, _, prop_name = path.partition(".")
        if side == "FROM":
            match["from"][prop_name] = value
        elif side == "TO":
            match["to"][prop_name] = value
        else:
            match["edge"][prop_name] = value

    if surface_type == "query":
        applies_to = "query"
        effect = "suppress"
        query_name = surface_name
        workflow_name = None
    else:
        applies_to = "workflow"
        effect = "require_review"
        query_name = None
        workflow_name = surface_name

    if not any(match[side] for side in ("from", "to", "edge")):
        return None

    match["context"]["relationship_type"] = relationship_type
    match["context"][f"{surface_type}_name"] = surface_name
    name = _dedupe_name(
        used_names,
        f"{relationship_type}_{reason_code}_{surface_type}",
    )
    return DecisionPolicySuggestion(
        name=name,
        description=(
            f"Suggested from {len(rows)} rejected feedback records for reason_code "
            f"'{reason_code}'"
        ),
        relationship_type=relationship_type,
        applies_to=applies_to,
        effect=effect,
        rationale=rows[0].reason or f"Repeated feedback for reason_code '{reason_code}'",
        match=match,
        query_name=query_name,
        workflow_name=workflow_name,
        support_count=len(rows),
        feedback_ids=[row.feedback_id for row in rows[:5]],
    )


def _build_constraint_suggestions(
    *,
    config,
    relationship_type: str,
    rows: list[FeedbackRecord],
    property_pairs: list[tuple[str, str]] | None,
    min_support: int,
    warnings: list[str],
    warning_keys: set[str],
) -> list[ConstraintSuggestion]:
    """Build constraint suggestions from repeated endpoint mismatches."""
    rel = config.get_relationship(relationship_type)
    if rel is None:
        return []
    from_schema = config.get_entity_type(rel.from_entity)
    to_schema = config.get_entity_type(rel.to_entity)
    if from_schema is None or to_schema is None:
        return []

    pairs = property_pairs or [
        (name, name)
        for name, prop in from_schema.properties.items()
        if name in to_schema.properties
        and prop.type != "json"
        and to_schema.properties[name].type != "json"
    ]
    existing_rules = {constraint.rule for constraint in config.constraints}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    missing_snapshot_counts: dict[tuple[str, str], int] = defaultdict(int)

    for row in rows:
        snapshot = row.context_snapshot or {}
        from_props = _snapshot_properties(snapshot, "from")
        to_props = _snapshot_properties(snapshot, "to")
        for from_prop, to_prop in pairs:
            if from_prop not in from_props or to_prop not in to_props:
                missing_snapshot_counts[(from_prop, to_prop)] += 1
                continue
            from_val = from_props[from_prop]
            to_val = to_props[to_prop]
            if from_val is None or to_val is None or from_val == to_val:
                continue
            grouped[(from_prop, to_prop)].append(
                {
                    "feedback_id": row.feedback_id,
                    "from_value": from_val,
                    "to_value": to_val,
                }
            )

    for (from_prop, to_prop), skipped in sorted(missing_snapshot_counts.items()):
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"snapshot:{relationship_type}:{from_prop}:{to_prop}",
            message=(
                f"Feedback snapshots for relationship '{relationship_type}' do not include "
                f"properties needed for mismatch analysis ({from_prop} -> {to_prop}); "
                f"skipped {skipped} row(s)"
            ),
        )

    suggestions: list[ConstraintSuggestion] = []
    used_names = {constraint.name for constraint in config.constraints}
    for (from_prop, to_prop), items in sorted(grouped.items()):
        if len(items) < min_support:
            continue
        rule = f"{relationship_type}.FROM.{from_prop} == {relationship_type}.TO.{to_prop}"
        if rule in existing_rules:
            continue
        name = _dedupe_name(used_names, f"{relationship_type}_{from_prop}_eq_{to_prop}")
        suggestions.append(
            ConstraintSuggestion(
                name=name,
                description=(
                    f"Suggested from {len(items)} rejected feedback records showing "
                    f"{from_prop} != {to_prop}"
                ),
                relationship_type=relationship_type,
                rule=rule,
                severity="warning",
                support_count=len(items),
                feedback_ids=[item["feedback_id"] for item in items[:5]],
                sample_value_pairs=items[:3],
            )
        )
        used_names.add(name)
    return suggestions


def _resolve_group_remediation_hint(
    *,
    relationship_type: str,
    profile,
    reason_code: str,
    rows: list[FeedbackRecord],
    warnings: list[str],
    warning_keys: set[str],
) -> FeedbackRemediationHint:
    """Resolve one group's remediation lane without reinterpreting old rows."""
    hints = {
        hint
        for hint in (
            _resolve_row_remediation_hint(
                relationship_type=relationship_type,
                profile=profile,
                reason_code=reason_code,
                row=row,
                warnings=warnings,
                warning_keys=warning_keys,
            )
            for row in rows
        )
        if hint is not None
    }
    if not hints:
        return "unknown"
    if len(hints) > 1:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=(
                "mixed-remediation:"
                f"{relationship_type}:{reason_code}:{_freeze_mapping(rows[0].decision_context)}:"
                f"{_freeze_mapping(rows[0].scope_hints)}"
            ),
            message=(
                f"Feedback group '{relationship_type}/{reason_code}' has mixed remediation "
                "hints across stored feedback rows; automated suggestions were skipped"
            ),
        )
        return "unknown"
    return next(iter(hints))


def _resolve_outcome_group_remediation_hint(
    *,
    config,
    outcome_code: str,
    rows: list[OutcomeRecord],
    warnings: list[str],
    warning_keys: set[str],
) -> OutcomeRemediationHint:
    """Resolve one outcome group's remediation lane without reinterpreting old rows."""
    hints = {
        hint
        for hint in (
            _resolve_outcome_row_remediation_hint(
                config=config,
                outcome_code=outcome_code,
                row=row,
                warnings=warnings,
                warning_keys=warning_keys,
            )
            for row in rows
        )
        if hint is not None
    }
    if not hints:
        return "unknown"
    if len(hints) > 1:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=(
                "mixed-outcome-remediation:"
                f"{rows[0].anchor_type}:{outcome_code}:{_freeze_mapping(rows[0].decision_context)}:"
                f"{_freeze_mapping(rows[0].scope_hints)}"
            ),
            message=(
                f"Outcome group '{rows[0].anchor_type}/{outcome_code}' has mixed remediation "
                "hints across stored outcome rows; automated suggestions were skipped"
            ),
        )
        return "unknown"
    return next(iter(hints))


def _resolve_outcome_row_remediation_hint(
    *,
    config,
    outcome_code: str,
    row: OutcomeRecord,
    warnings: list[str],
    warning_keys: set[str],
) -> OutcomeRemediationHint | None:
    """Resolve one outcome row's remediation hint from stored metadata first."""
    if row.outcome_remediation_hint is not None:
        if row.outcome_profile_key is not None:
            profile = config.get_outcome_profile(row.outcome_profile_key)
            if (
                profile is not None
                and row.outcome_profile_version is not None
                and row.outcome_profile_version != profile.version
            ):
                _append_warning_once(
                    warnings=warnings,
                    warning_keys=warning_keys,
                    key=(
                        "outcome-profile-version:"
                        f"{row.outcome_profile_key}:{row.outcome_profile_version}:{profile.version}"
                    ),
                    message=(
                        f"Outcomes for profile '{row.outcome_profile_key}' reference version "
                        f"{row.outcome_profile_version} while current config is version "
                        f"{profile.version}; using stored remediation hints"
                    ),
                )
        return row.outcome_remediation_hint

    if row.outcome_profile_key is None:
        return None

    profile = config.get_outcome_profile(row.outcome_profile_key)
    if profile is None:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"outcome-profile-key:{row.outcome_profile_key}",
            message=f"Outcome profile '{row.outcome_profile_key}' is not defined in config",
        )
        return None

    if (
        row.outcome_profile_version is not None
        and row.outcome_profile_version != profile.version
    ):
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=(
                f"outcome-profile-version-nohint:{row.outcome_profile_key}:"
                f"{row.outcome_profile_version}:{profile.version}"
            ),
            message=(
                f"Outcome profile '{row.outcome_profile_key}' references version "
                f"{row.outcome_profile_version} but does not store a remediation hint; "
                "automated suggestions for those rows were skipped"
            ),
        )
        return None

    code_schema = profile.outcome_codes.get(outcome_code)
    if code_schema is None:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"outcome-code:{row.outcome_profile_key}:{outcome_code}",
            message=(
                f"Outcome code '{outcome_code}' is not defined in the current outcome profile "
                f"'{row.outcome_profile_key}'"
            ),
        )
        return None
    return code_schema.remediation_hint


def _count_outcomes(rows: list[OutcomeRecord]) -> dict[str, int]:
    """Count coarse outcome labels in one grouped row set."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.outcome] = counts.get(row.outcome, 0) + 1
    return counts


def _build_trust_adjustment_suggestion(
    *,
    instance: InstanceProtocol,
    rows: list[OutcomeRecord],
    outcome_code: str,
    warnings: list[str],
    warning_keys: set[str],
) -> TrustAdjustmentSuggestion | None:
    """Build a deterministic trust-demotion suggestion from repeated resolution outcomes."""
    signatures = {
        (
            row.relationship_type,
            _lineage_value(row.lineage_snapshot, "group", "group_signature"),
        )
        for row in rows
    }
    if len(signatures) != 1:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"mixed-signature:{outcome_code}:{len(rows)}",
            message=(
                f"Outcome code '{outcome_code}' spans multiple resolution signatures; "
                "trust-adjustment suggestions were skipped"
            ),
        )
        return None

    relationship_type, group_signature = next(iter(signatures))
    if not relationship_type or not group_signature:
        return None

    incorrect_count = sum(1 for row in rows if row.outcome == "incorrect")
    if incorrect_count == 0:
        return None

    group_store = instance.get_group_store()
    try:
        latest = group_store.find_resolution(
            relationship_type,
            group_signature,
            action="approve",
            confirmed=True,
        )
    finally:
        group_store.close()
    if latest is None:
        return None

    current_trust = latest.trust_status
    if current_trust == "invalidated":
        return None
    suggested = "watch" if current_trust == "trusted" else "invalidated"
    return TrustAdjustmentSuggestion(
        resolution_id=latest.resolution_id,
        relationship_type=relationship_type,
        group_signature=group_signature,
        current_trust_status=current_trust,
        suggested_trust_status=suggested,
        support_count=incorrect_count,
        rationale=(
            f"{incorrect_count} recorded '{outcome_code}' outcomes indicate this trusted "
            "proposal path should be demoted"
        ),
        outcome_ids=[row.outcome_id for row in rows[:5]],
    )


def _build_workflow_review_policy_suggestion(
    *,
    used_names: set[str],
    rows: list[OutcomeRecord],
    outcome_code: str,
) -> OutcomeDecisionPolicySuggestion | None:
    """Build a workflow require-review suggestion from repeated resolution outcomes."""
    first = rows[0]
    workflow_name = str(first.decision_context.get("surface_name") or "")
    relationship_type = first.relationship_type
    if first.decision_context.get("surface_type") != "workflow" or not workflow_name:
        return None
    if not relationship_type:
        return None

    match = {
        "from": {},
        "to": {},
        "edge": {},
        "context": {
            "workflow_name": workflow_name,
            "relationship_type": relationship_type,
            **first.scope_hints,
        },
    }
    name = _dedupe_name(used_names, f"{relationship_type}_{outcome_code}_workflow_review")
    return OutcomeDecisionPolicySuggestion(
        name=name,
        description=(
            f"Suggested from {len(rows)} negative outcomes for outcome_code '{outcome_code}'"
        ),
        relationship_type=relationship_type,
        applies_to="workflow",
        effect="require_review",
        rationale=first.detail.get("reason", "") or f"Repeated outcome '{outcome_code}'",
        match=match,
        workflow_name=workflow_name,
        support_count=len(rows),
        outcome_ids=[row.outcome_id for row in rows[:5]],
    )


def _build_query_policy_suggestion(
    *,
    rows: list[OutcomeRecord],
    outcome_code: str,
) -> QueryPolicySuggestion | None:
    """Build a read-only query policy candidate from receipt-anchored outcomes."""
    first = rows[0]
    if first.decision_context.get("surface_type") != "query":
        return None
    surface_name = str(first.decision_context.get("surface_name") or "")
    if not surface_name:
        return None
    return QueryPolicySuggestion(
        surface_name=surface_name,
        outcome_code=outcome_code,
        support_count=len(rows),
        description=(
            f"Repeated receipt outcomes for query '{surface_name}' and outcome_code "
            f"'{outcome_code}' suggest a query-side policy review"
        ),
        outcome_ids=[row.outcome_id for row in rows[:5]],
    )


def _build_outcome_provider_fix_candidate(
    *,
    rows: list[OutcomeRecord],
    outcome_code: str,
) -> OutcomeProviderFixCandidate | None:
    """Build a provider/workflow fix candidate from receipt outcomes."""
    first = rows[0]
    surface_type = str(first.decision_context.get("surface_type") or "")
    surface_name = str(first.decision_context.get("surface_name") or "")
    if not surface_type or not surface_name:
        return None
    return OutcomeProviderFixCandidate(
        surface_type=surface_type,
        surface_name=surface_name,
        outcome_code=outcome_code,
        support_count=len(rows),
        description=(
            f"Repeated outcome_code '{outcome_code}' on {surface_type} '{surface_name}' "
            "suggests a provider or workflow fix"
        ),
        outcome_ids=[row.outcome_id for row in rows[:5]],
    )


def _build_debug_packages(
    rows: list[OutcomeRecord],
    *,
    min_support: int,
) -> list[DebugPackage]:
    """Build bounded debug packages grouped by anchor identifier."""
    grouped: dict[str, list[OutcomeRecord]] = defaultdict(list)
    for row in rows:
        grouped[row.anchor_id or row.receipt_id].append(row)

    packages: list[DebugPackage] = []
    for anchor_id, anchor_rows in sorted(grouped.items()):
        if len(anchor_rows) < min_support:
            continue
        packages.append(
            DebugPackage(
                anchor_id=anchor_id,
                outcome_count=len(anchor_rows),
                outcome_breakdown=_count_outcomes(anchor_rows),
                outcome_code_breakdown=_count_outcome_codes(anchor_rows),
                sample_outcome_ids=[row.outcome_id for row in anchor_rows[:5]],
                lineage_summary=_summarize_lineage(anchor_rows),
                common_providers=_common_providers(anchor_rows),
                common_trace_patterns=_common_trace_patterns(anchor_rows),
            )
        )
    return packages


def _count_outcome_codes(rows: list[OutcomeRecord]) -> dict[str, int]:
    """Count structured outcome codes in one row set."""
    counts: dict[str, int] = {}
    for row in rows:
        if not row.outcome_code:
            continue
        counts[row.outcome_code] = counts.get(row.outcome_code, 0) + 1
    return counts


def _lineage_value(snapshot: dict[str, Any], section: str, key: str) -> Any:
    """Read one bounded lineage value from a stored snapshot."""
    payload = snapshot.get(section)
    if not isinstance(payload, dict):
        return None
    return payload.get(key)


def _summarize_lineage(rows: list[OutcomeRecord]) -> dict[str, Any]:
    """Aggregate stored lineage fields into one bounded debug summary."""
    first = rows[0]
    summary: dict[str, Any] = {
        "surface_type": first.decision_context.get("surface_type"),
        "surface_name": first.decision_context.get("surface_name"),
    }
    if first.anchor_type == "resolution":
        summary["relationship_type"] = first.relationship_type
        summary["group_signature"] = _lineage_value(
            first.lineage_snapshot,
            "group",
            "group_signature",
        )
    else:
        summary["operation_type"] = _lineage_value(
            first.lineage_snapshot,
            "receipt",
            "operation_type",
        )
    summary["trace_count"] = _lineage_value(first.lineage_snapshot, "trace_set", "trace_count")
    return summary


def _common_providers(rows: list[OutcomeRecord]) -> list[str]:
    """Return providers that recur across stored lineage snapshots."""
    counts: dict[str, int] = {}
    for row in rows:
        trace_set = row.lineage_snapshot.get("trace_set")
        if not isinstance(trace_set, dict):
            continue
        providers = trace_set.get("provider_names")
        if not isinstance(providers, list):
            continue
        for provider in providers:
            if not isinstance(provider, str) or not provider:
                continue
            counts[provider] = counts.get(provider, 0) + 1
    return [
        provider
        for provider, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]


def _common_trace_patterns(rows: list[OutcomeRecord]) -> list[str]:
    """Return repeated provider/step/status patterns from stored trace summaries."""
    counts: dict[str, int] = {}
    for row in rows:
        trace_set = row.lineage_snapshot.get("trace_set")
        if not isinstance(trace_set, dict):
            continue
        summaries = trace_set.get("summaries")
        if not isinstance(summaries, list):
            continue
        seen: set[str] = set()
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            provider = str(summary.get("provider_name") or "")
            step_id = str(summary.get("step_id") or "")
            status = str(summary.get("status") or "")
            pattern = f"{provider}:{step_id}:{status}"
            if pattern in seen or pattern == "::":
                continue
            seen.add(pattern)
            counts[pattern] = counts.get(pattern, 0) + 1
    return [
        pattern
        for pattern, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]


def _resolve_row_remediation_hint(
    *,
    relationship_type: str,
    profile,
    reason_code: str,
    row: FeedbackRecord,
    warnings: list[str],
    warning_keys: set[str],
) -> FeedbackRemediationHint | None:
    """Resolve one row's remediation hint from stored metadata first."""
    if row.reason_remediation_hint is not None:
        if (
            profile is not None
            and row.feedback_profile_version is not None
            and row.feedback_profile_version != profile.version
        ):
            _append_warning_once(
                warnings=warnings,
                warning_keys=warning_keys,
                key=(
                    f"profile-version:{relationship_type}:{row.feedback_profile_version}:"
                    f"{profile.version}"
                ),
                message=(
                    f"Feedback for relationship '{relationship_type}' references profile "
                    f"version {row.feedback_profile_version} while current config is "
                    f"version {profile.version}; using stored remediation hints"
                ),
            )
        return row.reason_remediation_hint

    if profile is None:
        return None
    if row.feedback_profile_key not in (None, relationship_type):
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"profile-key:{row.feedback_id}",
            message=(
                f"Feedback '{row.feedback_id}' references feedback profile "
                f"'{row.feedback_profile_key}', not '{relationship_type}'"
            ),
        )
        return None
    if (
        row.feedback_profile_version is not None
        and row.feedback_profile_version != profile.version
    ):
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=(
                f"profile-version-nohint:{relationship_type}:{row.feedback_profile_version}:"
                f"{profile.version}"
            ),
            message=(
                f"Feedback for relationship '{relationship_type}' references profile "
                f"version {row.feedback_profile_version} but does not store a remediation hint; "
                "automated suggestions for those rows were skipped"
            ),
        )
        return None

    reason_schema = profile.reason_codes.get(reason_code)
    if reason_schema is None:
        _append_warning_once(
            warnings=warnings,
            warning_keys=warning_keys,
            key=f"reason-code:{relationship_type}:{reason_code}",
            message=(
                f"Feedback reason_code '{reason_code}' is not defined in the current "
                f"feedback profile for relationship '{relationship_type}'"
            ),
        )
        return None
    return reason_schema.remediation_hint


def _snapshot_properties(snapshot: dict[str, Any], side: str) -> dict[str, Any]:
    """Return stored snapshot properties for one endpoint side."""
    side_payload = snapshot.get(side)
    if not isinstance(side_payload, dict):
        return {}
    properties = side_payload.get("properties")
    if not isinstance(properties, dict):
        return {}
    return properties


def _append_warning_once(
    *,
    warnings: list[str],
    warning_keys: set[str],
    key: str,
    message: str,
) -> None:
    """Append a warning once per stable key."""
    if key in warning_keys:
        return
    warning_keys.add(key)
    warnings.append(message)


def _dedupe_name(existing_names: set[str], base_name: str) -> str:
    """Produce a deterministic non-colliding config name."""
    candidate = base_name
    suffix = 2
    while candidate in existing_names:
        candidate = f"{base_name}_{suffix}"
        suffix += 1
    return candidate
