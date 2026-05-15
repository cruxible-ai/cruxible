"""Graph quality assessment.

Deterministic checks for orphans, coverage gaps, constraint violations,
governed support state, and configured quality rules.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.config.constraint_rules import parse_constraint_rule
from cruxible_core.config.property_validation import entity_properties_with_identity
from cruxible_core.config.schema import CoreConfig
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import load_provenance, provenance_group_id
from cruxible_core.graph.types import REJECTED_STATUSES, make_node_id, split_node_id
from cruxible_core.predicate import evaluate_comparison

if TYPE_CHECKING:
    from cruxible_core.group.types import CandidateMember
    from cruxible_core.instance_protocol import GroupStoreProtocol

FindingCategory = Literal[
    "orphan_entity",
    "coverage_gap",
    "constraint_violation",
    "governed_support_relationship",
    "unreviewed_co_member",
    "quality_check_failed",
]


class EvaluationFinding(BaseModel):
    """A single finding from graph evaluation."""

    category: FindingCategory
    severity: Literal["info", "warning", "error"]
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    """Results of a graph evaluation."""

    entity_count: int
    edge_count: int
    findings: list[EvaluationFinding]
    summary: dict[str, int]  # category -> count
    constraint_summary: dict[str, int] = Field(default_factory=dict)
    quality_summary: dict[str, int] = Field(default_factory=dict)


def evaluate_graph(
    config: CoreConfig,
    graph: EntityGraph,
    *,
    group_store: GroupStoreProtocol | None = None,
    max_findings: int = 100,
    exclude_orphan_types: list[str] | None = None,
) -> EvaluationReport:
    """Evaluate graph quality with deterministic checks.

    Runs six checks:
    1. Orphan entities — nodes with no edges
    2. Coverage gaps — entity/relationship types in config but absent from graph
    3. Constraint violations — rule-based checks on edge properties
    4. Governed support — pending or weakly supported governed relationships
    5. Unreviewed co-members — entities sharing an intermediary with a cross-referenced
       entity but lacking a cross-reference edge themselves
    6. Quality checks — config-defined property, json_content, uniqueness, bounds,
       and cardinality rules
    """
    findings: list[EvaluationFinding] = []
    constraint_summary: dict[str, int] = {
        constraint.name: 0 for constraint in config.constraints
    }
    quality_summary: dict[str, int] = {
        check.name: 0 for check in config.quality_checks
    }

    _check_orphans(graph, findings, exclude_types=exclude_orphan_types)
    _check_coverage_gaps(config, graph, findings)
    _check_constraint_violations(config, graph, findings, constraint_summary)
    _check_governed_support_relationships(config, graph, findings, group_store)
    _check_unreviewed_co_members(config, graph, findings)
    _check_quality_rules(config, graph, findings, quality_summary)

    # Truncate to max_findings
    truncated = findings[:max_findings]

    # Build summary from all findings (before truncation) for accurate counts
    summary: dict[str, int] = {}
    for f in findings:
        summary[f.category] = summary.get(f.category, 0) + 1

    return EvaluationReport(
        entity_count=graph.entity_count(),
        edge_count=graph.edge_count(),
        findings=truncated,
        summary=summary,
        constraint_summary=constraint_summary,
        quality_summary=quality_summary,
    )


def _check_orphans(
    graph: EntityGraph,
    findings: list[EvaluationFinding],
    exclude_types: list[str] | None = None,
) -> None:
    """Find entities with no edges."""
    _exclude = set(exclude_types) if exclude_types else set()
    for entity in graph.iter_all_entities():
        if entity.entity_type in _exclude:
            continue
        if graph.is_isolated(entity.entity_type, entity.entity_id):
            findings.append(
                EvaluationFinding(
                    category="orphan_entity",
                    severity="warning",
                    message=f"Orphan entity: {entity.entity_type}:{entity.entity_id}",
                    detail={
                        "entity_type": entity.entity_type,
                        "entity_id": entity.entity_id,
                    },
                )
            )


def _check_coverage_gaps(
    config: CoreConfig, graph: EntityGraph, findings: list[EvaluationFinding]
) -> None:
    """Find entity/relationship types in config but not in graph."""
    graph_entity_types = set(graph.list_entity_types())
    for entity_type in config.entity_types:
        if entity_type not in graph_entity_types:
            findings.append(
                EvaluationFinding(
                    category="coverage_gap",
                    severity="info",
                    message=f"Entity type '{entity_type}' defined in config but absent from graph",
                    detail={"type": "entity_type", "name": entity_type},
                )
            )

    graph_rel_types = set(graph.list_relationship_types())
    for rel in config.relationships:
        if rel.name not in graph_rel_types:
            findings.append(
                EvaluationFinding(
                    category="coverage_gap",
                    severity="info",
                    message=f"Relationship '{rel.name}' defined in config but absent from graph",
                    detail={"type": "relationship_type", "name": rel.name},
                )
            )


def _check_constraint_violations(
    config: CoreConfig,
    graph: EntityGraph,
    findings: list[EvaluationFinding],
    constraint_summary: dict[str, int],
) -> None:
    """Check constraint rules against graph edges."""
    for constraint in config.constraints:
        parsed = parse_constraint_rule(constraint.rule)
        if not parsed:
            # Skip unparseable rules (matches validator.py pattern)
            continue

        rel_name = parsed.relationship
        from_prop = parsed.from_property
        to_prop = parsed.to_property

        for from_type, from_id, to_type, to_id, _props in graph.iter_edge_data(rel_name):
            from_entity = graph.get_entity(from_type, from_id)
            to_entity = graph.get_entity(to_type, to_id)

            from_props = (
                entity_properties_with_identity(
                    config, from_type, from_id, from_entity.properties
                )
                if from_entity
                else {}
            )
            to_props = (
                entity_properties_with_identity(config, to_type, to_id, to_entity.properties)
                if to_entity
                else {}
            )

            from_val = from_props.get(from_prop)
            to_val = to_props.get(to_prop)

            if (
                from_val is not None
                and to_val is not None
                and not evaluate_comparison(from_val, parsed.operator, to_val)
            ):
                constraint_summary[constraint.name] = (
                    constraint_summary.get(constraint.name, 0) + 1
                )
                findings.append(
                    EvaluationFinding(
                        category="constraint_violation",
                        severity=constraint.severity,
                        message=(
                            f"Constraint '{constraint.name}' violated: "
                            f"expected {from_type}:{from_id}.{from_prop} ({from_val!r}) "
                            f"{parsed.operator} {to_type}:{to_id}.{to_prop} ({to_val!r})"
                        ),
                        detail={
                            "constraint": constraint.name,
                            "rule": constraint.rule,
                            "operator": parsed.operator,
                            "from_entity": f"{from_type}:{from_id}",
                            "to_entity": f"{to_type}:{to_id}",
                            "from_value": from_val,
                            "to_value": to_val,
                        },
                    )
                )


def _check_governed_support_relationships(
    config: CoreConfig,
    graph: EntityGraph,
    findings: list[EvaluationFinding],
    group_store: GroupStoreProtocol | None,
) -> None:
    """Find governed relationships whose tri-state support needs review."""
    governed = {
        relationship.name: relationship
        for relationship in config.relationships
        if relationship.proposal_policy is not None
    }
    if not governed:
        return

    for edge in graph.iter_edges():
        relationship_type = edge["relationship_type"]
        relationship = governed.get(relationship_type)
        if relationship is None or relationship.proposal_policy is None:
            continue

        from_type = edge["from_type"]
        from_id = edge["from_id"]
        to_type = edge["to_type"]
        to_id = edge["to_id"]
        props = edge["properties"]
        review_status = props.get("review_status")

        if review_status == "pending_review":
            findings.append(
                EvaluationFinding(
                    category="governed_support_relationship",
                    severity="warning",
                    message=(
                        f"Pending review: {from_type}:{from_id} "
                        f"—[{relationship_type}]→ "
                        f"{to_type}:{to_id}"
                    ),
                    detail={
                        "from_entity": f"{from_type}:{from_id}",
                        "to_entity": f"{to_type}:{to_id}",
                        "relationship_type": relationship_type,
                        "review_status": "pending_review",
                        "reason": "pending_review",
                    },
                )
            )
            continue

        if group_store is None:
            continue

        member = resolve_edge_signal_history(
            group_store,
            from_type=from_type,
            from_id=from_id,
            relationship_type=relationship_type,
            to_type=to_type,
            to_id=to_id,
            properties=props,
        )
        if member is None:
            findings.append(
                _governed_support_finding(
                    from_type,
                    from_id,
                    relationship_type,
                    to_type,
                    to_id,
                    "missing_group_trail",
                    "Governed relationship has no resolvable group signal trail",
                )
            )
            continue

        signals_by_source = {signal.signal_source: signal.signal for signal in member.signals}
        required_signal_sources = {
            name
            for name, guardrail in relationship.proposal_policy.signals.items()
            if guardrail.role in {"blocking", "required"}
        }
        missing = sorted(required_signal_sources - set(signals_by_source))
        if missing:
            findings.append(
                _governed_support_finding(
                    from_type,
                    from_id,
                    relationship_type,
                    to_type,
                    to_id,
                    "missing_required_signal",
                    "Governed relationship is missing required signal-source support",
                    signal_sources=missing,
                )
            )

        for source_name, guardrail in relationship.proposal_policy.signals.items():
            signal = signals_by_source.get(source_name)
            if guardrail.role == "blocking" and signal == "contradict":
                findings.append(
                    _governed_support_finding(
                        from_type,
                        from_id,
                        relationship_type,
                        to_type,
                        to_id,
                        "blocking_contradict",
                        "Governed relationship has a blocking contradict signal",
                        signal_sources=[source_name],
                    )
                )
            elif guardrail.role in {"blocking", "required"} and signal == "unsure":
                findings.append(
                    _governed_support_finding(
                        from_type,
                        from_id,
                        relationship_type,
                        to_type,
                        to_id,
                        "required_unsure",
                        "Governed relationship has required or blocking unsure support",
                        signal_sources=[source_name],
                    )
                )


def resolve_edge_signal_history(
    group_store: GroupStoreProtocol,
    *,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any],
) -> CandidateMember | None:
    """Resolve a graph edge back to its approved group candidate member."""
    provenance = load_provenance(properties.get("_provenance"))
    if provenance is None:
        return None
    group_id = provenance_group_id(provenance)
    if group_id is None:
        return None
    group = group_store.get_group(group_id)
    if group is None:
        return None
    for member in group_store.get_members(group_id):
        if (
            member.from_type == from_type
            and member.from_id == from_id
            and member.relationship_type == relationship_type
            and member.to_type == to_type
            and member.to_id == to_id
        ):
            return member
    return None


def _governed_support_finding(
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    reason: str,
    message: str,
    *,
    signal_sources: list[str] | None = None,
) -> EvaluationFinding:
    detail: dict[str, Any] = {
        "from_entity": f"{from_type}:{from_id}",
        "to_entity": f"{to_type}:{to_id}",
        "relationship_type": relationship_type,
        "reason": reason,
    }
    if signal_sources:
        detail["signal_sources"] = signal_sources
    return EvaluationFinding(
        category="governed_support_relationship",
        severity="warning",
        message=(
            f"{message}: {from_type}:{from_id} —[{relationship_type}]→ {to_type}:{to_id}"
        ),
        detail=detail,
    )


_MAX_MATCHED_FOR_CO_MEMBERS = 1000
_MAX_INTERMEDIARY_DEGREE = 200


def _check_unreviewed_co_members(
    config: CoreConfig,
    graph: EntityGraph,
    findings: list[EvaluationFinding],
) -> None:
    """Find entities sharing an intermediary with a cross-referenced
    entity but lacking a cross-reference edge.

    For each relationship R, find co-membership relationships S where
    R.to_entity == S.from_entity. Entities reachable from matched
    targets through shared intermediaries that lack their own R edge
    are flagged as unreviewed co-members.

    Precision limitation: this check is purely structural. It does not
    inspect entity properties (e.g., a `category` discriminator) or the
    direction of an existing R edge on the candidate co-member. In a
    domain where R-targets share intermediaries with semantically
    unrelated entities -- for example a brake pad and a door panel that
    both fit the same vehicle -- the door panel will still be surfaced
    as a co-member of the brake pad's replacement chain. Co-members that
    are themselves R-sources (the active replacements) are also
    surfaced, since matched_set only contains R-targets.

    Treat findings as low-precision audit prompts for a human reviewer,
    not as actionable suggestions. For higher-precision "needs review"
    candidates, use a proposal workflow with a provider that applies
    domain logic.
    """
    for r_rel in config.relationships:
        # Find co-membership relationships S where R.to_entity == S.from_entity
        s_rels = [
            s
            for s in config.relationships
            if s.from_entity == r_rel.to_entity and s.name != r_rel.name
        ]
        if not s_rels:
            continue

        # Build matched_set: non-rejected R targets
        matched_set: set[str] = set()
        for _, _, to_type, to_id, props in graph.iter_edge_data(r_rel.name):
            if to_type != r_rel.to_entity:
                continue
            if props.get("review_status") in REJECTED_STATUSES:
                continue
            matched_set.add(make_node_id(to_type, to_id))

        if not matched_set or len(matched_set) > _MAX_MATCHED_FOR_CO_MEMBERS:
            continue

        for s_rel in s_rels:
            seen: set[tuple[str, str, str, str]] = set()
            intermediary_cache: dict[str, list[tuple[Any, dict[str, Any], int]] | None] = {}

            for matched_node_id in matched_set:
                matched_type, matched_id = split_node_id(matched_node_id)

                # Follow S outgoing from matched entity to intermediaries
                outgoing = graph.get_neighbors_with_edge_refs(
                    matched_type, matched_id, s_rel.name, "outgoing"
                )

                for intermediary, out_edge_props, _ in outgoing:
                    # Skip rejected outgoing S edges
                    if out_edge_props.get("review_status") in REJECTED_STATUSES:
                        continue

                    intermediary_node_id = make_node_id(
                        intermediary.entity_type, intermediary.entity_id
                    )

                    # Check/populate cache for this intermediary
                    if intermediary_node_id not in intermediary_cache:
                        degree = graph.count_edges(
                            intermediary.entity_type,
                            intermediary.entity_id,
                            s_rel.name,
                            "incoming",
                        )
                        if degree > _MAX_INTERMEDIARY_DEGREE:
                            intermediary_cache[intermediary_node_id] = None
                        else:
                            intermediary_cache[intermediary_node_id] = (
                                graph.get_neighbors_with_edge_refs(
                                    intermediary.entity_type,
                                    intermediary.entity_id,
                                    s_rel.name,
                                    "incoming",
                                )
                            )

                    cached = intermediary_cache[intermediary_node_id]
                    if cached is None:
                        continue

                    for co_member, in_edge_props, _ in cached:
                        # Skip rejected incoming S edges
                        if in_edge_props.get("review_status") in REJECTED_STATUSES:
                            continue

                        # Defensive: skip malformed edges
                        if co_member.entity_type != r_rel.to_entity:
                            continue

                        co_member_node_id = make_node_id(co_member.entity_type, co_member.entity_id)

                        # Skip self
                        if co_member_node_id == matched_node_id:
                            continue

                        # Skip if already matched
                        if co_member_node_id in matched_set:
                            continue

                        # Dedup
                        dedup_key = (
                            co_member.entity_type,
                            co_member.entity_id,
                            r_rel.name,
                            s_rel.name,
                        )
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)

                        findings.append(
                            EvaluationFinding(
                                category="unreviewed_co_member",
                                severity="info",
                                message=(
                                    f"Unreviewed co-member: "
                                    f"{r_rel.to_entity}:{co_member.entity_id}"
                                    f" shares {intermediary.entity_type}"
                                    f":{intermediary.entity_id}"
                                    f" (via '{s_rel.name}') with "
                                    f"{r_rel.to_entity}:{matched_id}"
                                    f" (cross-referenced via"
                                    f" '{r_rel.name}') but has no"
                                    f" '{r_rel.name}' edge"
                                ),
                                detail={
                                    "entity_type": co_member.entity_type,
                                    "entity_id": co_member.entity_id,
                                    "matched_sibling": (f"{r_rel.to_entity}:{matched_id}"),
                                    "shared_via": s_rel.name,
                                    "shared_entity": (
                                        f"{intermediary.entity_type}:{intermediary.entity_id}"
                                    ),
                                    "missing_relationship": r_rel.name,
                                },
                            )
                        )


def _check_quality_rules(
    config: CoreConfig,
    graph: EntityGraph,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    """Run config-defined quality checks against the current graph state."""
    for check in config.quality_checks:
        kind = getattr(check, "kind", "")
        if kind == "property":
            _run_property_quality_check(graph, check, findings, quality_summary)
        elif kind == "json_content":
            _run_json_content_quality_check(graph, check, findings, quality_summary)
        elif kind == "uniqueness":
            _run_uniqueness_quality_check(graph, check, findings, quality_summary)
        elif kind == "bounds":
            _run_bounds_quality_check(graph, check, findings, quality_summary)
        elif kind == "cardinality":
            _run_cardinality_quality_check(graph, check, findings, quality_summary)


def _run_property_quality_check(
    graph: EntityGraph,
    check: Any,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    for target in _iter_quality_targets(graph, check):
        value = target["properties"].get(check.property)
        has_property = check.property in target["properties"]
        failed = False
        reason = ""
        if check.rule == "required":
            failed = not has_property or value is None
            reason = "missing_required_property"
        elif check.rule == "non_empty":
            failed = not has_property or _is_empty_value(value)
            reason = "empty_property"
        elif check.rule == "type":
            failed = not has_property or not _matches_expected_type(value, check.expected_type)
            reason = "type_mismatch"
        else:
            assert check.pattern is not None
            failed = (
                not has_property
                or not isinstance(value, str)
                or re.search(check.pattern, value) is None
            )
            reason = "pattern_mismatch"

        if failed:
            _append_quality_finding(
                findings,
                quality_summary,
                check,
                message=(
                    f"Quality check '{check.name}' failed for "
                    f"{target['label']}.{check.property}"
                ),
                detail={
                    "reason": reason,
                    **target["detail"],
                    "property": check.property,
                    "value": value,
                    "expected_type": getattr(check, "expected_type", None),
                    "pattern": getattr(check, "pattern", None),
                },
            )


def _run_json_content_quality_check(
    graph: EntityGraph,
    check: Any,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    for target in _iter_quality_targets(graph, check):
        value = target["properties"].get(check.property)
        if value is None:
            continue  # absent optional property — not a content violation
        if not isinstance(value, list):
            _append_quality_finding(
                findings,
                quality_summary,
                check,
                message=(
                    f"Quality check '{check.name}' failed for "
                    f"{target['label']}.{check.property}"
                ),
                detail={
                    "reason": "not_array",
                    **target["detail"],
                    "property": check.property,
                    "value": value,
                },
            )
            continue

        for index, item in enumerate(value):
            if not isinstance(item, dict):
                _append_quality_finding(
                    findings,
                    quality_summary,
                    check,
                    message=(
                        f"Quality check '{check.name}' failed for "
                        f"{target['label']}.{check.property}[{index}]"
                    ),
                    detail={
                        "reason": "item_not_object",
                        **target["detail"],
                        "property": check.property,
                        "index": index,
                        "item": item,
                    },
                )
                continue

            if check.rule == "no_empty_objects_in_array":
                if not item:
                    _append_quality_finding(
                        findings,
                        quality_summary,
                        check,
                        message=(
                            f"Quality check '{check.name}' failed for "
                            f"{target['label']}.{check.property}[{index}]"
                        ),
                        detail={
                            "reason": "empty_object",
                            **target["detail"],
                            "property": check.property,
                            "index": index,
                            "item": item,
                        },
                    )
                continue

            populated_keys = [key for key in check.keys if not _is_empty_value(item.get(key))]
            is_valid = bool(populated_keys) if check.match == "any" else len(populated_keys) == len(
                check.keys
            )
            if not is_valid:
                _append_quality_finding(
                    findings,
                    quality_summary,
                    check,
                    message=(
                        f"Quality check '{check.name}' failed for "
                        f"{target['label']}.{check.property}[{index}]"
                    ),
                    detail={
                        "reason": "missing_nested_keys",
                        **target["detail"],
                        "property": check.property,
                        "index": index,
                        "item": item,
                        "required_keys": check.keys,
                        "match": check.match,
                    },
                )


def _run_uniqueness_quality_check(
    graph: EntityGraph,
    check: Any,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    grouped: dict[tuple[Any, ...], list[str]] = {}
    for entity in graph.list_entities(check.entity_type):
        values: list[Any] = []
        skip = False
        for prop_name in check.properties:
            value = entity.properties.get(prop_name)
            if value is None:
                skip = True
                break
            values.append(value)
        if skip:
            continue
        grouped.setdefault(tuple(values), []).append(entity.entity_id)

    for grouped_values, entity_ids in grouped.items():
        if len(entity_ids) < 2:
            continue
        sorted_ids = sorted(entity_ids)
        _append_quality_finding(
            findings,
            quality_summary,
            check,
            message=(
                f"Quality check '{check.name}' failed: {len(sorted_ids)} "
                f"{check.entity_type} entities share {check.properties}"
            ),
            detail={
                "entity_type": check.entity_type,
                "properties": check.properties,
                "values": list(grouped_values),
                "entity_ids": sorted_ids,
            },
        )


def _run_bounds_quality_check(
    graph: EntityGraph,
    check: Any,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    if check.target == "entity_count":
        count = graph.entity_count(check.entity_type)
        target_detail = {"target": check.target, "entity_type": check.entity_type}
        label = f"entity type '{check.entity_type}'"
    else:
        count = graph.edge_count(check.relationship_type)
        target_detail = {
            "target": check.target,
            "relationship_type": check.relationship_type,
        }
        label = f"relationship '{check.relationship_type}'"

    if _violates_bounds(count, check.min_count, check.max_count):
        _append_quality_finding(
            findings,
            quality_summary,
            check,
            message=f"Quality check '{check.name}' failed for {label} count",
            detail={
                **target_detail,
                "count": count,
                "min_count": check.min_count,
                "max_count": check.max_count,
            },
        )


def _run_cardinality_quality_check(
    graph: EntityGraph,
    check: Any,
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
) -> None:
    for entity in graph.list_entities(check.entity_type):
        count = graph.count_edges(
            entity.entity_type,
            entity.entity_id,
            relationship_type=check.relationship_type,
            direction=check.direction,
        )
        if _violates_bounds(count, check.min_count, check.max_count):
            _append_quality_finding(
                findings,
                quality_summary,
                check,
                message=(
                    f"Quality check '{check.name}' failed for "
                    f"{entity.entity_type}:{entity.entity_id}"
                ),
                detail={
                    "entity_type": entity.entity_type,
                    "entity_id": entity.entity_id,
                    "relationship_type": check.relationship_type,
                    "direction": check.direction,
                    "count": count,
                    "min_count": check.min_count,
                    "max_count": check.max_count,
                },
            )


def _iter_quality_targets(graph: EntityGraph, check: Any) -> list[dict[str, Any]]:
    if check.target == "entity":
        return [
            {
                "label": f"{entity.entity_type}:{entity.entity_id}",
                "properties": entity.properties,
                "detail": {
                    "entity_type": entity.entity_type,
                    "entity_id": entity.entity_id,
                },
            }
            for entity in graph.list_entities(check.entity_type)
        ]

    return [
        {
            "label": f"{from_type}:{from_id}->{to_type}:{to_id}",
            "properties": properties,
            "detail": {
                "relationship_type": check.relationship_type,
                "from_entity": f"{from_type}:{from_id}",
                "to_entity": f"{to_type}:{to_id}",
            },
        }
        for from_type, from_id, to_type, to_id, properties in graph.iter_edge_data(
            check.relationship_type
        )
    ]


def _append_quality_finding(
    findings: list[EvaluationFinding],
    quality_summary: dict[str, int],
    check: Any,
    *,
    message: str,
    detail: dict[str, Any],
) -> None:
    findings.append(
        EvaluationFinding(
            category="quality_check_failed",
            severity=check.severity,
            message=message,
            detail={
                "check_name": check.name,
                "check_kind": check.kind,
                **detail,
            },
        )
    )
    quality_summary[check.name] = quality_summary.get(check.name, 0) + 1


def _violates_bounds(count: int, min_count: int | None, max_count: int | None) -> bool:
    if min_count is not None and count < min_count:
        return True
    if max_count is not None and count > max_count:
        return True
    return False


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _matches_expected_type(value: Any, expected_type: str | None) -> bool:
    if expected_type is None:
        return False
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "float":
        return isinstance(value, float)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "bool":
        return isinstance(value, bool)
    if expected_type == "date":
        return isinstance(value, str)
    if expected_type == "json":
        return isinstance(value, (dict, list))
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    return False
