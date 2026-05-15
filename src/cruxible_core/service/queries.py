"""Query and read service functions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    ConfigError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    TraceNotFoundError,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
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
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.decisions import record_decision_event_for_context
from cruxible_core.service.types import (
    InspectEntityResult,
    InspectNeighborResult,
    ListResult,
    OperationContext,
    QueryDefinitionServiceResult,
    QueryParamHints,
    QueryServiceResult,
    QuerySurfaceServiceResult,
    RelationshipLineageResult,
    StatsServiceResult,
    TraceListResult,
)

# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def service_query(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    context: OperationContext | None = None,
) -> QueryServiceResult:
    """Execute a named query and persist the receipt.

    Returns results, receipt, and execution metadata.
    """
    started_at = datetime.now(timezone.utc)
    input_event = {"query_name": query_name, "params": params}
    try:
        config = instance.load_config()
        graph = instance.load_graph()
        query_result = read_run_query(config, graph, query_name, params)
        head_snapshot_id = instance.get_head_snapshot_id()

        if query_result.receipt:
            query_result.receipt.head_snapshot_id = head_snapshot_id
            store = instance.get_receipt_store()
            try:
                store.save_receipt(query_result.receipt)
            finally:
                store.close()

        total = query_result.total_results or len(query_result.results)
        result = QueryServiceResult(
            results=query_result.results,
            receipt_id=query_result.receipt.receipt_id if query_result.receipt else None,
            receipt=query_result.receipt,
            total_results=total,
            steps_executed=query_result.steps_executed,
            param_hints=_query_param_hints(config, graph, query_name),
            policy_summary=query_result.policy_summary,
        )
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
        output_payload={
            "results": [entity.model_dump(mode="json") for entity in result.results],
            "total_results": result.total_results,
            "steps_executed": result.steps_executed,
        },
        receipt_id=result.receipt_id,
        head_snapshot_id=receipt_head_snapshot_id,
        started_at=started_at,
    )
    return result


def service_query_surface(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    limit: int | None = None,
    context: OperationContext | None = None,
) -> QuerySurfaceServiceResult:
    """Execute a named query and apply caller-facing result truncation."""
    if limit is not None and limit < 1:
        raise ConfigError("limit must be a positive integer")

    result = service_query(instance, query_name, params, context=context)
    truncated = limit is not None and result.total_results > limit
    visible = result.results[:limit] if truncated else result.results
    return QuerySurfaceServiceResult(
        results=visible,
        receipt_id=result.receipt_id,
        receipt=result.receipt,
        total_results=result.total_results,
        truncated=truncated,
        steps_executed=result.steps_executed,
        param_hints=result.param_hints,
        policy_summary=result.policy_summary,
    )


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
        neighbors=[
            InspectNeighborResult(
                direction=neighbor.direction,
                relationship_type=neighbor.relationship_type,
                edge_key=neighbor.edge_key,
                properties=neighbor.properties,
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
    provenance = relationship.properties.get("_provenance")
    if not isinstance(provenance, dict):
        return RelationshipLineageResult(
            found=True,
            relationship=relationship,
            warnings=["missing_provenance"],
        )

    source_ref = provenance.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.startswith("group:"):
        warnings.append("non_group_provenance")
        return RelationshipLineageResult(
            found=True,
            relationship=relationship,
            provenance=dict(provenance),
            warnings=warnings,
        )

    group_id = source_ref.removeprefix("group:")
    group_store = instance.get_group_store()
    try:
        group = group_store.get_group(group_id)
        if group is None:
            warnings.append("missing_group")
            return RelationshipLineageResult(
                found=True,
                relationship=relationship,
                provenance=dict(provenance),
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
            provenance=dict(provenance),
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


def service_get_trace(
    instance: InstanceProtocol,
    trace_id: str,
) -> ExecutionTrace:
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
    return TraceListResult(traces=traces, count=len(traces))


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
    entity_schema = config.get_entity_type(query_schema.entry_point)
    primary_key = entity_schema.get_primary_key() if entity_schema is not None else None
    required_params = [primary_key] if primary_key is not None else []
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


def _query_definition(
    config: CoreConfig,
    graph: Any,
    query_name: str,
) -> QueryDefinitionServiceResult:
    query_schema = config.named_queries[query_name]
    hints = _query_param_hints(config, graph, query_name)
    return QueryDefinitionServiceResult(
        name=query_name,
        entry_point=query_schema.entry_point,
        required_params=list(hints.required_params) if hints is not None else [],
        returns=query_schema.returns,
        description=query_schema.description,
        example_ids=list(hints.example_ids) if hints is not None else [],
    )
