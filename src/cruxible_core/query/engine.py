"""Query engine: execute named queries from config against an EntityGraph.

Traversal model:
- Start at an entry entity (resolved from params via primary key)
- Each TraversalStep follows one or more relationships (fan-out),
  applying edge filters and target entity constraints
- Steps chain: output entities of step N become input for step N+1
- max_depth controls how many hops a single step traverses (BFS)
- Final step output is the query result. Entity-shaped queries can collect
  only declared return-type entities while traversing through intermediate
  entity types on the final BFS step.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, replace
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any

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
from cruxible_core.graph.types import EntityInstance, RelationshipMetadata
from cruxible_core.predicate import (
    COMPARISON_SYMBOL_PATTERN,
    PredicateValueType,
    evaluate_typed_comparison,
)
from cruxible_core.query.enums import QueryDedupe, QueryRelationshipState, QueryResultShape
from cruxible_core.query.filters import matches_exact_filter
from cruxible_core.query.predicates import (
    build_predicate_context,
    evaluate_query_predicates,
    evaluate_related_predicate,
    is_missing_path,
    iter_edge_where_property_fields,
    iter_step_relationships,
    query_filter_summary,
    related_edge_exists,
    resolve_path,
    segment_endpoint_entities,
    validate_edge_where_property_fields,
)
from cruxible_core.query.projection import (
    QueryRowContext,
    coerce_query_order_value,
    compare_order_values,
    compare_sort_keys,
    project_query_row,
    sort_query_row_contexts,
    stable_row_identity,
)
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.query.types import (
    BaseQueryRow,
    QueryIncludeItem,
    QueryIncludeResult,
    QueryPathRow,
    QueryPathSegment,
    QueryRelationshipRow,
    QueryResult,
    QueryRow,
    dump_query_row,
)
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.temporal import is_expired

if TYPE_CHECKING:
    from cruxible_core.config.schema import (
        CoreConfig,
        NamedQuerySchema,
        QueryIncludeSpec,
        QueryOrderSpec,
        TraversalStep,
    )
    from cruxible_core.graph.entity_graph import EntityGraph


_NO_COLLECTION_PUSHDOWN = object()


@dataclass(frozen=True)
class _TraversalState:
    """Internal path-carrying traversal state."""

    entry: EntityInstance
    current: EntityInstance
    entities: tuple[EntityInstance, ...]
    path: tuple[QueryPathSegment, ...]
    parent_id: str | None = None


@dataclass(frozen=True)
class _EffectiveQueryOptions:
    relationship_state: QueryRelationshipState
    relationship_state_source: str
    result_shape: QueryResultShape
    dedupe: QueryDedupe

    def receipt_options(self) -> dict[str, str]:
        return {
            "relationship_state": self.relationship_state,
            "relationship_state_source": self.relationship_state_source,
            "result_shape": self.result_shape,
            "dedupe": self.dedupe,
        }


@dataclass(frozen=True)
class _PathBudgetResult:
    contexts: list[QueryRowContext]
    total_path_count: int | None
    retained_path_count: int | None
    path_truncated: bool
    truncation_reasons: list[str]


@dataclass
class _TraversalBudgetState:
    """Mutable per-query traversal budget bookkeeping."""

    max_paths: int | None
    truncated: bool = False
    truncation_recorded: bool = False
    evaluated_path_candidate_count: int = 0


def _matches_filter(entity_props: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
    """Backward-compatible alias for the shared exact-match helper."""
    return matches_exact_filter(entity_props, filter_spec)


def _query_include_summary(query_schema: NamedQuerySchema) -> list[dict[str, Any]]:
    """Return config-level include summaries for receipt root metadata."""
    return [
        {"alias": alias, **include.model_dump(mode="python", by_alias=True, exclude_none=True)}
        for alias, include in query_schema.include.items()
    ]


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
    recording every lookup, traversal, filter, and constraint. Result rows
    are shaped by the named query's ``result_shape`` setting.

    Args:
        config: Config with named query definitions
        graph: Populated graph to query
        query_name: Name of the query in config.named_queries
        params: Query parameters (must include entry entity ID)
        relationship_state: Optional relationship visibility override. The
            named query must allow runtime overrides when this is provided.

    Returns:
        QueryResult with shaped rows, execution metadata, and a Receipt
    """
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        raise QueryNotFoundError(query_name)
    return execute_query_definition(
        config,
        graph,
        query_name,
        query_schema,
        params,
        relationship_state=relationship_state,
    )


def execute_query_definition(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    query_schema: NamedQuerySchema,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryResult:
    """Execute a named or inline query definition against the graph."""
    effective_options = _resolve_effective_query_options(
        config,
        query_name,
        query_schema,
        relationship_state,
    )
    declared_entity_return_type = _resolve_declared_entity_return_type(
        config,
        query_name,
        query_schema,
        effective_options.result_shape,
    )
    requires_path_retention = _requires_path_retention(
        result_shape=effective_options.result_shape,
        dedupe=effective_options.dedupe,
    )
    optional_path_aliases = _optional_path_aliases(query_schema)

    builder = ReceiptBuilder(
        query_name=query_name,
        parameters=params,
        execution_options=effective_options.receipt_options(),
        root_detail={
            "filter_summary": query_filter_summary(query_schema),
            "select": query_schema.select,
            "order_by": [
                order.model_dump(mode="python", exclude_none=True)
                for order in query_schema.order_by
            ],
            "limit": query_schema.limit,
            "max_paths": query_schema.max_paths,
            "max_paths_per_result": query_schema.max_paths_per_result,
            "include": _query_include_summary(query_schema),
        },
    )

    if query_schema.mode == "collection":
        return _execute_collection_query(
            config,
            graph,
            query_name,
            query_schema,
            params,
            effective_options=effective_options,
            builder=builder,
        )

    if query_schema.entry_point is None:
        raise QueryExecutionError(f"Traversal query '{query_name}' requires entry_point")
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
    traversal_budget = _TraversalBudgetState(
        max_paths=query_schema.max_paths if requires_path_retention else None
    )

    for step_index, step in enumerate(query_schema.traversal):
        collect_entity_type = _step_collect_entity_type(
            config,
            query_schema,
            effective_options.result_shape,
            declared_entity_return_type,
            step,
            step_index=step_index,
        )
        current_states = _execute_step(
            config,
            graph,
            step,
            current_states,
            params,
            query_name=query_name,
            requires_path_retention=requires_path_retention,
            relationship_state=effective_options.relationship_state,
            traversal_budget=traversal_budget,
            step_index=step_index,
            policy_summary=policy_summary,
            optional_path_aliases=optional_path_aliases,
            collect_entity_type=collect_entity_type,
            builder=builder,
        )
        steps_executed += 1

    result_states = _dedupe_states(current_states, effective_options.dedupe)
    result_contexts = _build_result_contexts(
        config,
        result_states,
        effective_options.result_shape,
        optional_path_aliases=optional_path_aliases,
    )
    if declared_entity_return_type is not None:
        _validate_result_context_return_types(
            query_name,
            result_contexts,
            expected_entity_type=declared_entity_return_type,
        )
    result_contexts = _apply_includes(
        config,
        graph,
        query_schema,
        result_contexts,
        params,
        relationship_state=effective_options.relationship_state,
        builder=builder,
    )
    budget_result = _apply_path_budgets(
        result_contexts,
        max_paths_per_result=query_schema.max_paths_per_result,
        traversal_max_paths=query_schema.max_paths,
        traversal_truncated=traversal_budget.truncated,
    )
    result_contexts = budget_result.contexts
    result_contexts = sort_query_row_contexts(
        result_contexts,
        query_schema.order_by,
        params,
        config=config,
    )
    total_results = len(result_contexts)
    limited_contexts = (
        result_contexts[: query_schema.limit] if query_schema.limit is not None else result_contexts
    )
    limit_truncated = query_schema.limit is not None and total_results > query_schema.limit
    truncation_reasons = list(budget_result.truncation_reasons)
    if limit_truncated:
        truncation_reasons.append("limit")
    truncated = bool(truncation_reasons)
    result_rows: list[QueryRow] = [
        (
            project_query_row(query_schema.select, context, params)
            if query_schema.select is not None
            else context.row
        )
        for context in limited_contexts
    ]
    result_dicts = [dump_query_row(row, include_source=True) for row in result_rows]
    parent_ids = [
        context.parent_id for context in limited_contexts if context.parent_id is not None
    ]
    builder.record_results(
        result_dicts,
        parent_ids=parent_ids or None,
        detail={
            "total_results": total_results,
            "limit": query_schema.limit,
            "truncated": truncated,
            "limit_truncated": limit_truncated,
            "path_truncated": budget_result.path_truncated,
            "truncation_reasons": truncation_reasons,
            "max_paths": query_schema.max_paths,
            "max_paths_per_result": query_schema.max_paths_per_result,
            "total_path_count": budget_result.total_path_count,
            "retained_path_count": budget_result.retained_path_count,
            "evaluated_path_candidate_count": (
                traversal_budget.evaluated_path_candidate_count
                if query_schema.max_paths is not None
                else None
            ),
        },
    )
    receipt = builder.build(result_dicts)

    return QueryResult(
        query_name=query_name,
        parameters=params,
        results=result_rows,
        result_shape=effective_options.result_shape,
        dedupe=effective_options.dedupe,
        relationship_state=effective_options.relationship_state,
        steps_executed=steps_executed,
        total_results=total_results,
        limit=query_schema.limit,
        truncated=truncated,
        limit_truncated=limit_truncated,
        path_truncated=budget_result.path_truncated,
        truncation_reasons=truncation_reasons,
        max_paths=query_schema.max_paths,
        max_paths_per_result=query_schema.max_paths_per_result,
        total_path_count=budget_result.total_path_count,
        retained_path_count=budget_result.retained_path_count,
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


def _execute_collection_query(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    query_schema: NamedQuerySchema,
    params: dict[str, Any],
    *,
    effective_options: _EffectiveQueryOptions,
    builder: ReceiptBuilder,
) -> QueryResult:
    """Execute an entity or relationship collection query."""
    if query_schema.result_shape == "path":
        raise QueryExecutionError(
            f"Collection query '{query_name}' does not support result_shape 'path'"
        )
    if query_schema.traversal:
        raise QueryExecutionError(f"Collection query '{query_name}' must not define traversal")
    if query_schema.result_shape == "entity":
        contexts = _collection_entity_contexts(
            config,
            graph,
            query_name,
            query_schema,
            params,
        )
        policy_summary: dict[str, int] = {}
    elif query_schema.result_shape == "relationship":
        contexts, policy_summary = _collection_relationship_contexts(
            config,
            graph,
            query_name,
            query_schema,
            params,
            relationship_state=effective_options.relationship_state,
            builder=builder,
        )
    else:
        raise QueryExecutionError(
            f"Unsupported collection query result_shape '{query_schema.result_shape}'"
        )

    contexts = _apply_includes(
        config,
        graph,
        query_schema,
        contexts,
        params,
        relationship_state=effective_options.relationship_state,
        builder=builder,
    )
    contexts = sort_query_row_contexts(
        contexts,
        query_schema.order_by,
        params,
        config=config,
    )
    total_results = len(contexts)
    limited_contexts = (
        contexts[: query_schema.limit] if query_schema.limit is not None else contexts
    )
    limit_truncated = query_schema.limit is not None and total_results > query_schema.limit
    truncation_reasons = ["limit"] if limit_truncated else []
    result_rows: list[QueryRow] = [
        (
            project_query_row(query_schema.select, context, params)
            if query_schema.select is not None
            else context.row
        )
        for context in limited_contexts
    ]
    result_dicts = [dump_query_row(row, include_source=True) for row in result_rows]
    builder.record_results(
        result_dicts,
        detail={
            "total_results": total_results,
            "limit": query_schema.limit,
            "truncated": limit_truncated,
            "limit_truncated": limit_truncated,
            "path_truncated": False,
            "truncation_reasons": truncation_reasons,
            "max_paths": None,
            "max_paths_per_result": None,
            "total_path_count": None,
            "retained_path_count": None,
            "evaluated_path_candidate_count": None,
            "policy_summary": policy_summary,
        },
    )
    receipt = builder.build(result_dicts)
    return QueryResult(
        query_name=query_name,
        parameters=params,
        results=result_rows,
        result_shape=query_schema.result_shape,
        dedupe=effective_options.dedupe,
        relationship_state=effective_options.relationship_state,
        steps_executed=0,
        total_results=total_results,
        limit=query_schema.limit,
        truncated=limit_truncated,
        limit_truncated=limit_truncated,
        path_truncated=False,
        truncation_reasons=truncation_reasons,
        max_paths=None,
        max_paths_per_result=None,
        total_path_count=None,
        retained_path_count=None,
        receipt=receipt,
        policy_summary=policy_summary,
    )


def _collection_entity_contexts(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    query_schema: NamedQuerySchema,
    params: dict[str, Any],
) -> list[QueryRowContext]:
    entity_type = _normalize_entity_returns(query_schema.returns)
    if entity_type not in config.entity_types:
        raise QueryExecutionError(
            f"Entryless query '{query_name}' declares unknown result entity type "
            f"'{query_schema.returns}'"
        )
    contexts: list[QueryRowContext] = []
    for raw_entity in _collection_entity_candidates(
        config,
        graph,
        entity_type,
        query_schema.where,
        params,
    ):
        entity = entity_with_identity_properties(config, raw_entity)
        if query_schema.where is not None:
            predicate_context = _entity_collection_predicate_context(entity)
            if not evaluate_query_predicates(
                config,
                query_schema.where,
                predicate_context,
                params,
            ):
                continue
        contexts.append(
            QueryRowContext(
                row=entity,
                entry=entity,
                result=entity,
                entities=(entity,),
                path=(),
            )
        )
    return contexts


def _collection_entity_candidates(
    config: CoreConfig,
    graph: EntityGraph,
    entity_type: str,
    where: Any | None,
    params: dict[str, Any],
) -> list[EntityInstance]:
    if where is None:
        return graph.list_entities(entity_type)

    entity_id = _collection_entity_id_eq_filter(where, params)
    if entity_id is not _NO_COLLECTION_PUSHDOWN:
        if not isinstance(entity_id, str):
            return []
        entity = graph.get_entity(entity_type, entity_id)
        return [entity] if entity is not None else []

    primary_key_id = _collection_entity_primary_key_eq_filter(
        config,
        entity_type,
        where,
        params,
    )
    if primary_key_id is not _NO_COLLECTION_PUSHDOWN:
        if not isinstance(primary_key_id, str):
            return []
        entity = graph.get_entity(entity_type, primary_key_id)
        return [entity] if entity is not None else []

    property_filter = _collection_entity_property_eq_filter(
        config,
        entity_type,
        where,
        params,
    )
    if property_filter:
        return graph.list_entities(entity_type, property_filter=property_filter)

    return graph.list_entities(entity_type)


def _collection_entity_id_eq_filter(where: Any, params: dict[str, Any]) -> Any:
    operators = where.root.get("result.entity_id")
    if not isinstance(operators, dict) or "eq" not in operators:
        return _NO_COLLECTION_PUSHDOWN
    expected = _resolve_collection_pushdown_expected(operators["eq"], params)
    if expected is _NO_COLLECTION_PUSHDOWN:
        return _NO_COLLECTION_PUSHDOWN
    # entity_id is stored as an identity string, so raw == only matches the typed
    # predicate when the expected value is itself a plain string. A non-string
    # expected (e.g. int) would be type-coerced by the typed matcher, so pushing
    # it down here could silently drop candidates the predicate would include.
    if not _pushdown_value_eq_is_sound(expected):
        return _NO_COLLECTION_PUSHDOWN
    return expected


def _collection_entity_primary_key_eq_filter(
    config: CoreConfig,
    entity_type: str,
    where: Any,
    params: dict[str, Any],
) -> Any:
    primary_key_properties = _entity_primary_key_properties(config, entity_type)
    for property_name in primary_key_properties:
        operators = where.root.get(f"result.properties.{property_name}")
        if not isinstance(operators, dict) or "eq" not in operators:
            continue
        if not _pushdown_property_eq_is_sound(config, entity_type, property_name):
            continue
        expected = _resolve_collection_pushdown_expected(operators["eq"], params)
        if expected is _NO_COLLECTION_PUSHDOWN:
            continue
        if not _pushdown_value_eq_is_sound(expected):
            continue
        return expected
    return _NO_COLLECTION_PUSHDOWN


def _collection_entity_property_eq_filter(
    config: CoreConfig,
    entity_type: str,
    where: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    property_filter: dict[str, Any] = {}
    primary_key_properties = _entity_primary_key_properties(config, entity_type)
    for predicate_path, operators in where.root.items():
        if not predicate_path.startswith("result.properties."):
            continue
        property_name = predicate_path.removeprefix("result.properties.")
        if property_name in primary_key_properties:
            continue
        if "." in property_name or not isinstance(operators, dict) or "eq" not in operators:
            continue
        if not _pushdown_property_eq_is_sound(config, entity_type, property_name):
            continue
        expected = _resolve_collection_pushdown_expected(operators["eq"], params)
        if expected is _NO_COLLECTION_PUSHDOWN:
            continue
        if not _pushdown_value_eq_is_sound(expected):
            continue
        property_filter[property_name] = expected
    return property_filter


def _pushdown_property_eq_is_sound(
    config: CoreConfig,
    entity_type: str,
    property_name: str,
) -> bool:
    """Return whether a raw ``==`` eq-filter pushdown is sound for a property.

    The collection-query pushdown (``EntityGraph.list_entities`` property_filter)
    compares stored values with a raw Python ``==``. The typed predicate matcher,
    by contrast, coerces operands by declared/inferred type before comparing
    (see :func:`resolve_query_predicate_value_type`). The pushdown therefore
    *must* be a superset of the typed match — it may never drop a candidate the
    predicate would keep.

    Raw ``==`` is provably equivalent to the typed compare only when the property
    is declared with ``string`` type: stored values are then always strings, so
    :func:`infer_predicate_value_type` returns ``None`` (no coercion) for any
    string expected value. Any numeric/bool/date/datetime/json declaration — or
    an undeclared property — can hold values the typed matcher would coerce, so
    the pushdown is disabled and the typed matcher does the work.
    """
    entity_schema = config.entity_types.get(entity_type)
    if entity_schema is None:
        return False
    prop = entity_schema.properties.get(property_name)
    if prop is None:
        return False
    return prop.type == "string"


def _pushdown_value_eq_is_sound(expected: Any) -> bool:
    """Return whether a resolved eq expected value is safe to push down raw.

    Only plain strings are safe: a non-string expected (int/float/bool/date/...)
    forces the typed matcher into a coercing comparison via
    :func:`infer_predicate_value_type`, which can match stored values that a raw
    ``==`` against the non-string literal would reject. ``bool`` is a ``str``
    subclass-free ``int`` here, so excluding non-``str`` covers it.
    """
    return isinstance(expected, str)


def _entity_primary_key_properties(config: CoreConfig, entity_type: str) -> set[str]:
    entity_schema = config.entity_types.get(entity_type)
    if entity_schema is None:
        return set()
    return {name for name, prop in entity_schema.properties.items() if prop.primary_key}


def _resolve_collection_pushdown_expected(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        prefix, sep, path = value[1:].partition(".")
        if prefix != "input" or not sep or not path:
            return _NO_COLLECTION_PUSHDOWN
        resolved = resolve_path(params, path.split("."))
        if is_missing_path(resolved):
            raise QueryExecutionError(f"Missing query input reference '{value}'")
        return resolved
    return value


def _collection_relationship_contexts(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    query_schema: NamedQuerySchema,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState,
    builder: ReceiptBuilder,
) -> tuple[list[QueryRowContext], dict[str, int]]:
    resolved = config.resolve_relationship_reference(query_schema.returns)
    if resolved is None:
        raise RelationshipNotFoundError(query_schema.returns)
    relationship_schema, is_reverse = resolved
    if is_reverse:
        raise QueryExecutionError(
            f"Entryless relationship query '{query_name}' must return canonical "
            f"relationship name '{relationship_schema.name}', not reverse alias "
            f"'{query_schema.returns}'"
        )
    if query_schema.where is not None:
        # Fail-closed parity with service_list("edges"): an edge `where` may only
        # reference properties configured on the relationship schema. Validated
        # up front (data-independent) so the same ConfigError is raised whether or
        # not any stored edge happens to match.
        validate_edge_where_property_fields(
            config,
            relationship_schema.name,
            iter_edge_where_property_fields(query_schema.where),
            subject=f"relationship type '{relationship_schema.name}'",
        )
    contexts: list[QueryRowContext] = []
    policy_summary: dict[str, int] = {}
    policies = _active_query_policies(config, query_name, relationship_schema.name)
    for relationship in sorted(
        graph.iter_relationships(relationship_schema.name),
        key=lambda item: _relationship_instance_identity(item),
    ):
        if not relationship_matches_query_state(relationship.metadata, relationship_state):
            continue
        from_entity = graph.get_entity(relationship.from_type, relationship.from_id)
        to_entity = graph.get_entity(relationship.to_type, relationship.to_id)
        if from_entity is None or to_entity is None:
            continue
        source = entity_with_identity_properties(config, from_entity)
        target = entity_with_identity_properties(config, to_entity)
        segment = QueryPathSegment.model_validate(relationship.model_dump(mode="python"))
        base_predicate_context = build_predicate_context(
            entry=source,
            current=source,
            candidate=target,
            segment=segment,
            path=(segment,),
            entities=(source, target),
        )
        predicate_context = replace(base_predicate_context, result=segment)
        if query_schema.where is not None and not evaluate_query_predicates(
            config,
            query_schema.where,
            predicate_context,
            params,
        ):
            continue
        if policies and _policy_should_suppress(
            config=config,
            policies=policies,
            from_entity=source,
            to_entity=target,
            edge_props=segment.properties,
            context={
                "query_name": query_name,
                "relationship_type": relationship_schema.name,
                "direction": "outgoing",
            },
            policy_summary=policy_summary,
            builder=builder,
            parent_id=None,
        ):
            continue
        row = QueryRelationshipRow(
            relationship_type=relationship.relationship_type,
            from_type=relationship.from_type,
            from_id=relationship.from_id,
            to_type=relationship.to_type,
            to_id=relationship.to_id,
            edge_key=relationship.edge_key,
            properties=dict(relationship.properties),
            metadata=relationship.metadata,
            entry=source,
            from_entity=source,
            to_entity=target,
        )
        contexts.append(
            QueryRowContext(
                row=row,
                entry=source,
                result=target,
                entities=(source, target),
                path=(segment,),
            )
        )
    return contexts, policy_summary


def _entity_collection_predicate_context(entity: EntityInstance) -> Any:
    dummy = QueryPathSegment(
        relationship_type="",
        from_type=entity.entity_type,
        from_id=entity.entity_id,
        to_type=entity.entity_type,
        to_id=entity.entity_id,
        properties={},
        metadata=RelationshipMetadata(),
    )
    return build_predicate_context(
        entry=entity,
        current=entity,
        candidate=entity,
        segment=dummy,
        path=(),
        entities=(entity,),
    )


def _relationship_instance_identity(relationship: Any) -> tuple[Any, ...]:
    return (
        relationship.relationship_type,
        relationship.from_type,
        relationship.from_id,
        relationship.to_type,
        relationship.to_id,
        relationship.edge_key is None,
        relationship.edge_key if relationship.edge_key is not None else -1,
    )


def _resolve_declared_entity_return_type(
    config: CoreConfig,
    query_name: str,
    query_schema: NamedQuerySchema,
    result_shape: QueryResultShape,
) -> str | None:
    """Normalize entity/path query returns to an entity type."""
    if result_shape == "relationship":
        return None
    declared = _normalize_entity_returns(query_schema.returns)
    if declared == "AnyEntity":
        return None
    if declared not in config.entity_types:
        raise QueryExecutionError(
            f"Named query '{query_name}' declares unknown result entity type "
            f"'{query_schema.returns}'"
        )
    return declared


def _normalize_entity_returns(returns: str) -> str:
    value = returns.strip()
    list_match = re.fullmatch(r"list\[\s*([A-Za-z_][\w-]*)\s*\]", value)
    if list_match is not None:
        return list_match.group(1)
    return value


def _validate_result_context_return_types(
    query_name: str,
    contexts: list[QueryRowContext],
    *,
    expected_entity_type: str,
) -> None:
    for context in contexts:
        actual = context.result
        if actual.entity_type == expected_entity_type:
            continue
        raise QueryExecutionError(
            f"Named query '{query_name}' returned {actual.entity_type}:{actual.entity_id} "
            f"but declares result entity type '{expected_entity_type}'"
        )


def _step_collect_entity_type(
    config: CoreConfig,
    query_schema: NamedQuerySchema,
    result_shape: QueryResultShape,
    declared_entity_return_type: str | None,
    step: TraversalStep,
    *,
    step_index: int,
) -> str | None:
    """Return the final-step entity collection type, when the step can emit it."""
    if (
        result_shape != "entity"
        or declared_entity_return_type is None
        or step_index != len(query_schema.traversal) - 1
    ):
        return None
    if _step_can_reach_entity_type(config, step, declared_entity_return_type):
        return declared_entity_return_type
    return None


def _step_can_reach_entity_type(
    config: CoreConfig,
    step: TraversalStep,
    entity_type: str,
) -> bool:
    """Return whether one traversal step can produce *entity_type* as a neighbor."""
    for rel_ref in step.relationship_types:
        resolved = config.resolve_relationship_reference(rel_ref)
        if resolved is None:
            raise RelationshipNotFoundError(rel_ref)
        rel_schema, is_reverse = resolved
        direction = _flip_relationship_direction(step.direction) if is_reverse else step.direction
        if direction in {"outgoing", "both"} and rel_schema.to_entity == entity_type:
            return True
        if direction in {"incoming", "both"} and rel_schema.from_entity == entity_type:
            return True
    return False


def _resolve_effective_query_options(
    config: CoreConfig,
    query_name: str,
    query_schema: NamedQuerySchema,
    relationship_state_override: QueryRelationshipState | None,
) -> _EffectiveQueryOptions:
    effective_relationship_state = (
        query_schema.relationship_state
        if relationship_state_override is None
        else relationship_state_override
    )
    relationship_state_source = (
        "query_config" if relationship_state_override is None else "runtime_override"
    )
    _validate_effective_query_options(
        config,
        query_name,
        query_schema,
        effective_relationship_state,
        relationship_state_override=relationship_state_override,
    )
    return _EffectiveQueryOptions(
        relationship_state=effective_relationship_state,
        relationship_state_source=relationship_state_source,
        result_shape=query_schema.result_shape,
        dedupe=query_schema.dedupe,
    )


def _validate_effective_query_options(
    config: CoreConfig,
    query_name: str,
    query_schema: NamedQuerySchema,
    effective_relationship_state: QueryRelationshipState,
    *,
    relationship_state_override: QueryRelationshipState | None,
) -> None:
    """Validate execution-time query options after runtime overrides are applied."""
    is_collection = query_schema.mode == "collection"
    if (
        relationship_state_override is not None
        and not query_schema.allow_relationship_state_override
    ):
        raise QueryExecutionError("relationship_state override is not allowed for this named query")
    if effective_relationship_state not in {"live", "accepted", "pending", "reviewable"}:
        raise QueryExecutionError(
            f"Unsupported relationship_state '{effective_relationship_state}'"
        )
    if query_schema.result_shape == "entity" and query_schema.dedupe != "entity":
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'entity' requires dedupe 'entity'"
        )
    has_non_required_step = any(not step.required for step in query_schema.traversal)
    if query_schema.result_shape == "entity" and has_non_required_step:
        raise QueryExecutionError(
            f"Named query '{query_name}' with required false traversal steps requires "
            "result_shape 'path' or 'relationship'"
        )
    if query_schema.result_shape == "entity" and (
        query_schema.max_paths is not None or query_schema.max_paths_per_result is not None
    ):
        raise QueryExecutionError(
            f"Named query '{query_name}' with path budgets requires "
            "result_shape 'path' or 'relationship'"
        )
    if effective_relationship_state == "pending":
        if query_schema.result_shape not in {"path", "relationship"} and not (
            is_collection and query_schema.include
        ):
            raise QueryExecutionError(
                f"Named query '{query_name}' with relationship_state 'pending' "
                "requires result_shape 'path' or 'relationship'"
            )
        if query_schema.dedupe == "entity" and not (is_collection and query_schema.include):
            raise QueryExecutionError(
                f"Named query '{query_name}' with relationship_state 'pending' "
                "requires dedupe 'path' or 'none'"
            )
    if effective_relationship_state == "reviewable":
        if query_schema.result_shape != "path" and not (
            (is_collection and query_schema.result_shape == "relationship")
            or (is_collection and query_schema.include)
        ):
            raise QueryExecutionError(
                f"Named query '{query_name}' with relationship_state 'reviewable' "
                "requires result_shape 'path'"
            )
        if query_schema.dedupe == "entity" and not (is_collection and query_schema.include):
            raise QueryExecutionError(
                f"Named query '{query_name}' with relationship_state 'reviewable' "
                "requires dedupe 'path' or 'none'"
            )
    if query_schema.result_shape != "relationship":
        return
    if query_schema.dedupe == "entity":
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' requires "
            "dedupe 'path' or 'none'"
        )
    if is_collection:
        resolved = config.resolve_relationship_reference(query_schema.returns)
        if resolved is None:
            raise RelationshipNotFoundError(query_schema.returns)
        rel_schema, is_reverse = resolved
        if is_reverse:
            raise QueryExecutionError(
                f"Entryless relationship query '{query_name}' must return canonical "
                f"relationship name '{rel_schema.name}'"
            )
        return
    if not query_schema.traversal:
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' requires traversal"
        )
    if has_non_required_step and not query_schema.traversal[-1].required:
        raise QueryExecutionError(
            f"Named query '{query_name}' with result_shape 'relationship' requires "
            "the final traversal step to be required when using required false"
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


def _execute_step(
    config: CoreConfig,
    graph: EntityGraph,
    step: TraversalStep,
    current_states: list[_TraversalState],
    params: dict[str, Any],
    query_name: str,
    requires_path_retention: bool,
    relationship_state: QueryRelationshipState,
    traversal_budget: _TraversalBudgetState,
    step_index: int,
    policy_summary: dict[str, int],
    optional_path_aliases: frozenset[str],
    collect_entity_type: str | None = None,
    *,
    builder: ReceiptBuilder | None = None,
) -> list[_TraversalState]:
    """Execute one traversal step via BFS with multi-relationship fan-out.

    Supports multiple relationship types per step and multi-hop traversal
    via max_depth. Entity-shaped queries can prune repeated entities during
    expansion and can collect only declared return-type entities on the final
    step while traversing intermediate bridge entities. Path-retaining queries
    preserve distinct evidence paths for path or relationship-shaped output.
    Three dedup layers:
      1. Entity-frontier pruning: compact entity queries expand each node at most once
      2. Result dedup: final rows are deduped by entity, path, or not at all
      3. Evidence: all traversed edges are recorded in the receipt before dedup
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
    # Queue entries: (root input index, path state, current_depth)
    queue: deque[tuple[int, _TraversalState, int]] = deque()
    seen_expanded: set[str] = set()  # nodes already expanded (neighbors queried)
    seen_results: set[str] = set()  # nodes already in result list
    produced_roots: set[int] = set()

    indexed_states = list(enumerate(current_states))
    if requires_path_retention and traversal_budget.max_paths is not None:
        indexed_states.sort(key=lambda item: _traversal_state_identity(item[1]))

    # Seed queue with input entities (they are inputs, not results)
    for root_index, state in indexed_states:
        nid = state.current.node_id()
        if not requires_path_retention:
            seen_expanded.add(nid)
            seen_results.add(nid)
        queue.append((root_index, state, 0))

    while queue:
        root_index, state, depth = queue.popleft()
        entity = state.current

        if depth >= step.max_depth:
            continue
        if _retained_path_cap_reached(
            next_states,
            requires_path_retention=requires_path_retention,
            traversal_budget=traversal_budget,
        ):
            _record_max_paths_truncation(
                traversal_budget,
                builder,
                step_index=step_index,
                step=step,
                retained_path_count=len(next_states),
            )
            queue.clear()
            break

        for rel_type, direction in resolved_refs:
            if _retained_path_cap_reached(
                next_states,
                requires_path_retention=requires_path_retention,
                traversal_budget=traversal_budget,
            ):
                _record_max_paths_truncation(
                    traversal_budget,
                    builder,
                    step_index=step_index,
                    step=step,
                    retained_path_count=len(next_states),
                )
                queue.clear()
                break
            rel_policies = step_policies.get(rel_type, [])
            relationships = _step_relationships_for_execution(
                graph,
                entity,
                relationship_type=rel_type,
                direction=direction,
                alias=step.alias,
                stable=requires_path_retention and traversal_budget.max_paths is not None,
            )
            for neighbor, segment, relative_direction in relationships:
                if _retained_path_cap_reached(
                    next_states,
                    requires_path_retention=requires_path_retention,
                    traversal_budget=traversal_budget,
                ):
                    _record_max_paths_truncation(
                        traversal_budget,
                        builder,
                        step_index=step_index,
                        step=step,
                        retained_path_count=len(next_states),
                    )
                    queue.clear()
                    break
                if not relationship_matches_query_state(segment.metadata, relationship_state):
                    continue

                nid = neighbor.node_id()
                if requires_path_retention and any(
                    path_entity.node_id() == nid for path_entity in state.entities
                ):
                    continue
                if requires_path_retention and traversal_budget.max_paths is not None:
                    traversal_budget.evaluated_path_candidate_count += 1

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

                matches_collect_type = _matches_collect_entity_type(
                    collect_entity_type,
                    neighbor,
                )

                # Apply target entity filter to emitted candidates. In typed
                # collection mode, non-return intermediates remain bridge nodes.
                if step.target_filter and matches_collect_type:
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

                predicate_context = None
                if matches_collect_type:
                    predicate_context = build_predicate_context(
                        entry=state.entry,
                        current=entity,
                        candidate=neighbor,
                        segment=segment,
                        path=state.path,
                        entities=state.entities,
                        optional_path_aliases=optional_path_aliases,
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

                # Apply constraint to emitted candidates. Non-return
                # intermediates may still bridge to return-type entities.
                if step.constraint and matches_collect_type:
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

                if step.exclude_if_related and matches_collect_type:
                    excluded = False
                    for exclusion in step.exclude_if_related:
                        passed = not related_edge_exists(
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

                if step.where_related and matches_collect_type:
                    if predicate_context is None:
                        predicate_context = build_predicate_context(
                            entry=state.entry,
                            current=entity,
                            candidate=neighbor,
                            segment=segment,
                            path=state.path,
                            entities=state.entities,
                            optional_path_aliases=optional_path_aliases,
                        )
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

                if step.where_not_related and matches_collect_type:
                    if predicate_context is None:
                        predicate_context = build_predicate_context(
                            entry=state.entry,
                            current=entity,
                            candidate=neighbor,
                            segment=segment,
                            path=state.path,
                            entities=state.entities,
                            optional_path_aliases=optional_path_aliases,
                        )
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
                if _retained_path_cap_reached(
                    next_states,
                    requires_path_retention=requires_path_retention,
                    traversal_budget=traversal_budget,
                ):
                    _record_max_paths_truncation(
                        traversal_budget,
                        builder,
                        step_index=step_index,
                        step=step,
                        retained_path_count=len(next_states),
                    )
                    queue.clear()
                    break

                # Result dedup: first path owns the lineage
                if not requires_path_retention and matches_collect_type:
                    if nid not in seen_results:
                        seen_results.add(nid)
                        next_states.append(next_state)
                        produced_roots.add(root_index)
                else:
                    if requires_path_retention:
                        next_states.append(next_state)
                        produced_roots.add(root_index)

                # Expansion dedup: enqueue for deeper hops if not yet expanded
                if not requires_path_retention and nid not in seen_expanded:
                    seen_expanded.add(nid)
                    queue.append((root_index, next_state, depth + 1))
                elif requires_path_retention:
                    if _retained_path_cap_reached(
                        next_states,
                        requires_path_retention=requires_path_retention,
                        traversal_budget=traversal_budget,
                    ):
                        if depth + 1 < step.max_depth:
                            _record_max_paths_truncation(
                                traversal_budget,
                                builder,
                                step_index=step_index,
                                step=step,
                                retained_path_count=len(next_states),
                            )
                    else:
                        queue.append((root_index, next_state, depth + 1))

    if not step.required:
        fallback_states = (
            indexed_states
            if (requires_path_retention and traversal_budget.max_paths is not None)
            else list(enumerate(current_states))
        )
        for root_index, state in fallback_states:
            if root_index in produced_roots:
                continue
            if _retained_path_cap_reached(
                next_states,
                requires_path_retention=requires_path_retention,
                traversal_budget=traversal_budget,
            ):
                _record_max_paths_truncation(
                    traversal_budget,
                    builder,
                    step_index=step_index,
                    step=step,
                    retained_path_count=len(next_states),
                )
                break
            if builder is not None:
                builder.record_validation(
                    passed=True,
                    detail={
                        "optional_traversal_preserved": True,
                        "relationship": step.relationship,
                        "direction": step.direction,
                        "alias": step.alias,
                        "reason": "no_matching_segment",
                    },
                    parent_id=state.parent_id,
                )
            next_states.append(state)

    return next_states


def _matches_collect_entity_type(
    collect_entity_type: str | None,
    candidate: EntityInstance,
) -> bool:
    return collect_entity_type is None or candidate.entity_type == collect_entity_type


def _step_relationships_for_execution(
    graph: EntityGraph,
    entity: EntityInstance,
    *,
    relationship_type: str,
    direction: str,
    alias: str | None,
    stable: bool,
) -> list[tuple[EntityInstance, QueryPathSegment, str]]:
    relationships = iter_step_relationships(
        graph,
        entity,
        relationship_type=relationship_type,
        direction=direction,
        alias=alias,
    )
    if not stable:
        return relationships
    return sorted(relationships, key=_relationship_candidate_identity)


def _relationship_candidate_identity(
    row: tuple[EntityInstance, QueryPathSegment, str],
) -> tuple[Any, ...]:
    neighbor, segment, relative_direction = row
    return (
        segment.relationship_type,
        segment.from_type,
        segment.from_id,
        segment.to_type,
        segment.to_id,
        segment.edge_key is None,
        segment.edge_key if segment.edge_key is not None else -1,
        neighbor.entity_type,
        neighbor.entity_id,
        relative_direction,
    )


def _traversal_state_identity(state: _TraversalState) -> tuple[Any, ...]:
    return (
        state.current.entity_type,
        state.current.entity_id,
        _path_identity(state.path),
    )


def _retained_path_cap_reached(
    retained_states: list[_TraversalState],
    *,
    requires_path_retention: bool,
    traversal_budget: _TraversalBudgetState,
) -> bool:
    return (
        requires_path_retention
        and traversal_budget.max_paths is not None
        and len(retained_states) >= traversal_budget.max_paths
    )


def _record_max_paths_truncation(
    traversal_budget: _TraversalBudgetState,
    builder: ReceiptBuilder | None,
    *,
    step_index: int,
    step: TraversalStep,
    retained_path_count: int,
) -> None:
    traversal_budget.truncated = True
    if builder is None or traversal_budget.truncation_recorded:
        return
    traversal_budget.truncation_recorded = True
    builder.record_validation(
        passed=False,
        detail={
            "path_truncated": True,
            "truncation_reason": "max_paths",
            "max_paths": traversal_budget.max_paths,
            "retained_path_count": retained_path_count,
            "evaluated_path_candidate_count": (traversal_budget.evaluated_path_candidate_count),
            "step": step_index,
            "relationship": step.relationship,
            "direction": step.direction,
        },
    )


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
    """Return a stable graph identity for one evidence path.

    ``edge_key`` may be ``None`` for some segments and an ``int`` for others
    between identical endpoints. A raw ``None`` in the sort tuple would raise a
    ``TypeError`` when this identity is used as a sort key (see the path
    traversal sort under ``max_paths``), so the key is encoded None-safely,
    mirroring ``query.projection._path_identity``.
    """
    return tuple(
        (
            segment.relationship_type,
            segment.from_type,
            segment.from_id,
            segment.to_type,
            segment.to_id,
            segment.edge_key is None,
            segment.edge_key if segment.edge_key is not None else -1,
        )
        for segment in path
    )


def _build_result_contexts(
    config: CoreConfig,
    states: list[_TraversalState],
    result_shape: QueryResultShape,
    *,
    optional_path_aliases: frozenset[str] = frozenset(),
) -> list[QueryRowContext]:
    contexts: list[QueryRowContext] = []
    for state in states:
        entry = entity_with_identity_properties(config, state.entry)
        result = entity_with_identity_properties(config, state.current)
        entities = tuple(
            entity_with_identity_properties(config, entity) for entity in state.entities
        )
        row: BaseQueryRow | None
        if result_shape == "path":
            row = QueryPathRow(
                entry=entry,
                result=result,
                entities=list(entities),
                path=list(state.path),
            )
        elif result_shape == "relationship":
            row = _build_relationship_row(config, state) if state.path else None
        else:
            row = result
        if row is None:
            continue
        contexts.append(
            QueryRowContext(
                row=row,
                entry=entry,
                result=result,
                entities=entities,
                path=state.path,
                parent_id=state.parent_id,
                optional_path_aliases=optional_path_aliases,
            )
        )
    return contexts


def _apply_includes(
    config: CoreConfig,
    graph: EntityGraph,
    query_schema: NamedQuerySchema,
    contexts: list[QueryRowContext],
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState,
    builder: ReceiptBuilder | None,
) -> list[QueryRowContext]:
    """Attach one-hop include side context to base query row contexts."""
    if not query_schema.include:
        return contexts

    retained: list[QueryRowContext] = []
    summaries: dict[str, dict[str, Any]] = {
        alias: {
            "alias": alias,
            "from": spec.from_,
            "relationship": spec.relationship,
            "direction": spec.direction,
            "many": spec.many,
            "required": spec.required,
            "limit": spec.limit,
            "relationship_state": relationship_state,
            "rows_evaluated": 0,
            "rows_with_matches": 0,
            "total_matches": 0,
            "truncated_rows": 0,
        }
        for alias, spec in query_schema.include.items()
    }
    # Under `dedupe: path`, several retained rows can share the same include
    # anchor (e.g. the same `$result` entity reached by distinct evidence
    # paths). Each row's `$include.*.count` is independently correct, but a
    # naive sum over rows double-counts those shared anchors. An anchor-only
    # dedupe key, however, *under*-counts when the include `where`/`where_related`
    # predicates reference `$entry.*`: the same anchor reached under two
    # different entry rows can legitimately match different neighbors, so
    # collapsing on anchor alone drops the matches found under the later entry.
    # Track the set of distinct matched-neighbor identities per alias instead.
    # Identical matches (same anchor, same edges) collapse to one; genuinely
    # different matches (entry-sensitive includes) each contribute. This is
    # exact regardless of whether the include depends on entry scope.
    counted_matches: dict[str, set[tuple[Any, ...]]] = {
        alias: set() for alias in query_schema.include
    }

    for context in contexts:
        includes: dict[str, QueryIncludeResult] = {}
        drop_row = False
        for alias, spec in query_schema.include.items():
            result = _evaluate_include(
                config,
                graph,
                alias,
                spec,
                context,
                params,
                relationship_state=relationship_state,
            )
            summary = summaries[alias]
            summary["rows_evaluated"] += 1
            seen = counted_matches[alias]
            for identity in result.match_identities:
                if identity not in seen:
                    seen.add(identity)
                    summary["total_matches"] += 1
            if result.exists:
                summary["rows_with_matches"] += 1
            if result.truncated:
                summary["truncated_rows"] += 1
            includes[alias] = result
            if spec.required and not result.exists:
                drop_row = True
                break
        if drop_row:
            continue
        retained.append(_context_with_includes(context, includes))

    if builder is not None:
        builder.record_validation(
            passed=True,
            detail={"include_summary": list(summaries.values())},
        )
    return retained


def _evaluate_include(
    config: CoreConfig,
    graph: EntityGraph,
    alias: str,
    spec: QueryIncludeSpec,
    context: QueryRowContext,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState,
) -> QueryIncludeResult:
    anchor = _resolve_include_anchor(alias, spec.from_, context)
    if anchor is None:
        return QueryIncludeResult(
            alias=alias,
            many=spec.many,
            limit=spec.limit,
        )
    resolved = config.resolve_relationship_reference(spec.relationship)
    if resolved is None:
        raise RelationshipNotFoundError(spec.relationship)
    relationship_schema, is_reverse = resolved
    direction = _flip_relationship_direction(spec.direction) if is_reverse else spec.direction
    relationships = sorted(
        iter_step_relationships(
            graph,
            anchor,
            relationship_type=relationship_schema.name,
            direction=direction,
            alias=alias,
        ),
        key=_relationship_candidate_identity,
    )
    items: list[QueryIncludeItem] = []
    for neighbor, segment, _relative_direction in relationships:
        if not relationship_matches_query_state(segment.metadata, relationship_state):
            continue
        predicate_context = build_predicate_context(
            entry=context.entry,
            current=anchor,
            candidate=neighbor,
            segment=segment,
            path=context.path,
            entities=context.entities,
            optional_path_aliases=context.optional_path_aliases,
        )
        if spec.where is not None and not evaluate_query_predicates(
            config,
            spec.where,
            predicate_context,
            params,
        ):
            continue
        if spec.where_related and not all(
            evaluate_related_predicate(
                graph,
                related,
                predicate_context,
                params,
                config=config,
                relationship_state=relationship_state,
            )
            for related in spec.where_related
        ):
            continue
        if spec.where_not_related and any(
            evaluate_related_predicate(
                graph,
                related,
                predicate_context,
                params,
                config=config,
                relationship_state=relationship_state,
            )
            for related in spec.where_not_related
        ):
            continue

        source, target = segment_endpoint_entities(anchor, neighbor, segment)
        items.append(
            QueryIncludeItem(
                edge=segment,
                source=entity_with_identity_properties(config, source),
                target=entity_with_identity_properties(config, target),
            )
        )

    ordered_items = _sort_include_items(items, spec.order_by, params, config)
    count = len(ordered_items)
    if not spec.many and count > 1:
        raise QueryExecutionError(
            f"Include '{alias}' matched {count} relationships; set many: true "
            "or narrow the include predicates"
        )
    returned_items = ordered_items[: spec.limit] if spec.limit is not None else ordered_items
    # Carry the identities of *all* matched neighbors (before `limit`
    # truncation) so the include summary can count distinct matches even when
    # the displayed `items` are truncated.
    match_identities = tuple(_include_match_identity(item) for item in ordered_items)
    return QueryIncludeResult(
        alias=alias,
        many=spec.many,
        exists=count > 0,
        count=count,
        limit=spec.limit,
        truncated=spec.limit is not None and count > spec.limit,
        items=returned_items,
        match_identities=match_identities,
    )


def _include_match_identity(item: QueryIncludeItem) -> tuple[Any, ...]:
    """Stable identity of one matched include neighbor.

    Combines the matched edge identity (including ``edge_key`` so parallel
    multigraph edges stay distinct) with the resolved source/target endpoints.
    Two include matches share an identity iff they are the same relationship
    between the same entities -- so identical matches reached via different
    rows collapse, while genuinely different matches (e.g. surfaced by an
    entry-sensitive include `where`) each count once.
    """
    edge = item.edge
    return (
        edge.relationship_type,
        edge.from_type,
        edge.from_id,
        edge.to_type,
        edge.to_id,
        edge.edge_key,
        item.source.entity_type,
        item.source.entity_id,
        item.target.entity_type,
        item.target.entity_id,
    )


def _context_with_includes(
    context: QueryRowContext,
    includes: dict[str, QueryIncludeResult],
) -> QueryRowContext:
    row = context.row
    if isinstance(row, QueryPathRow | QueryRelationshipRow):
        row = row.model_copy(update={"includes": includes})
    return replace(context, row=row, includes=includes)


def _resolve_include_anchor(
    alias: str,
    ref: str,
    context: QueryRowContext,
) -> EntityInstance | None:
    if ref == "$entry":
        return context.entry
    if ref == "$result":
        return context.result
    if not ref.startswith("$path."):
        raise QueryExecutionError(
            f"Include '{alias}' from reference '{ref}' must use $entry, $result, "
            "or $path.<alias>.source|target"
        )
    parts = ref[1:].split(".")
    if len(parts) != 3 or parts[2] not in {"source", "target"}:
        raise QueryExecutionError(
            f"Include '{alias}' from reference '{ref}' must use "
            "$path.<alias>.source or $path.<alias>.target"
        )
    path_alias = parts[1]
    segment = _path_segment_by_alias(
        path_alias,
        context.path,
        optional_path_aliases=context.optional_path_aliases,
    )
    if segment is None:
        return None
    if parts[2] == "source":
        return _find_context_entity(context, segment.from_type, segment.from_id)
    return _find_context_entity(context, segment.to_type, segment.to_id)


def _find_context_entity(
    context: QueryRowContext,
    entity_type: str,
    entity_id: str,
) -> EntityInstance:
    for entity in context.entities:
        if entity.entity_type == entity_type and entity.entity_id == entity_id:
            return entity
    raise QueryExecutionError(
        f"Include anchor path references missing entity {entity_type}:{entity_id}"
    )


def _path_segment_by_alias(
    alias: str,
    path: tuple[QueryPathSegment, ...],
    *,
    optional_path_aliases: frozenset[str],
) -> QueryPathSegment | None:
    matches = [segment for segment in path if segment.alias == alias]
    if not matches:
        if alias in optional_path_aliases:
            return None
        raise QueryExecutionError(f"Unknown path alias '{alias}' in include anchor")
    if len(matches) > 1:
        raise QueryExecutionError(f"Duplicate path alias '{alias}' in query result path")
    return matches[0]


def _sort_include_items(
    items: list[QueryIncludeItem],
    order_by: list[QueryOrderSpec],
    params: dict[str, Any],
    config: CoreConfig,
) -> list[QueryIncludeItem]:
    if not order_by:
        return sorted(items, key=_include_item_identity)

    def compare(left: QueryIncludeItem, right: QueryIncludeItem) -> int:
        for order in order_by:
            left_value = _resolve_include_order_value(order, left, params, config)
            right_value = _resolve_include_order_value(order, right, params, config)
            if left_value is None and right_value is None:
                continue
            if left_value is None:
                return 1
            if right_value is None:
                return -1
            result = compare_order_values(left_value, right_value)
            if result != 0:
                return -result if order.direction == "desc" else result
        return compare_sort_keys(_include_item_identity(left), _include_item_identity(right))

    return sorted(items, key=cmp_to_key(compare))


def _resolve_include_order_value(
    order: QueryOrderSpec,
    item: QueryIncludeItem,
    params: dict[str, Any],
    config: CoreConfig,
) -> Any:
    value = _resolve_include_order_ref(order.by, item, params)
    return coerce_query_order_value(value, order, config, label=order.by)


def _resolve_include_order_ref(
    ref: str,
    item: QueryIncludeItem,
    params: dict[str, Any],
) -> Any:
    if not ref.startswith("$"):
        raise QueryExecutionError(f"Include order reference '{ref}' must start with '$'")
    scope, sep, raw_path = ref[1:].partition(".")
    if not sep or not raw_path:
        raise QueryExecutionError(f"Invalid include order reference '{ref}'")
    if scope == "input":
        value = resolve_path(params, raw_path.split("."))
        if is_missing_path(value):
            raise QueryExecutionError(f"Missing query input reference '{ref}'")
        return value
    if scope == "edge":
        base: Any = item.edge
    elif scope == "source":
        base = item.source
    elif scope == "target":
        base = item.target
    else:
        raise QueryExecutionError(
            f"Include order reference '{ref}' must use $edge, $source, or $target"
        )
    value = resolve_path(base, raw_path.split("."))
    return None if is_missing_path(value) else value


def _include_item_identity(item: QueryIncludeItem) -> tuple[Any, ...]:
    return (
        item.edge.relationship_type,
        item.edge.from_type,
        item.edge.from_id,
        item.edge.to_type,
        item.edge.to_id,
        item.edge.edge_key is None,
        item.edge.edge_key if item.edge.edge_key is not None else -1,
        item.source.entity_type,
        item.source.entity_id,
        item.target.entity_type,
        item.target.entity_id,
    )


def _optional_path_aliases(query_schema: NamedQuerySchema) -> frozenset[str]:
    """Return aliases that may be absent because their traversal step is non-required."""
    return frozenset(
        step.alias
        for step in query_schema.traversal
        if not step.required and step.alias is not None
    )


def _apply_path_budgets(
    contexts: list[QueryRowContext],
    *,
    max_paths_per_result: int | None,
    traversal_max_paths: int | None,
    traversal_truncated: bool,
) -> _PathBudgetResult:
    """Apply final retained-path-per-result budget before query ordering and limit."""
    if traversal_max_paths is None and max_paths_per_result is None:
        return _PathBudgetResult(
            contexts=list(contexts),
            total_path_count=None,
            retained_path_count=None,
            path_truncated=False,
            truncation_reasons=[],
        )

    ordered = sorted(contexts, key=stable_row_identity)
    total_path_count = None if traversal_truncated else len(ordered)
    retained = ordered
    reasons: list[str] = ["max_paths"] if traversal_truncated else []

    if max_paths_per_result is not None:
        per_result_counts: dict[tuple[str, str], int] = {}
        per_result_retained: list[QueryRowContext] = []
        for context in retained:
            key = (context.result.entity_type, context.result.entity_id)
            count = per_result_counts.get(key, 0)
            if count >= max_paths_per_result:
                if "max_paths_per_result" not in reasons:
                    reasons.append("max_paths_per_result")
                continue
            per_result_counts[key] = count + 1
            per_result_retained.append(context)
        retained = per_result_retained

    retained_path_count = len(retained)
    return _PathBudgetResult(
        contexts=retained,
        total_path_count=total_path_count,
        retained_path_count=retained_path_count,
        path_truncated=bool(reasons),
        truncation_reasons=reasons,
    )


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


_CONSTRAINT_RE = re.compile(rf"^(target|source)\.([\w-]+)\s*{COMPARISON_SYMBOL_PATTERN}\s*(.+)$")


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
            "source-side traversal constraints are not supported; use target.<property> constraints"
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
