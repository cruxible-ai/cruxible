"""Query and read service functions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Literal, cast

from pydantic import ValidationError

from cruxible_core.config.schema import CoreConfig, NamedQuerySchema
from cruxible_core.errors import (
    ConfigError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    TraceNotFoundError,
)
from cruxible_core.graph.provenance import (
    dump_provenance,
    provenance_group_id,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.query.engine import execute_query_definition
from cruxible_core.query.enums import QueryRelationshipState
from cruxible_core.query.read_surface import (
    get_entity as read_get_entity,
)
from cruxible_core.query.read_surface import (
    get_relationship as read_get_relationship,
)
from cruxible_core.query.read_surface import (
    graph_stats as read_graph_stats,
)
from cruxible_core.query.read_surface import (
    inspect_entity as read_inspect_entity,
)
from cruxible_core.query.read_surface import (
    list_entities as read_list_entities,
)
from cruxible_core.query.read_surface import (
    list_relationships as read_list_relationships,
)
from cruxible_core.query.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.query.read_surface import (
    sample_entities as read_sample_entities,
)
from cruxible_core.query.types import dump_query_row
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.decisions import record_decision_event_for_context
from cruxible_core.service.types import (
    InspectEntityResult,
    InspectNeighborResult,
    ListResult,
    NeighborDirection,
    OperationContext,
    QueryDefinitionServiceResult,
    QueryParamHints,
    QueryServiceResult,
    RelationshipLineageResult,
    StatsServiceResult,
    TraceListResult,
)
from cruxible_core.temporal import utc_now

_INPUT_REF_RE = re.compile(r"\$input\.([A-Za-z_][\w-]*)")
_CONSTRAINT_PARAM_RE = re.compile(r"\$([A-Za-z_][\w-]*)(?:\.([A-Za-z_][\w-]*))?")
_INLINE_QUERY_PREFIX = "inline:"
_INLINE_DEFAULT_LIMIT = 50
_INLINE_MAX_LIMIT = 500
_INLINE_DEFAULT_MAX_PATHS = 1000
_INLINE_MAX_PATHS = 5000
_INLINE_DEFAULT_MAX_PATHS_PER_RESULT = 25
_INLINE_MAX_PATHS_PER_RESULT = 100

# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def service_query(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
    context: OperationContext | None = None,
) -> QueryServiceResult:
    """Execute a named query and persist the receipt.

    Returns results, receipt, and execution metadata.
    """
    started_at = utc_now()
    input_event = {
        "query_name": query_name,
        "params": params,
        "relationship_state": relationship_state,
    }
    try:
        result = _evaluate_query_result(
            instance,
            query_name,
            params,
            relationship_state=relationship_state,
        )

        if result.receipt:
            with instance.write_transaction() as uow:
                uow.receipts.save_receipt(result.receipt)
    except Exception as exc:
        record_decision_event_for_context(
            instance,
            context,
            command=f"query:{query_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    receipt_head_snapshot_id = result.receipt.head_snapshot_id if result.receipt else None
    record_decision_event_for_context(
        instance,
        context,
        command=f"query:{query_name}",
        status="success",
        input_payload=input_event,
        output_payload=_query_output_payload(result),
        receipt_id=result.receipt_id,
        head_snapshot_id=receipt_head_snapshot_id,
        started_at=started_at,
    )
    return result


def service_evaluate_query(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryServiceResult:
    """Evaluate a named query without persisting receipts or decision events."""
    return _evaluate_query_result(
        instance,
        query_name,
        params,
        relationship_state=relationship_state,
    )


def service_query_surface(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    limit: int | None = None,
    relationship_state: QueryRelationshipState | None = None,
    context: OperationContext | None = None,
) -> QueryServiceResult:
    """Execute a named query and apply caller-facing result truncation."""
    surface_limit = limit
    if surface_limit is not None and surface_limit < 1:
        raise ConfigError("limit must be a positive integer")

    result = service_query(
        instance,
        query_name,
        params,
        relationship_state=relationship_state,
        context=context,
    )
    return _query_result_with_response_limit(result, surface_limit=surface_limit)


def service_query_inline_surface(
    instance: InstanceProtocol,
    definition: Mapping[str, Any],
    params: dict[str, Any],
    *,
    limit: int | None = None,
    relationship_state: QueryRelationshipState | None = None,
    context: OperationContext | None = None,
) -> QueryServiceResult:
    """Execute a bounded inline query definition without persisting it to config."""
    surface_limit = limit
    if surface_limit is not None and surface_limit < 1:
        raise ConfigError("limit must be a positive integer")

    inline_name, query_schema = _inline_query_schema(definition)
    query_name = f"{_INLINE_QUERY_PREFIX}{inline_name}"
    started_at = utc_now()
    input_event = {
        "query_name": query_name,
        "definition": _inline_query_definition_payload(inline_name, query_schema),
        "params": params,
        "relationship_state": relationship_state,
    }
    try:
        result = _evaluate_inline_query_result(
            instance,
            query_name,
            query_schema,
            params,
            relationship_state=relationship_state,
        )

        if result.receipt:
            with instance.write_transaction() as uow:
                uow.receipts.save_receipt(result.receipt)
    except Exception as exc:
        record_decision_event_for_context(
            instance,
            context,
            command=f"query_inline:{inline_name}",
            status="error",
            input_payload=input_event,
            error=exc,
            started_at=started_at,
        )
        raise

    receipt_head_snapshot_id = result.receipt.head_snapshot_id if result.receipt else None
    record_decision_event_for_context(
        instance,
        context,
        command=f"query_inline:{inline_name}",
        status="success",
        input_payload=input_event,
        output_payload=_query_output_payload(result),
        receipt_id=result.receipt_id,
        head_snapshot_id=receipt_head_snapshot_id,
        started_at=started_at,
    )
    return _query_result_with_response_limit(result, surface_limit=surface_limit)


def service_evaluate_query_surface(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    limit: int | None = None,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryServiceResult:
    """Evaluate a named query with caller-facing truncation and no persisted receipt."""
    surface_limit = limit
    if surface_limit is not None and surface_limit < 1:
        raise ConfigError("limit must be a positive integer")

    result = service_evaluate_query(
        instance,
        query_name,
        params,
        relationship_state=relationship_state,
    )
    return _query_result_with_response_limit(result, surface_limit=surface_limit)


def _evaluate_query_result(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryServiceResult:
    config = instance.load_config()
    graph = instance.load_graph()
    query_result = read_run_query(
        config,
        graph,
        query_name,
        params,
        relationship_state=relationship_state,
    )
    if query_result.receipt:
        query_result.receipt.head_snapshot_id = instance.get_head_snapshot_id()
    total = query_result.total_results or len(query_result.results)
    return QueryServiceResult(
        items=query_result.results,
        receipt_id=query_result.receipt.receipt_id if query_result.receipt else None,
        receipt=query_result.receipt,
        total=total,
        limit=query_result.limit,
        truncated=query_result.truncated,
        steps_executed=query_result.steps_executed,
        limit_truncated=query_result.limit_truncated,
        path_truncated=query_result.path_truncated,
        truncation_reasons=list(query_result.truncation_reasons),
        max_paths=query_result.max_paths,
        max_paths_per_result=query_result.max_paths_per_result,
        total_path_count=query_result.total_path_count,
        retained_path_count=query_result.retained_path_count,
        result_shape=query_result.result_shape,
        dedupe=query_result.dedupe,
        relationship_state=query_result.relationship_state,
        param_hints=_query_param_hints(config, graph, query_name),
        policy_summary=query_result.policy_summary,
    )


def _evaluate_inline_query_result(
    instance: InstanceProtocol,
    query_name: str,
    query_schema: NamedQuerySchema,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryServiceResult:
    config = instance.load_config()
    graph = instance.load_graph()
    query_result = execute_query_definition(
        config,
        graph,
        query_name,
        query_schema,
        params,
        relationship_state=relationship_state,
    )
    if query_result.receipt:
        query_result.receipt.head_snapshot_id = instance.get_head_snapshot_id()
    total = query_result.total_results or len(query_result.results)
    return QueryServiceResult(
        items=query_result.results,
        receipt_id=query_result.receipt.receipt_id if query_result.receipt else None,
        receipt=query_result.receipt,
        total=total,
        limit=query_result.limit,
        truncated=query_result.truncated,
        steps_executed=query_result.steps_executed,
        limit_truncated=query_result.limit_truncated,
        path_truncated=query_result.path_truncated,
        truncation_reasons=list(query_result.truncation_reasons),
        max_paths=query_result.max_paths,
        max_paths_per_result=query_result.max_paths_per_result,
        total_path_count=query_result.total_path_count,
        retained_path_count=query_result.retained_path_count,
        result_shape=query_result.result_shape,
        dedupe=query_result.dedupe,
        relationship_state=query_result.relationship_state,
        param_hints=_query_param_hints_for_schema(config, graph, query_schema),
        policy_summary=query_result.policy_summary,
    )


def _inline_query_schema(definition: Mapping[str, Any]) -> tuple[str, NamedQuerySchema]:
    payload = {key: value for key, value in dict(definition).items() if value is not None}
    raw_name = payload.pop("name", None)
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConfigError("inline query definition requires non-empty name")
    inline_name = raw_name.strip()
    if payload.get("limit") is None:
        payload["limit"] = _INLINE_DEFAULT_LIMIT
    try:
        query_schema = NamedQuerySchema.model_validate(payload)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error['msg']}"
            if error.get("loc")
            else str(error["msg"])
            for error in exc.errors()
        ]
        raise ConfigError("Invalid inline query definition", errors=errors) from exc

    _validate_inline_query_bound("limit", query_schema.limit, _INLINE_MAX_LIMIT)
    _validate_inline_query_bound("max_paths", query_schema.max_paths, _INLINE_MAX_PATHS)
    _validate_inline_query_bound(
        "max_paths_per_result",
        query_schema.max_paths_per_result,
        _INLINE_MAX_PATHS_PER_RESULT,
    )

    supports_path_budgets = query_schema.mode == "traversal" and query_schema.result_shape in {
        "path",
        "relationship",
    }
    if not supports_path_budgets:
        return inline_name, query_schema

    update: dict[str, Any] = query_schema.model_dump(mode="python")
    changed = False
    if query_schema.max_paths is None:
        update["max_paths"] = _INLINE_DEFAULT_MAX_PATHS
        changed = True
    if query_schema.max_paths_per_result is None:
        update["max_paths_per_result"] = _INLINE_DEFAULT_MAX_PATHS_PER_RESULT
        changed = True
    if not changed:
        return inline_name, query_schema
    return inline_name, NamedQuerySchema.model_validate(update)


def _validate_inline_query_bound(
    field_name: str,
    value: int | None,
    maximum: int,
) -> None:
    if value is None:
        return
    if value > maximum:
        raise ConfigError(f"inline query {field_name} must be <= {maximum}")


def _inline_query_definition_payload(
    inline_name: str,
    query_schema: NamedQuerySchema,
) -> dict[str, Any]:
    return {
        "name": inline_name,
        **query_schema.model_dump(mode="json", by_alias=True, exclude_none=True),
    }


def _query_output_payload(result: QueryServiceResult) -> dict[str, Any]:
    return {
        "items": [dump_query_row(row, mode="json") for row in result.items],
        "total": result.total,
        "limit": result.limit,
        "truncated": result.truncated,
        "limit_truncated": result.limit_truncated,
        "path_truncated": result.path_truncated,
        "truncation_reasons": list(result.truncation_reasons),
        "max_paths": result.max_paths,
        "max_paths_per_result": result.max_paths_per_result,
        "total_path_count": result.total_path_count,
        "retained_path_count": result.retained_path_count,
        "steps_executed": result.steps_executed,
        "result_shape": result.result_shape,
        "dedupe": result.dedupe,
        "relationship_state": result.relationship_state,
    }


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def service_schema(instance: InstanceProtocol) -> CoreConfig:
    """Get the config for an instance."""
    return instance.load_config()


def service_list_queries(instance: InstanceProtocol) -> list[QueryDefinitionServiceResult]:
    """Return named-query definitions with the invocation details agents need."""
    config = instance.load_config()
    graph = instance.load_graph()
    definitions: list[QueryDefinitionServiceResult] = []
    for name in sorted(config.named_queries.keys()):
        definitions.append(_query_definition(config, graph, name))
    return definitions


def service_describe_query(
    instance: InstanceProtocol,
    query_name: str,
) -> QueryDefinitionServiceResult:
    """Return one named-query definition with invocation details."""
    config = instance.load_config()
    graph = instance.load_graph()
    if query_name not in config.named_queries:
        raise QueryNotFoundError(query_name)
    return _query_definition(config, graph, query_name)


def service_sample(
    instance: InstanceProtocol,
    entity_type: str,
    limit: int = 5,
) -> list[EntityInstance]:
    """Sample entities of a given type."""
    config = instance.load_config()
    graph = instance.load_graph()
    return read_sample_entities(graph, entity_type, config=config, limit=limit)


def service_stats(instance: InstanceProtocol) -> StatsServiceResult:
    """Return graph counts grouped by entity and relationship type."""
    graph = instance.load_graph()
    result = read_graph_stats(graph, head_snapshot_id=instance.get_head_snapshot_id())
    return StatsServiceResult(
        entity_count=result.entity_count,
        edge_count=result.edge_count,
        entity_counts=result.entity_counts,
        relationship_counts=result.relationship_counts,
        head_snapshot_id=result.head_snapshot_id,
    )


def service_get_entity(
    instance: InstanceProtocol,
    entity_type: str,
    entity_id: str,
) -> EntityInstance | None:
    """Look up a specific entity by type and ID."""
    config = instance.load_config()
    graph = instance.load_graph()
    return read_get_entity(graph, entity_type, entity_id, config=config)


def service_inspect_entity(
    instance: InstanceProtocol,
    entity_type: str,
    entity_id: str,
    *,
    direction: Literal["incoming", "outgoing", "both"] = "both",
    relationship_type: str | None = None,
    limit: int | None = None,
) -> InspectEntityResult:
    """Look up an entity and its immediate neighbors."""
    config = instance.load_config()
    graph = instance.load_graph()
    result = read_inspect_entity(
        graph,
        entity_type,
        entity_id,
        config=config,
        direction=direction,
        relationship_type=relationship_type,
        limit=limit,
    )
    return InspectEntityResult(
        found=result.found,
        entity_type=result.entity_type,
        entity_id=result.entity_id,
        properties=result.properties,
        metadata=result.metadata,
        neighbors=[
            InspectNeighborResult(
                direction=cast(NeighborDirection, neighbor.direction),
                relationship_type=neighbor.relationship_type,
                edge_key=neighbor.edge_key,
                properties=neighbor.properties,
                metadata=neighbor.metadata,
                entity=neighbor.entity,
            )
            for neighbor in result.neighbors
        ],
        total_neighbors=result.total_neighbors,
    )


def service_get_relationship(
    instance: InstanceProtocol,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> RelationshipInstance | None:
    """Look up a specific relationship by its endpoints and type.

    Raises RelationshipAmbiguityError if multiple edges match and no edge_key given.
    """
    graph = instance.load_graph()
    return read_get_relationship(
        graph,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )


def service_get_relationship_lineage(
    instance: InstanceProtocol,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> RelationshipLineageResult:
    """Look up a relationship and follow group provenance when present."""
    relationship = service_get_relationship(
        instance,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )
    if relationship is None:
        return RelationshipLineageResult(
            found=False,
            warnings=["relationship_not_found"],
        )

    warnings: list[str] = []
    provenance = relationship.metadata.provenance
    if provenance is None:
        return RelationshipLineageResult(
            found=True,
            relationship=relationship,
            warnings=["missing_provenance"],
        )

    group_id = provenance_group_id(provenance)
    if group_id is None:
        warnings.append("non_group_provenance")
        return RelationshipLineageResult(
            found=True,
            relationship=relationship,
            provenance=dump_provenance(provenance),
            warnings=warnings,
        )

    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(group_id)
        if group is None:
            warnings.append("missing_group")
            return RelationshipLineageResult(
                found=True,
                relationship=relationship,
                provenance=dump_provenance(provenance),
                warnings=warnings,
            )
        resolution = (
            group_store.get_resolution(group.resolution_id)
            if group.resolution_id is not None
            else None
        )
        return RelationshipLineageResult(
            found=True,
            relationship=relationship,
            provenance=dump_provenance(provenance),
            group=group,
            resolution=resolution,
            source_workflow_receipt_id=group.source_workflow_receipt_id,
            source_trace_ids=list(group.source_trace_ids),
            warnings=warnings,
        )
    finally:
        group_store.close()


def service_get_receipt(
    instance: InstanceProtocol,
    receipt_id: str,
) -> Receipt:
    """Retrieve a stored receipt by ID.

    Raises ReceiptNotFoundError if not found.
    """
    store = instance.get_receipt_store()
    try:
        receipt = store.get_receipt(receipt_id)
    finally:
        store.close()
    if receipt is None:
        raise ReceiptNotFoundError(receipt_id)
    return receipt


def service_get_trace(instance: InstanceProtocol, trace_id: str) -> ExecutionTrace:
    """Retrieve a stored provider execution trace by ID.

    Raises TraceNotFoundError if not found.
    """
    store = instance.get_receipt_store()
    try:
        trace = store.get_trace(trace_id)
    finally:
        store.close()
    if trace is None:
        raise TraceNotFoundError(trace_id)
    return trace


def service_list_traces(
    instance: InstanceProtocol,
    *,
    workflow_name: str | None = None,
    provider_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> TraceListResult:
    """List stored provider execution trace summaries."""
    if limit < 1:
        raise ConfigError("limit must be at least 1")
    if offset < 0:
        raise ConfigError("offset must be non-negative")
    store = instance.get_receipt_store()
    try:
        traces = store.list_traces(
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        )
    finally:
        store.close()
    return TraceListResult(items=traces, total=len(traces))


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def service_list(
    instance: InstanceProtocol,
    resource: Literal["entities", "edges", "receipts", "feedback", "outcomes"],
    *,
    entity_type: str | None = None,
    relationship_type: str | None = None,
    query_name: str | None = None,
    receipt_id: str | None = None,
    property_filter: dict[str, Any] | None = None,
    operation_type: str | None = None,
    limit: int = 50,
) -> ListResult:
    """List entities, edges, receipts, feedback, or outcomes."""
    _VALID_RESOURCES = ("entities", "edges", "receipts", "feedback", "outcomes")
    if resource not in _VALID_RESOURCES:
        raise ConfigError(f"Unknown resource '{resource}'. Use: {', '.join(_VALID_RESOURCES)}")

    if property_filter is not None and resource not in ("entities", "edges"):
        raise ConfigError("property_filter is only supported for entities and edges")

    if resource == "entities":
        if not entity_type:
            raise ConfigError("entity_type is required when listing entities")
        config = instance.load_config()
        graph = instance.load_graph()
        result = read_list_entities(
            graph,
            entity_type,
            config=config,
            property_filter=property_filter,
            limit=limit,
        )
        return ListResult(items=result.items, total=result.total)

    if resource == "edges":
        graph = instance.load_graph()
        result = read_list_relationships(
            graph,
            relationship_type=relationship_type,
            property_filter=property_filter,
            limit=limit,
        )
        return ListResult(items=result.items, total=result.total)

    if resource == "receipts":
        store = instance.get_receipt_store()
        try:
            summaries = store.list_receipts(
                query_name=query_name, operation_type=operation_type, limit=limit
            )
            total = store.count_receipts(query_name=query_name, operation_type=operation_type)
        finally:
            store.close()
        return ListResult(items=summaries, total=total)

    if resource == "feedback":
        feedback_store = instance.get_feedback_store()
        try:
            feedback_records = feedback_store.list_feedback(receipt_id=receipt_id, limit=limit)
            total = feedback_store.count_feedback(receipt_id=receipt_id)
        finally:
            feedback_store.close()
        return ListResult(items=feedback_records, total=total)

    # outcomes
    feedback_store = instance.get_feedback_store()
    try:
        outcome_records = feedback_store.list_outcomes(receipt_id=receipt_id, limit=limit)
        total = feedback_store.count_outcomes(receipt_id=receipt_id)
    finally:
        feedback_store.close()
    return ListResult(items=outcome_records, total=total)


def _query_param_hints(
    config: CoreConfig,
    graph: Any,
    query_name: str,
) -> QueryParamHints | None:
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    return _query_param_hints_for_schema(config, graph, query_schema)


def _query_param_hints_for_schema(
    config: CoreConfig,
    graph: Any,
    query_schema: NamedQuerySchema,
) -> QueryParamHints:
    if query_schema.entry_point is None:
        required_params = _infer_query_required_params(query_schema, primary_key=None)
        return QueryParamHints(
            entry_point=None,
            required_params=required_params,
            primary_key=None,
            example_ids=[],
        )
    entity_schema = config.get_entity_type(query_schema.entry_point)
    primary_key = entity_schema.get_primary_key() if entity_schema is not None else None
    required_params = _infer_query_required_params(query_schema, primary_key=primary_key)
    example_ids: list[str] = []
    if primary_key is not None:
        example_ids = sorted(
            entity.entity_id for entity in graph.list_entities(query_schema.entry_point)
        )[:3]
    return QueryParamHints(
        entry_point=query_schema.entry_point,
        required_params=required_params,
        primary_key=primary_key,
        example_ids=example_ids,
    )


def _infer_query_required_params(
    query_schema: Any,
    *,
    primary_key: str | None,
) -> list[str]:
    params: set[str] = set()
    if primary_key is not None:
        params.add(primary_key)
    if query_schema.where is not None:
        params.update(_input_params_from_value(query_schema.where.root))
    if query_schema.select is not None:
        params.update(_input_params_from_value(query_schema.select))
    for order in query_schema.order_by:
        params.update(_input_params_from_value(order.by))
    for include in query_schema.include.values():
        params.update(_input_params_from_value(include.from_))
        if include.where is not None:
            params.update(_input_params_from_value(include.where.root))
        for related in [*include.where_related, *include.where_not_related]:
            params.update(
                _input_params_from_value(related.model_dump(mode="python", exclude_none=True))
            )
        for order in include.order_by:
            params.update(_input_params_from_value(order.by))
    for step in query_schema.traversal:
        if step.where is not None:
            params.update(_input_params_from_value(step.where.root))
        for related in [*step.where_related, *step.where_not_related]:
            params.update(
                _input_params_from_value(related.model_dump(mode="python", exclude_none=True))
            )
        if step.constraint:
            params.update(_input_params_from_constraint(step.constraint))
    return sorted(params)


def _input_params_from_value(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_INPUT_REF_RE.findall(value))
    if isinstance(value, list | tuple | set | frozenset):
        params: set[str] = set()
        for item in value:
            params.update(_input_params_from_value(item))
        return params
    if isinstance(value, dict):
        mapping_params: set[str] = set()
        for key, item in value.items():
            mapping_params.update(_input_params_from_value(key))
            mapping_params.update(_input_params_from_value(item))
        return mapping_params
    return set()


def _input_params_from_constraint(constraint: str) -> set[str]:
    params: set[str] = set()
    for name, dotted_name in _CONSTRAINT_PARAM_RE.findall(constraint):
        if name == "input" and dotted_name:
            params.add(dotted_name)
            continue
        if name not in {"entry", "result", "path", "relationship", "from_entity", "to_entity"}:
            params.add(name)
    return params


def _query_definition(
    config: CoreConfig,
    graph: Any,
    query_name: str,
) -> QueryDefinitionServiceResult:
    query_schema = config.named_queries[query_name]
    hints = _query_param_hints(config, graph, query_name)
    return QueryDefinitionServiceResult(
        name=query_name,
        mode=query_schema.mode,
        entry_point=query_schema.entry_point,
        required_params=list(hints.required_params) if hints is not None else [],
        returns=query_schema.returns,
        result_shape=query_schema.result_shape,
        dedupe=query_schema.dedupe,
        relationship_state=query_schema.relationship_state,
        allow_relationship_state_override=query_schema.allow_relationship_state_override,
        select=query_schema.select,
        order_by=[
            order.model_dump(mode="json", exclude_none=True) for order in query_schema.order_by
        ],
        include={
            alias: include.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            for alias, include in query_schema.include.items()
        },
        limit=query_schema.limit,
        max_paths=query_schema.max_paths,
        max_paths_per_result=query_schema.max_paths_per_result,
        description=query_schema.description,
        example_ids=list(hints.example_ids) if hints is not None else [],
    )


def _apply_response_limit(
    result: QueryServiceResult,
    *,
    surface_limit: int | None,
) -> tuple[list[Any], int | None, bool, bool]:
    visible = result.items
    response_truncated = False
    if surface_limit is not None and len(result.items) > surface_limit:
        visible = result.items[:surface_limit]
        response_truncated = True
    query_limit = result.limit
    limits = [value for value in (query_limit, surface_limit) if value is not None]
    effective_limit = min(limits) if limits else None
    return visible, effective_limit, result.truncated or response_truncated, response_truncated


def _query_result_with_response_limit(
    result: QueryServiceResult,
    *,
    surface_limit: int | None,
) -> QueryServiceResult:
    visible, effective_limit, truncated, response_truncated = _apply_response_limit(
        result,
        surface_limit=surface_limit,
    )
    return replace(
        result,
        items=visible,
        limit=effective_limit,
        truncated=truncated,
        limit_truncated=result.limit_truncated or response_truncated,
        truncation_reasons=_merge_truncation_reasons(
            result.truncation_reasons,
            "response_limit" if response_truncated else None,
        ),
    )


def _merge_truncation_reasons(
    reasons: list[str],
    extra: str | None,
) -> list[str]:
    merged = list(reasons)
    if extra is not None and extra not in merged:
        merged.append(extra)
    return merged
