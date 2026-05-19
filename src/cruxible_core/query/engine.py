"""Query engine: execute named queries from config against an EntityGraph.

Traversal model:
- Start at an entry entity (resolved from params via primary key)
- Each TraversalStep follows one or more relationships (fan-out),
  applying edge filters and target entity constraints
- Steps chain: output entities of step N become input for step N+1
- max_depth controls how many hops a single step traverses (BFS)
- Final step output is the query result
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    entity_with_identity_properties,
)
from cruxible_core.errors import (
    EntityNotFoundError,
    QueryExecutionError,
    QueryNotFoundError,
    RelationshipNotFoundError,
)
from cruxible_core.graph.types import EntityInstance
from cruxible_core.predicate import (
    COMPARISON_SYMBOL_PATTERN,
    PredicateValueType,
    evaluate_typed_comparison,
    infer_predicate_value_type,
    validate_typed_predicate_operand,
)
from cruxible_core.query.enums import QueryDedupe, QueryRelationshipState, QueryResultShape
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.query.types import (
    QueryPathRow,
    QueryPathSegment,
    QueryRelationshipRow,
    QueryResult,
    QueryRow,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.temporal import is_expired

if TYPE_CHECKING:
    from cruxible_core.config.schema import (
        CoreConfig,
        NamedQuerySchema,
        QueryPredicateSpec,
        RelatedPredicateSpec,
        TraversalStep,
    )
    from cruxible_core.graph.entity_graph import EntityGraph

_MISSING = object()
_RELATED_PREDICATE_SCOPES = (
    "edge",
    "source",
    "target",
    "current",
    "candidate",
    "entry",
)


@dataclass(frozen=True)
class _TraversalState:
    """Internal path-carrying traversal state."""

    entry: EntityInstance
    current: EntityInstance
    entities: tuple[EntityInstance, ...]
    path: tuple[QueryPathSegment, ...]
    parent_id: str | None = None


@dataclass(frozen=True)
class _PredicateContext:
    edge: QueryPathSegment
    source: EntityInstance
    target: EntityInstance
    current: EntityInstance
    candidate: EntityInstance
    entry: EntityInstance


def _matches_filter(entity_props: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
    """Backward-compatible alias for the shared exact-match helper."""
    return matches_exact_filter(entity_props, filter_spec)


def execute_query(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryResult:
    """Execute a named query from the config against the graph.

    Resolves the entry entity from params using the entry_point type's
    primary key, then chains traversal steps. Builds a receipt DAG
    recording every lookup, traversal, filter, and constraint.

    Args:
        config: Config with named query definitions
        graph: Populated graph to query
        query_name: Name of the query in config.named_queries
        params: Query parameters (must include entry entity ID)

    Returns:
        QueryResult with matching entities and a Receipt
    """
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        raise QueryNotFoundError(query_name)
    _validate_query_result_shape(config, query_name, query_schema)
    effective_relationship_state = _effective_relationship_state(
        query_schema,
        relationship_state,
    )
    relationship_state_source = (
        "runtime_override" if relationship_state is not None else "query_config"
    )
    requires_path_retention = _requires_path_retention(
        result_shape=query_schema.result_shape,
        dedupe=query_schema.dedupe,
    )

    execution_options = {
        "relationship_state": effective_relationship_state,
        "relationship_state_source": relationship_state_source,
        "result_shape": query_schema.result_shape,
        "dedupe": query_schema.dedupe,
    }
    builder = ReceiptBuilder(
        query_name=query_name,
        parameters=params,
        execution_options=execution_options,
        root_detail={"filter_summary": _query_filter_summary(query_schema)},
    )

    entry_entity = _resolve_entry_entity(
        config,
        graph,
        query_schema.entry_point,
        params,
        builder=builder,
    )

    current_states = [
        _TraversalState(
            entry=entry_entity,
            current=entry_entity,
            entities=(entry_entity,),
            path=(),
        )
    ]
    steps_executed = 0
    policy_summary: dict[str, int] = {}

    for step in query_schema.traversal:
        current_states = _execute_step(
            config,
            graph,
            step,
            current_states,
            params,
            query_name=query_name,
            requires_path_retention=requires_path_retention,
            relationship_state=effective_relationship_state,
            policy_summary=policy_summary,
            builder=builder,
        )
        steps_executed += 1

    result_states = _dedupe_states(current_states, query_schema.dedupe)
    result_rows = _build_result_rows(config, result_states, query_schema.result_shape)
    result_dicts = [row.model_dump() for row in result_rows]
    parent_ids = [state.parent_id for state in result_states if state.parent_id is not None]
    builder.record_results(result_dicts, parent_ids=parent_ids or None)
    receipt = builder.build(result_dicts)

    return QueryResult(
        query_name=query_name,
        parameters=params,
        results=result_rows,
        result_shape=query_schema.result_shape,
        dedupe=query_schema.dedupe,
        relationship_state=effective_relationship_state,
        steps_executed=steps_executed,
        receipt=receipt,
        policy_summary=policy_summary,
    )


def _resolve_entry_entity(
    config: CoreConfig,
    graph: EntityGraph,
    entry_point: str,
    params: dict[str, Any],
    *,
    builder: ReceiptBuilder | None = None,
) -> EntityInstance:
    """Find the entry entity using the primary key from params."""
    entity_schema = config.get_entity_type(entry_point)
    if entity_schema is None:
        raise QueryExecutionError(f"Entry point entity type '{entry_point}' not in config")

    pk = entity_schema.get_primary_key()
    if pk is None:
        raise QueryExecutionError(f"Entity type '{entry_point}' has no primary key")

    entity_id = params.get(pk)
    if entity_id is None:
        raise QueryExecutionError(
            f"Parameter '{pk}' required for entry point '{entry_point}'. "
            f"Got params: {sorted(params.keys())}"
        )

    entity = graph.get_entity(entry_point, str(entity_id))
    if entity is None:
        raise EntityNotFoundError(entry_point, str(entity_id))

    if builder is not None:
        builder.record_entity_lookup(
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
        )

    return entity


def _validate_query_result_shape(
    config: CoreConfig,
    query_name: str,
    query_schema: Any,
) -> None:
    """Validate result-shape constraints before executing traversal."""
    if query_schema.result_shape == "entity" and query_schema.dedupe != "entity":
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'entity' requires "
            "dedupe 'entity'"
        )
    if query_schema.result_shape != "relationship":
        return
    if query_schema.dedupe == "entity":
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' requires "
            "dedupe 'path' or 'none'"
        )
    if not query_schema.traversal:
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' requires traversal"
        )
    final_step = query_schema.traversal[-1]
    final_relationships: list[str] = []
    for rel_ref in final_step.relationship_types:
        resolved = config.resolve_relationship_reference(rel_ref)
        if resolved is None:
            raise RelationshipNotFoundError(rel_ref)
        rel_schema, _is_reverse = resolved
        final_relationships.append(rel_schema.name)
    final_relationships = list(dict.fromkeys(final_relationships))
    if len(final_relationships) != 1 or query_schema.returns != final_relationships[0]:
        expected = ", ".join(final_relationships) if final_relationships else "<unknown>"
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' must set "
            f"returns to its final relationship type ({expected})"
        )


def _requires_path_retention(
    *,
    result_shape: QueryResultShape,
    dedupe: QueryDedupe,
) -> bool:
    """Return whether traversal must retain full path state.

    Future path-dependent features such as projections, ordering, aggregation,
    or predicates over ``$path`` should feed into this decision.
    """
    return result_shape in {"path", "relationship"} or dedupe in {"path", "none"}


def _effective_relationship_state(
    query_schema: NamedQuerySchema,
    override: QueryRelationshipState | None,
) -> QueryRelationshipState:
    if override is None:
        return query_schema.relationship_state
    if not query_schema.allow_relationship_state_override:
        raise QueryExecutionError(
            "relationship_state override is not allowed for this named query"
        )
    return override


def _query_filter_summary(query_schema: NamedQuerySchema) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for index, step in enumerate(query_schema.traversal):
        summary: dict[str, Any] = {
            "step": index,
            "relationship": step.relationship,
            "direction": step.direction,
        }
        if step.where is not None:
            summary["where"] = step.where.model_dump(mode="python")
        if step.where_related:
            summary["where_related"] = [
                related.model_dump(mode="python", exclude_none=True)
                for related in step.where_related
            ]
        if step.where_not_related:
            summary["where_not_related"] = [
                related.model_dump(mode="python", exclude_none=True)
                for related in step.where_not_related
            ]
        if any(key in summary for key in ("where", "where_related", "where_not_related")):
            summaries.append(summary)
    return summaries


def _execute_step(
    config: CoreConfig,
    graph: EntityGraph,
    step: TraversalStep,
    current_states: list[_TraversalState],
    params: dict[str, Any],
    query_name: str,
    requires_path_retention: bool,
    relationship_state: QueryRelationshipState,
    policy_summary: dict[str, int],
    *,
    builder: ReceiptBuilder | None = None,
) -> list[_TraversalState]:
    """Execute one traversal step via BFS with multi-relationship fan-out.

    Supports multiple relationship types per step and multi-hop traversal
    via max_depth. Three dedup layers:
      1. Entity-frontier pruning: entity rows expand each node at most once
      2. Result dedup: entity rows appear once in default entity-shaped queries
      3. Evidence: all traversal edges recorded in receipt regardless of dedup
    """
    # Validate and resolve all relationship references up front.
    resolved_refs: list[tuple[str, str]] = []
    for rel_ref in step.relationship_types:
        resolved = config.resolve_relationship_reference(rel_ref)
        if resolved is None:
            raise RelationshipNotFoundError(rel_ref)
        rel_schema, is_reverse = resolved
        direction = _flip_relationship_direction(step.direction) if is_reverse else step.direction
        resolved_refs.append((rel_schema.name, direction))
    step_policies = {
        rel_name: _active_query_policies(config, query_name, rel_name)
        for rel_name, _ in resolved_refs
    }

    next_states: list[_TraversalState] = []

    # BFS state
    # Queue entries: (path state, current_depth)
    queue: deque[tuple[_TraversalState, int]] = deque()
    seen_expanded: set[str] = set()  # nodes already expanded (neighbors queried)
    seen_results: set[str] = set()  # nodes already in result list

    # Seed queue with input entities (they are inputs, not results)
    for state in current_states:
        nid = state.current.node_id()
        if not requires_path_retention:
            seen_expanded.add(nid)
            seen_results.add(nid)
        queue.append((state, 0))

    while queue:
        state, depth = queue.popleft()
        entity = state.current

        if depth >= step.max_depth:
            continue

        for rel_type, direction in resolved_refs:
            rel_policies = step_policies.get(rel_type, [])
            for neighbor, segment, relative_direction in _iter_step_relationships(
                graph,
                entity,
                relationship_type=rel_type,
                direction=direction,
                alias=step.alias,
            ):
                if not relationship_matches_query_state(segment.metadata, relationship_state):
                    continue

                nid = neighbor.node_id()
                if requires_path_retention and any(
                    path_entity.node_id() == nid for path_entity in state.entities
                ):
                    continue

                # Record evidence regardless of dedup
                traversal_id = None
                if builder is not None:
                    traversal_id = builder.record_traversal(
                        from_entity_type=entity.entity_type,
                        from_entity_id=entity.entity_id,
                        to_entity_type=neighbor.entity_type,
                        to_entity_id=neighbor.entity_id,
                        relationship=rel_type,
                        edge_props=segment.properties,
                        edge_key=segment.edge_key,
                        parent_id=state.parent_id,
                    )

                # Apply edge filter (blocks subtree on failure)
                if step.filter:
                    passed = matches_exact_filter(segment.properties, step.filter)
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec=step.filter,
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                # Apply target entity filter (blocks subtree on failure)
                if step.target_filter:
                    passed = matches_exact_filter(
                        entity_properties_with_identity(
                            config,
                            neighbor.entity_type,
                            neighbor.entity_id,
                            neighbor.properties,
                        ),
                        step.target_filter,
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec=step.target_filter,
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                predicate_context = _predicate_context(
                    state,
                    current=entity,
                    candidate=neighbor,
                    segment=segment,
                )
                if step.where is not None:
                    passed = evaluate_query_predicates(
                        config,
                        step.where,
                        predicate_context,
                        params,
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec={"where": step.where.model_dump(mode="python")},
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                # Apply constraint (blocks subtree on failure)
                if step.constraint:
                    passed = _evaluate_constraint(
                        config,
                        step.constraint,
                        neighbor,
                        params,
                        value_type=step.constraint_value_type,
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_constraint(
                            constraint=step.constraint,
                            passed=passed,
                            entity_type=neighbor.entity_type,
                            entity_id=neighbor.entity_id,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                if step.exclude_if_related:
                    excluded = False
                    for exclusion in step.exclude_if_related:
                        passed = not _related_edge_exists(
                            graph,
                            current_entity=entity,
                            candidate_entity=neighbor,
                            relationship_type=exclusion.relationship,
                            direction=exclusion.direction,
                            relationship_state=relationship_state,
                        )
                        if builder is not None and traversal_id is not None:
                            builder.record_filter(
                                filter_spec={
                                    "exclude_if_related": {
                                        "relationship": exclusion.relationship,
                                        "direction": exclusion.direction,
                                    }
                                },
                                passed=passed,
                                parent_id=traversal_id,
                            )
                        if not passed:
                            excluded = True
                            break
                    if excluded:
                        continue

                if step.where_related:
                    passed = all(
                        evaluate_related_predicate(
                            graph,
                            related,
                            predicate_context,
                            params,
                            config=config,
                            relationship_state=relationship_state,
                        )
                        for related in step.where_related
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec={
                                "where_related": [
                                    related.model_dump(mode="python")
                                    for related in step.where_related
                                ]
                            },
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                if step.where_not_related:
                    passed = not any(
                        evaluate_related_predicate(
                            graph,
                            related,
                            predicate_context,
                            params,
                            config=config,
                            relationship_state=relationship_state,
                        )
                        for related in step.where_not_related
                    )
                    if builder is not None and traversal_id is not None:
                        builder.record_filter(
                            filter_spec={
                                "where_not_related": [
                                    related.model_dump(mode="python")
                                    for related in step.where_not_related
                                ]
                            },
                            passed=passed,
                            parent_id=traversal_id,
                        )
                    if not passed:
                        continue

                if relative_direction == "outgoing":
                    policy_from_entity = entity
                    policy_to_entity = neighbor
                else:
                    policy_from_entity = neighbor
                    policy_to_entity = entity

                if rel_policies and _policy_should_suppress(
                    config=config,
                    policies=rel_policies,
                    from_entity=policy_from_entity,
                    to_entity=policy_to_entity,
                    edge_props=segment.properties,
                    context={
                        "query_name": query_name,
                        "relationship_type": rel_type,
                        "direction": direction,
                    },
                    policy_summary=policy_summary,
                    builder=builder,
                    parent_id=traversal_id,
                ):
                    continue

                next_state = _TraversalState(
                    entry=state.entry,
                    current=neighbor,
                    entities=(*state.entities, neighbor),
                    path=(*state.path, segment),
                    parent_id=traversal_id,
                )

                # Result dedup: first path owns the lineage
                if not requires_path_retention:
                    if nid not in seen_results:
                        seen_results.add(nid)
                        next_states.append(next_state)
                else:
                    next_states.append(next_state)

                # Expansion dedup: enqueue for deeper hops if not yet expanded
                if not requires_path_retention and nid not in seen_expanded:
                    seen_expanded.add(nid)
                    queue.append((next_state, depth + 1))
                elif requires_path_retention:
                    queue.append((next_state, depth + 1))

    return next_states


def _iter_step_relationships(
    graph: EntityGraph,
    entity: EntityInstance,
    *,
    relationship_type: str,
    direction: str,
    alias: str | None,
) -> list[tuple[EntityInstance, QueryPathSegment, str]]:
    """Return traversable neighbor relationships with concrete edge identity."""
    rows = graph.get_neighbor_relationships(
        entity.entity_type,
        entity.entity_id,
        relationship_type=relationship_type,
        direction=direction,
    )
    relationships: list[tuple[EntityInstance, QueryPathSegment, str]] = []
    for row in rows:
        neighbor = row.get("entity")
        if not isinstance(neighbor, EntityInstance):
            continue
        relative_direction = str(row.get("direction"))
        if relative_direction == "outgoing":
            from_entity = entity
            to_entity = neighbor
        else:
            from_entity = neighbor
            to_entity = entity
        segment = QueryPathSegment(
            alias=alias,
            relationship_type=str(row["relationship_type"]),
            from_type=from_entity.entity_type,
            from_id=from_entity.entity_id,
            to_type=to_entity.entity_type,
            to_id=to_entity.entity_id,
            edge_key=row.get("edge_key"),
            properties=dict(row.get("properties", {})),
            metadata=row.get("metadata", {}),
        )
        relationships.append((neighbor, segment, relative_direction))
    return relationships


def _predicate_context(
    state: _TraversalState,
    *,
    current: EntityInstance,
    candidate: EntityInstance,
    segment: QueryPathSegment,
) -> _PredicateContext:
    source, target = _segment_endpoint_entities(current, candidate, segment)
    return _PredicateContext(
        edge=segment,
        source=source,
        target=target,
        current=current,
        candidate=candidate,
        entry=state.entry,
    )


def _segment_endpoint_entities(
    current: EntityInstance,
    candidate: EntityInstance,
    segment: QueryPathSegment,
) -> tuple[EntityInstance, EntityInstance]:
    if (
        segment.from_type == current.entity_type
        and segment.from_id == current.entity_id
        and segment.to_type == candidate.entity_type
        and segment.to_id == candidate.entity_id
    ):
        return current, candidate
    if (
        segment.from_type == candidate.entity_type
        and segment.from_id == candidate.entity_id
        and segment.to_type == current.entity_type
        and segment.to_id == current.entity_id
    ):
        return candidate, current
    return current, candidate


def evaluate_query_predicates(
    config: CoreConfig,
    predicates: QueryPredicateSpec,
    context: _PredicateContext,
    params: dict[str, Any],
    *,
    base_scope: str | None = None,
) -> bool:
    """Evaluate structured named-query predicates against one traversal context."""
    for path, operators in predicates.root.items():
        scope, field_path = _split_predicate_path(path, base_scope=base_scope)
        left = _resolve_path(_scope_value(context, scope), field_path)
        for operator, raw_expected in operators.items():
            expected = _resolve_predicate_value(raw_expected, context, params)
            if not _evaluate_predicate_operator(
                config,
                context,
                path,
                scope,
                field_path,
                left,
                operator,
                expected,
            ):
                return False
    return True


def evaluate_related_predicate(
    graph: EntityGraph,
    related: RelatedPredicateSpec,
    context: _PredicateContext,
    params: dict[str, Any],
    *,
    config: CoreConfig,
    relationship_state: QueryRelationshipState,
) -> bool:
    """Return whether a related edge exists and matches the related predicates."""
    anchor = context.candidate
    for related_neighbor, related_segment, _relative_direction in _iter_step_relationships(
        graph,
        anchor,
        relationship_type=related.relationship,
        direction=related.direction,
        alias=None,
    ):
        if not relationship_matches_query_state(
            related_segment.metadata,
            relationship_state,
        ):
            continue
        related_context = _build_related_context(
            context,
            anchor=anchor,
            related_neighbor=related_neighbor,
            related_segment=related_segment,
        )
        if _related_predicates_match(config, related, related_context, params):
            return True
    return False


def _build_related_context(
    original_context: _PredicateContext,
    *,
    anchor: EntityInstance,
    related_neighbor: EntityInstance,
    related_segment: QueryPathSegment,
) -> _PredicateContext:
    source, target = _segment_endpoint_entities(anchor, related_neighbor, related_segment)
    return _PredicateContext(
        edge=related_segment,
        source=source,
        target=target,
        current=original_context.current,
        candidate=original_context.candidate,
        entry=original_context.entry,
    )


def _related_predicates_match(
    config: CoreConfig,
    related: RelatedPredicateSpec,
    context: _PredicateContext,
    params: dict[str, Any],
) -> bool:
    for scope, predicates in _iter_related_predicate_scopes(related):
        if not evaluate_query_predicates(
            config,
            predicates,
            context,
            params,
            base_scope=scope,
        ):
            return False
    return True


def _iter_related_predicate_scopes(
    related: RelatedPredicateSpec,
) -> Iterator[tuple[str, QueryPredicateSpec]]:
    for scope in _RELATED_PREDICATE_SCOPES:
        predicates = getattr(related, scope)
        if predicates is not None:
            yield scope, predicates


def _split_predicate_path(path: str, *, base_scope: str | None) -> tuple[str, list[str]]:
    parts = path.split(".")
    if not parts:
        raise QueryExecutionError("predicate path must not be empty")
    first = parts[0]
    if first == "result":
        raise QueryExecutionError("result predicates are not supported at traversal step time")
    if first in {"edge", "source", "target", "current", "candidate", "entry"}:
        return first, parts[1:]
    if base_scope is not None:
        return base_scope, parts
    allowed = "edge, source, target, current, candidate, entry"
    raise QueryExecutionError(
        f"Predicate path '{path}' must start with one of: {allowed}"
    )


def _scope_value(context: _PredicateContext, scope: str) -> Any:
    return {
        "edge": context.edge,
        "source": context.source,
        "target": context.target,
        "current": context.current,
        "candidate": context.candidate,
        "entry": context.entry,
    }[scope]


def _resolve_predicate_value(
    value: Any,
    context: _PredicateContext,
    params: dict[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_predicate_ref(value, context, params)
    if isinstance(value, list):
        return [_resolve_predicate_value(item, context, params) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_predicate_value(item, context, params)
            for key, item in value.items()
        }
    return value


def _resolve_predicate_ref(
    ref: str,
    context: _PredicateContext,
    params: dict[str, Any],
) -> Any:
    prefix, sep, path = ref[1:].partition(".")
    if not sep or not path:
        raise QueryExecutionError(f"Invalid predicate reference '{ref}'")
    if prefix == "input":
        value = _resolve_path(params, path.split("."))
        if value is _MISSING:
            raise QueryExecutionError(f"Missing query input reference '{ref}'")
        return value
    if prefix in {"edge", "source", "target", "current", "candidate", "entry"}:
        value = _resolve_path(_scope_value(context, prefix), path.split("."))
        if value is _MISSING:
            raise QueryExecutionError(f"Missing predicate reference '{ref}'")
        return value
    raise QueryExecutionError(f"Unsupported predicate reference '{ref}'")


def _resolve_path(value: Any, parts: list[str]) -> Any:
    current = value
    for part in parts:
        if current is _MISSING:
            return _MISSING
        if isinstance(current, BaseModel):
            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)
            continue
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        return _MISSING
    return current


def _infer_query_predicate_value_type(
    config: CoreConfig,
    context: _PredicateContext,
    scope: str,
    field_path: list[str],
    left: Any,
    expected: Any,
) -> PredicateValueType | None:
    declared_type = _declared_predicate_property_type(config, context, scope, field_path)
    if declared_type in {"date", "datetime"}:
        return declared_type
    return infer_predicate_value_type(left, expected)


def _declared_predicate_property_type(
    config: CoreConfig,
    context: _PredicateContext,
    scope: str,
    field_path: list[str],
) -> PredicateValueType | None:
    if len(field_path) < 2 or field_path[0] != "properties":
        return None

    property_name = field_path[1]
    if scope == "edge":
        return _relationship_property_type(
            config,
            context.edge.relationship_type,
            property_name,
        )

    value = _scope_value(context, scope)
    if isinstance(value, EntityInstance):
        return _entity_property_type(config, value.entity_type, property_name)
    return None


def _relationship_property_type(
    config: CoreConfig,
    relationship_type: str,
    property_name: str,
) -> PredicateValueType | None:
    for relationship in config.relationships:
        if relationship.name == relationship_type or relationship.reverse_name == relationship_type:
            prop = relationship.properties.get(property_name)
            return _temporal_property_value_type(prop.type) if prop is not None else None
    return None


def _entity_property_type(
    config: CoreConfig,
    entity_type: str,
    property_name: str,
) -> PredicateValueType | None:
    entity_schema = config.entity_types.get(entity_type)
    if entity_schema is None:
        return None
    prop = entity_schema.properties.get(property_name)
    return _temporal_property_value_type(prop.type) if prop is not None else None


def _temporal_property_value_type(property_type: str) -> PredicateValueType | None:
    if property_type == "date":
        return "date"
    if property_type == "datetime":
        return "datetime"
    return None


def _evaluate_predicate_operator(
    config: CoreConfig,
    context: _PredicateContext,
    path: str,
    scope: str,
    field_path: list[str],
    left: Any,
    operator: str,
    expected: Any,
) -> bool:
    if operator == "exists":
        exists = left is not _MISSING
        return exists is expected
    if left is _MISSING:
        return False
    if operator in {"in", "not_in"}:
        if not isinstance(expected, list | tuple | set | frozenset):
            raise QueryExecutionError(
                f"Predicate operator '{operator}' for '{path}' requires a list value"
            )
        matched = any(
            _evaluate_comparison(
                config,
                context,
                path,
                scope,
                field_path,
                left,
                "eq",
                item,
            )
            for item in expected
        )
        return matched if operator == "in" else not matched
    return _evaluate_comparison(
        config,
        context,
        path,
        scope,
        field_path,
        left,
        operator,
        expected,
    )


def _evaluate_comparison(
    config: CoreConfig,
    context: _PredicateContext,
    path: str,
    scope: str,
    field_path: list[str],
    left: Any,
    operator: str,
    expected: Any,
) -> bool:
    value_type = _infer_query_predicate_value_type(
        config,
        context,
        scope,
        field_path,
        left,
        expected,
    )
    if value_type in {"date", "datetime"}:
        _validate_typed_predicate_operand(path, left, value_type)
        _validate_typed_predicate_operand(path, expected, value_type)
    return evaluate_typed_comparison(left, operator, expected, value_type=value_type)


def _validate_typed_predicate_operand(
    path: str,
    value: Any,
    value_type: PredicateValueType,
) -> None:
    try:
        validate_typed_predicate_operand(value, value_type)
    except (TypeError, ValueError) as exc:
        raise QueryExecutionError(
            f"Invalid {value_type} predicate value for '{path}': {value!r}"
        ) from exc


def _dedupe_states(
    states: list[_TraversalState],
    dedupe: QueryDedupe,
) -> list[_TraversalState]:
    """Apply final query result dedupe while preserving deterministic first paths."""
    if dedupe == "none":
        return list(states)

    seen: set[Any] = set()
    deduped: list[_TraversalState] = []
    for state in states:
        key = state.current.node_id() if dedupe == "entity" else _path_identity(state.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(state)
    return deduped


def _path_identity(path: tuple[QueryPathSegment, ...]) -> tuple[tuple[Any, ...], ...]:
    """Return a stable graph identity for one evidence path."""
    return tuple(
        (
            segment.relationship_type,
            segment.from_type,
            segment.from_id,
            segment.to_type,
            segment.to_id,
            segment.edge_key,
        )
        for segment in path
    )


def _build_result_rows(
    config: CoreConfig,
    states: list[_TraversalState],
    result_shape: QueryResultShape,
) -> list[QueryRow]:
    if result_shape == "path":
        return [
            QueryPathRow(
                entry=entity_with_identity_properties(config, state.entry),
                result=entity_with_identity_properties(config, state.current),
                entities=[
                    entity_with_identity_properties(config, entity)
                    for entity in state.entities
                ],
                path=list(state.path),
            )
            for state in states
        ]
    if result_shape == "relationship":
        return [
            _build_relationship_row(config, state)
            for state in states
            if state.path
        ]
    return [
        entity_with_identity_properties(config, state.current)
        for state in states
    ]


def _build_relationship_row(
    config: CoreConfig,
    state: _TraversalState,
) -> QueryRelationshipRow:
    segment = state.path[-1]
    return QueryRelationshipRow(
        relationship_type=segment.relationship_type,
        from_type=segment.from_type,
        from_id=segment.from_id,
        to_type=segment.to_type,
        to_id=segment.to_id,
        edge_key=segment.edge_key,
        properties=dict(segment.properties),
        metadata=segment.metadata,
        entry=entity_with_identity_properties(config, state.entry),
        from_entity=_find_path_entity(
            config,
            state.entities,
            segment.from_type,
            segment.from_id,
        ),
        to_entity=_find_path_entity(
            config,
            state.entities,
            segment.to_type,
            segment.to_id,
        ),
    )


def _find_path_entity(
    config: CoreConfig,
    entities: tuple[EntityInstance, ...],
    entity_type: str,
    entity_id: str,
) -> EntityInstance | None:
    for entity in entities:
        if entity.entity_type == entity_type and entity.entity_id == entity_id:
            return entity_with_identity_properties(config, entity)
    return None


def _related_edge_exists(
    graph: EntityGraph,
    *,
    current_entity: EntityInstance,
    candidate_entity: EntityInstance,
    relationship_type: str,
    direction: str,
    relationship_state: QueryRelationshipState,
) -> bool:
    """Check whether a related edge exists under the effective query state."""
    for neighbor, segment, _relative_direction in _iter_step_relationships(
        graph,
        current_entity,
        relationship_type=relationship_type,
        direction=direction,
        alias=None,
    ):
        if neighbor.node_id() != candidate_entity.node_id():
            continue
        if relationship_matches_query_state(segment.metadata, relationship_state):
            return True
    return False


def _flip_relationship_direction(
    direction: str,
) -> str:
    if direction == "outgoing":
        return "incoming"
    if direction == "incoming":
        return "outgoing"
    return direction


def _active_query_policies(
    config: CoreConfig,
    query_name: str,
    relationship_type: str,
) -> list[Any]:
    """Return non-expired query policies applicable to one traversal relationship."""
    return [
        policy
        for policy in config.decision_policies
        if policy.applies_to == "query"
        and policy.query_name == query_name
        and policy.relationship_type == relationship_type
        and not is_expired(policy.expires_at)
    ]


def _policy_should_suppress(
    *,
    config: CoreConfig,
    policies: list[Any],
    from_entity: EntityInstance,
    to_entity: EntityInstance,
    edge_props: dict[str, Any],
    context: dict[str, Any],
    policy_summary: dict[str, int],
    builder: ReceiptBuilder | None,
    parent_id: str | None,
) -> bool:
    """Apply query-side suppress policies to one traversed edge."""
    for policy in policies:
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
        if not matches_exact_filter(edge_props, policy.match.edge):
            continue
        if not matches_exact_filter(context, policy.match.context):
            continue
        policy_summary[policy.name] = policy_summary.get(policy.name, 0) + 1
        if builder is not None and parent_id is not None:
            builder.record_validation(
                passed=True,
                detail={
                    "policy_name": policy.name,
                    "policy_effect": policy.effect,
                    "applies_to": "query",
                },
                parent_id=parent_id,
            )
        return True
    return False


_CONSTRAINT_RE = re.compile(
    rf"^(target|source)\.([\w-]+)\s*{COMPARISON_SYMBOL_PATTERN}\s*(.+)$"
)


def _evaluate_constraint(
    config: CoreConfig,
    constraint: str,
    target_entity: EntityInstance,
    params: dict[str, Any],
    *,
    value_type: PredicateValueType | None = None,
) -> bool:
    """Evaluate a simple constraint expression.

    Supported format: "target.<property> <op> $<param>" or literal.
    Source-side constraints are rejected because traversal source semantics
    depend on relationship direction and are not implemented.

    Examples:
        "target.vehicle_id == $vehicle_id"
        "target.category != brakes"
        "target.year >= 2024"
    """
    match = _CONSTRAINT_RE.match(constraint.strip())
    if match is None:
        return True  # Unknown constraint format — don't filter

    side, prop, operator, rhs = match.groups()
    rhs = rhs.strip()

    if side == "target":
        lhs_value = entity_properties_with_identity(
            config, target_entity.entity_type, target_entity.entity_id, target_entity.properties
        ).get(prop)
    else:
        raise QueryExecutionError(
            "source-side traversal constraints are not supported; "
            "use target.<property> constraints"
        )

    if rhs.startswith("$"):
        rhs_value = params.get(rhs[1:])
    else:
        rhs_value = _parse_literal(rhs)

    return evaluate_typed_comparison(
        lhs_value,
        operator,
        rhs_value,
        value_type=value_type,
    )


def _parse_literal(value: str) -> Any:
    """Parse a literal value from a constraint string."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    # Strip quotes if present
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
