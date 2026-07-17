"""Query and read service functions."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from typing import Any, Literal, cast

import structlog
from pydantic import ValidationError

from cruxible_core.config.property_validation import entity_with_identity_properties
from cruxible_core.config.schema import CoreConfig, NamedQuerySchema, QueryPredicateSpec
from cruxible_core.errors import (
    ConfigError,
    EntityTypeNotFoundError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    RelationshipNotFoundError,
    TraceNotFoundError,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import (
    dump_provenance,
    provenance_group_id,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, RelationshipMetadata
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.query.engine import execute_query_definition
from cruxible_core.query.entity_state import resolve_entity_visibility_state
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.query.predicates import (
    build_predicate_context,
    evaluate_query_predicates,
    validate_edge_where_property_fields,
)
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
    project_entity_fields,
    relationship_sort_key,
    validate_entity_projection_fields,
)
from cruxible_core.query.read_surface import (
    run_query as read_run_query,
)
from cruxible_core.query.read_surface import (
    sample_entities as read_sample_entities,
)
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.query.types import QueryPathSegment, dump_query_row
from cruxible_core.receipt.types import Receipt
from cruxible_core.service.decisions import record_decision_event_for_context
from cruxible_core.service.types import (
    EntityChangeHistoryItem,
    EntityChangeHistoryResult,
    InspectEntityResult,
    InspectNeighborResult,
    ListResult,
    NeighborDirection,
    OperationContext,
    PropertyChangeItem,
    QueryDefinitionServiceResult,
    QueryParamHints,
    QueryServiceResult,
    RelationshipLineageResult,
    StatsServiceResult,
    TraceListResult,
)
from cruxible_core.temporal import utc_now

logger = structlog.get_logger("cruxible.service.reads")

_INPUT_REF_RE = re.compile(r"\$input\.([A-Za-z_][\w-]*)")
_CONSTRAINT_PARAM_RE = re.compile(r"\$([A-Za-z_][\w-]*)(?:\.([A-Za-z_][\w-]*))?")
_LIST_FIELD_RE = re.compile(r"^[A-Za-z_][\w-]*$")
_LIST_WHERE_OPERATORS = {"eq", "contains", "in"}
_INLINE_QUERY_PREFIX = "inline:"
_INLINE_DEFAULT_LIMIT = 50
_INLINE_MAX_LIMIT = 500
_INLINE_DEFAULT_MAX_PATHS = 1000
_INLINE_MAX_PATHS = 5000
_INLINE_DEFAULT_MAX_PATHS_PER_RESULT = 25
_INLINE_MAX_PATHS_PER_RESULT = 100
_ENTITY_HISTORY_RECEIPT_PAGE_SIZE = 500

# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def service_query(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryVisibilityState | None = None,
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
    relationship_state: QueryVisibilityState | None = None,
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
    offset: int = 0,
    relationship_state: QueryVisibilityState | None = None,
    context: OperationContext | None = None,
) -> QueryServiceResult:
    """Execute a named query and apply caller-facing result windowing."""
    surface_limit = limit
    if surface_limit is not None and surface_limit < 1:
        raise ConfigError("limit must be a positive integer")
    if offset < 0:
        raise ConfigError("offset must be a non-negative integer")

    result = service_query(
        instance,
        query_name,
        params,
        relationship_state=relationship_state,
        context=context,
    )
    return _query_result_with_response_limit(
        result,
        surface_limit=surface_limit,
        offset=offset,
        resource=f"query:{query_name}",
    )


def service_query_inline_surface(
    instance: InstanceProtocol,
    definition: Mapping[str, Any],
    params: dict[str, Any],
    *,
    limit: int | None = None,
    relationship_state: QueryVisibilityState | None = None,
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
    return _query_result_with_response_limit(
        result,
        surface_limit=surface_limit,
        resource=f"query_inline:{inline_name}",
    )


def service_evaluate_query_surface(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    limit: int | None = None,
    relationship_state: QueryVisibilityState | None = None,
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
    return _query_result_with_response_limit(
        result,
        surface_limit=surface_limit,
        resource=f"query:{query_name}",
    )


def _evaluate_query_result(
    instance: InstanceProtocol,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryVisibilityState | None = None,
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
    relationship_state: QueryVisibilityState | None = None,
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


def query_definition_summary_payload(
    definition: QueryDefinitionServiceResult,
) -> dict[str, Any]:
    """Bounded discovery card for one named query.

    The single summary serializer consumed by the runtime API, CLI local
    mode, and MCP. Key order mirrors ``contracts.QueryDefinitionSummary`` so
    local and remote JSON surfaces are byte-identical.
    """
    return {
        "name": definition.name,
        "description": definition.description,
        "mode": definition.mode,
        "entry_point": definition.entry_point,
        "returns": definition.returns,
        "result_shape": definition.result_shape,
        "required_params": list(definition.required_params),
        "allow_relationship_state_override": definition.allow_relationship_state_override,
    }


def query_definition_full_payload(
    definition: QueryDefinitionServiceResult,
) -> dict[str, Any]:
    """Full named-query definition payload (describe_query / detail=full).

    The single full serializer consumed by the runtime API, CLI local mode,
    and MCP. Derived mechanically from ``QueryDefinitionServiceResult`` so the
    field enumeration lives in exactly one place: ``asdict`` walks the dataclass
    fields in declaration order, and ``contracts.NamedQueryInfoResult`` declares
    the same fields in the same order (pinned by tests), so local and remote
    JSON surfaces stay byte-identical and a new definition field is added in
    one spot.
    """
    return asdict(definition)


def service_list_queries(
    instance: InstanceProtocol,
    *,
    include_examples: bool = True,
) -> list[QueryDefinitionServiceResult]:
    """Return named-query definitions with the invocation details agents need.

    ``include_examples=False`` skips the per-query entity-ID scan behind
    ``example_ids`` (leaving it empty); summary-only consumers use it because
    the summary payload never exposes example IDs.
    """
    config = instance.load_config()
    graph = instance.load_graph()
    definitions: list[QueryDefinitionServiceResult] = []
    for name in sorted(config.named_queries.keys()):
        definitions.append(
            _query_definition(config, graph, name, include_examples=include_examples)
        )
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
    fields: list[str] | None = None,
) -> list[EntityInstance]:
    """Sample entities of a given type."""
    config = instance.load_config()
    graph = instance.load_graph()
    return read_sample_entities(graph, entity_type, config=config, fields=fields, limit=limit)


def service_stats(instance: InstanceProtocol) -> StatsServiceResult:
    """Return graph counts grouped by entity and relationship type."""
    config = instance.load_config()
    graph = instance.load_graph()
    result = read_graph_stats(graph, head_snapshot_id=instance.get_head_snapshot_id())
    return StatsServiceResult(
        entity_count=result.entity_count,
        edge_count=result.edge_count,
        entity_counts=result.entity_counts,
        relationship_counts=result.relationship_counts,
        status_counts=_status_counts(config, graph),
        head_snapshot_id=result.head_snapshot_id,
    )


def _status_counts(
    config: CoreConfig,
    graph: Any,
) -> dict[str, dict[str, int]]:
    """Return per-entity status counts for schemas with enum-backed status."""
    counts: dict[str, dict[str, int]] = {}
    for entity_type, entity_schema in config.entity_types.items():
        status_property = entity_schema.properties.get("status")
        if status_property is None:
            continue
        values = _status_enum_values(config, status_property.enum, status_property.enum_ref)
        if values is None:
            continue
        entity_status_counts = dict.fromkeys(values, 0)
        for entity in graph.list_entities(entity_type):
            status = entity.properties.get("status")
            if isinstance(status, str) and status in entity_status_counts:
                entity_status_counts[status] += 1
        counts[entity_type] = entity_status_counts
    return counts


def _status_enum_values(
    config: CoreConfig,
    inline_enum: list[Any] | None,
    enum_ref: str | None,
) -> list[str] | None:
    if enum_ref is not None:
        enum_schema = config.enums.get(enum_ref)
        if enum_schema is None:
            return None
        return list(enum_schema.values)
    if inline_enum is None:
        return None
    return [value for value in inline_enum if isinstance(value, str)]


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


def service_get_entity_change_history(
    instance: InstanceProtocol,
    entity_type: str,
    *,
    entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> EntityChangeHistoryResult:
    """Return receipt-derived property changes for one entity type or entity."""
    if limit < 1:
        raise ConfigError("limit must be at least 1")
    if offset < 0:
        raise ConfigError("offset must be non-negative")

    config = instance.load_config()
    _validate_entity_history_entity_type(config, entity_type)

    store = instance.get_receipt_store()
    changes: list[EntityChangeHistoryItem] = []
    legacy_count = 0
    try:
        receipt_ids = (
            store.get_receipts_for_entity(entity_type, entity_id)
            if entity_id is not None
            else _all_receipt_ids(store)
        )
        for receipt_id in receipt_ids:
            receipt = store.get_receipt(receipt_id)
            if receipt is None:
                continue
            for node in receipt.nodes:
                if node.node_type != "entity_write":
                    continue
                if node.entity_type != entity_type:
                    continue
                if entity_id is not None and node.entity_id != entity_id:
                    continue
                detail = node.detail
                change_kind = detail.get("change_kind")
                raw_property_changes = detail.get("property_changes")
                if change_kind not in {"created", "updated"} or not isinstance(
                    raw_property_changes, list
                ):
                    legacy_count += 1
                    continue
                property_changes = _property_change_items(raw_property_changes)
                if change_kind == "updated" and not property_changes:
                    continue
                actor_context = detail.get("actor_context")
                changes.append(
                    EntityChangeHistoryItem(
                        entity_type=entity_type,
                        entity_id=node.entity_id or "",
                        change_kind=cast(Literal["created", "updated"], change_kind),
                        property_changes=property_changes,
                        changed_at=node.timestamp,
                        receipt_id=receipt.receipt_id,
                        operation_type=receipt.operation_type,
                        actor_context=actor_context if isinstance(actor_context, dict) else None,
                    )
                )
    finally:
        store.close()

    changes.sort(key=lambda item: (item.changed_at, item.receipt_id), reverse=True)
    total = len(changes)
    page = changes[offset : offset + limit]
    warnings = (
        [f"{legacy_count} legacy entity write(s) lacked property change detail"]
        if legacy_count
        else []
    )
    return EntityChangeHistoryResult(
        entity_type=entity_type,
        entity_id=entity_id,
        items=page,
        total=total,
        limit=limit,
        offset=offset,
        truncated=offset + len(page) < total,
        legacy_entity_write_count=legacy_count,
        warnings=warnings,
    )


def _validate_entity_history_entity_type(config: CoreConfig, entity_type: str) -> None:
    entity_schema = config.get_entity_type(entity_type)
    if entity_schema is None:
        raise EntityTypeNotFoundError(entity_type, known_entity_types=list(config.entity_types))


def _all_receipt_ids(store: Any) -> list[str]:
    receipt_ids: list[str] = []
    offset = 0
    while True:
        page = store.list_receipts(limit=_ENTITY_HISTORY_RECEIPT_PAGE_SIZE, offset=offset)
        if not page:
            break
        receipt_ids.extend(str(row["receipt_id"]) for row in page)
        if len(page) < _ENTITY_HISTORY_RECEIPT_PAGE_SIZE:
            break
        offset += _ENTITY_HISTORY_RECEIPT_PAGE_SIZE
    return receipt_ids


def _property_change_items(raw_changes: list[Any]) -> list[PropertyChangeItem]:
    changes: list[PropertyChangeItem] = []
    for raw_change in raw_changes:
        if not isinstance(raw_change, Mapping):
            continue
        property_name = raw_change.get("property")
        if not isinstance(property_name, str) or not property_name:
            continue
        changes.append(
            PropertyChangeItem(
                property=property_name,
                from_value=raw_change.get("from_value"),
                to_value=raw_change.get("to_value"),
            )
        )
    return changes


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
    config = instance.load_config()
    graph = instance.load_graph()
    return read_get_relationship(
        graph,
        config=config,
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
        total = store.count_traces(
            workflow_name=workflow_name,
            provider_name=provider_name,
        )
    finally:
        store.close()
    return TraceListResult(items=traces, total=total)


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
    where: Mapping[str, Mapping[str, Any]] | None = None,
    relationship_state: QueryVisibilityState | None = None,
    operation_type: str | None = None,
    fields: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ListResult:
    """List entities, edges, receipts, feedback, or outcomes.

    ``relationship_state`` is the unified read-visibility selector. For ENTITIES
    it gates by lifecycle through the shared :func:`entity_matches_query_state`
    filter (the query engine applies it); for EDGES it gates by review AND
    lifecycle through :func:`relationship_matches_query_state`. The same selector
    therefore agrees exactly with traversal/include visibility across surfaces.

    Defaults differ by resource to preserve each surface's contract: entities
    default to ``live`` (only live entities), while edges default to ``None`` --
    the stored-edge inspection contract returns every stored edge regardless of
    review/lifecycle state. Pass an explicit selector (``all`` / ``not-live`` /
    ...) to override either.
    """
    _VALID_RESOURCES = ("entities", "edges", "receipts", "feedback", "outcomes")
    if resource not in _VALID_RESOURCES:
        raise ConfigError(f"Unknown resource '{resource}'. Use: {', '.join(_VALID_RESOURCES)}")

    if property_filter is not None and resource not in ("entities", "edges"):
        raise ConfigError("property_filter is only supported for entities and edges")
    if where is not None and resource not in ("entities", "edges"):
        raise ConfigError("where is only supported for entities and edges")
    if property_filter is not None and where is not None:
        raise ConfigError("property_filter and where are mutually exclusive")
    if relationship_state is not None and resource not in ("entities", "edges"):
        raise ConfigError("state is only supported for entities and edges")
    if fields is not None and resource != "entities":
        raise ConfigError("fields is only supported for entities")

    if resource == "entities":
        if not entity_type:
            raise ConfigError("entity_type is required when listing entities")
        result = _service_list_entities(
            instance,
            entity_type=entity_type,
            property_filter=property_filter,
            where=where,
            relationship_state=relationship_state,
            fields=fields,
            limit=limit,
            offset=offset,
        )
    elif resource == "edges":
        result = _service_list_edges(
            instance,
            relationship_type=relationship_type,
            property_filter=property_filter,
            where=where,
            relationship_state=relationship_state,
            limit=limit,
            offset=offset,
        )
    elif resource == "receipts":
        store = instance.get_receipt_store()
        try:
            summaries = store.list_receipts(
                query_name=query_name,
                operation_type=operation_type,
                limit=limit,
                offset=offset,
            )
            total = store.count_receipts(query_name=query_name, operation_type=operation_type)
        finally:
            store.close()
        result = ListResult(items=summaries, total=total)
    elif resource == "feedback":
        feedback_store = instance.get_feedback_store()
        try:
            feedback_records = feedback_store.list_feedback(
                receipt_id=receipt_id,
                limit=limit,
                offset=offset,
            )
            total = feedback_store.count_feedback(receipt_id=receipt_id)
        finally:
            feedback_store.close()
        result = ListResult(items=feedback_records, total=total)
    else:  # outcomes
        feedback_store = instance.get_feedback_store()
        try:
            outcome_records = feedback_store.list_outcomes(
                receipt_id=receipt_id,
                limit=limit,
                offset=offset,
            )
            total = feedback_store.count_outcomes(receipt_id=receipt_id)
        finally:
            feedback_store.close()
        result = ListResult(items=outcome_records, total=total)

    _warn_on_dropped_read(
        resource=f"list:{resource}",
        total=result.total,
        returned=len(result.items),
        limit=limit,
        offset=offset,
    )
    return result


def _service_list_entities(
    instance: InstanceProtocol,
    *,
    entity_type: str,
    property_filter: Mapping[str, Any] | None,
    where: Mapping[str, Mapping[str, Any]] | None,
    relationship_state: QueryVisibilityState | None,
    fields: list[str] | None,
    limit: int,
    offset: int,
) -> ListResult:
    config = instance.load_config()
    _require_list_entity_type(config, entity_type)
    validate_entity_projection_fields(config, entity_type, fields)
    query_where = _compile_entity_list_where(
        config,
        entity_type,
        property_filter=property_filter,
        where=where,
    )
    # Default ``list entities`` to live-only; honor an explicit visibility
    # selector (``all`` / ``not-live`` / ...) when the caller provides one. The
    # synthetic query routes through the engine, so entity-lifecycle gating is
    # applied by the same chokepoint every other read path uses. Entities have no
    # review axis, so the review-only selectors collapse to ``live`` here (they
    # would otherwise trip the path-shape constraints those values carry).
    entity_state = resolve_entity_visibility_state(relationship_state or "live")
    query_schema = NamedQuerySchema(
        mode="collection",
        returns=entity_type,
        result_shape="entity",
        where=query_where,
        relationship_state=entity_state,
    )
    query_result = execute_query_definition(
        config,
        instance.load_graph(),
        "__list_entities__",
        query_schema,
        {},
    )
    entities = sorted(
        [cast(EntityInstance, row) for row in query_result.results],
        key=lambda entity: (entity.entity_type, entity.entity_id),
    )
    total = len(entities)
    page = _paginate_items(entities, limit=limit, offset=offset)
    if fields is not None:
        page = [project_entity_fields(config, entity, fields) for entity in page]
    return ListResult(items=page, total=total)


def _service_list_edges(
    instance: InstanceProtocol,
    *,
    relationship_type: str | None,
    property_filter: Mapping[str, Any] | None,
    where: Mapping[str, Mapping[str, Any]] | None,
    relationship_state: QueryVisibilityState | None,
    limit: int,
    offset: int,
) -> ListResult:
    config = instance.load_config()
    graph = instance.load_graph()
    if relationship_type is not None:
        _require_list_relationship_type(config, relationship_type)
        relationship_types = [relationship_type]
    else:
        relationship_types = [relationship.name for relationship in config.relationships]
    query_where = _compile_edge_list_where(
        config,
        relationship_type,
        property_filter=property_filter,
        where=where,
    )
    relationships = [
        _relationship_list_payload(relationship)
        for name in relationship_types
        for relationship in graph.iter_relationships(name)
        # Route review/lifecycle visibility through the single shared filter so a
        # filtered `list edges` view agrees with engine traversal/include paths;
        # `relationship_state is None` preserves the stored-edge inspection
        # contract (every stored edge is returned).
        if (
            relationship_state is None
            or relationship_matches_query_state(relationship.metadata, relationship_state)
        )
        if _relationship_matches_list_where(config, graph, relationship, query_where)
    ]
    relationships = sorted(relationships, key=relationship_sort_key)
    total = len(relationships)
    return ListResult(
        items=_paginate_items(relationships, limit=limit, offset=offset),
        total=total,
    )


def _paginate_items(items: list[Any], *, limit: int, offset: int) -> list[Any]:
    return items[offset : offset + limit]


def _compile_entity_list_where(
    config: CoreConfig,
    entity_type: str,
    *,
    property_filter: Mapping[str, Any] | None,
    where: Mapping[str, Mapping[str, Any]] | None,
) -> QueryPredicateSpec | None:
    normalized = _normalize_list_where(property_filter=property_filter, where=where)
    if not normalized:
        return None
    subject = f"entity type '{entity_type}'"
    _validate_list_where_field_syntax(normalized)
    known_fields = set(config.entity_types[entity_type].properties)
    _validate_list_where_known_fields(normalized, known_fields, subject=subject)
    return _query_predicate_from_list_where(normalized, scope="result")


def _compile_edge_list_where(
    config: CoreConfig,
    relationship_type: str | None,
    *,
    property_filter: Mapping[str, Any] | None,
    where: Mapping[str, Mapping[str, Any]] | None,
) -> QueryPredicateSpec | None:
    normalized = _normalize_list_where(property_filter=property_filter, where=where)
    if not normalized:
        return None
    subject = (
        f"relationship type '{relationship_type}'"
        if relationship_type is not None
        else "configured relationships"
    )
    _validate_list_where_field_syntax(normalized)
    # Shared with the inline relationship-collection query (query engine) so an
    # unconfigured edge property raises the same ConfigError on both surfaces.
    validate_edge_where_property_fields(
        config,
        relationship_type,
        normalized.keys(),
        subject=subject,
    )
    return _query_predicate_from_list_where(normalized, scope="edge")


def _normalize_list_where(
    *,
    property_filter: Mapping[str, Any] | None,
    where: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    if property_filter is not None and where is not None:
        raise ConfigError("property_filter and where are mutually exclusive")
    if property_filter is not None:
        return {str(field): {"eq": value} for field, value in property_filter.items()}
    if where is None:
        return None
    normalized: dict[str, dict[str, Any]] = {}
    for field, operators in where.items():
        if not isinstance(operators, Mapping) or not operators:
            raise ConfigError(f"where field '{field}' must define at least one operator")
        normalized[str(field)] = dict(operators)
    return normalized


def _validate_list_where_field_syntax(
    where: Mapping[str, Mapping[str, Any]],
) -> None:
    """Validate the list-surface field-name format and operator allowlist.

    Field membership against the configured schema is checked separately so the
    edge surface can share the engine's :func:`validate_edge_where_property_fields`.
    """
    for field, operators in where.items():
        if not _LIST_FIELD_RE.fullmatch(field):
            raise ConfigError(f"where field '{field}' must be a bare configured property name")
        unsupported = sorted(set(operators) - _LIST_WHERE_OPERATORS)
        if unsupported:
            allowed = ", ".join(sorted(_LIST_WHERE_OPERATORS))
            raise ConfigError(
                f"Unsupported where operator for field '{field}': {', '.join(unsupported)}. "
                f"Allowed: {allowed}"
            )


def _validate_list_where_known_fields(
    where: Mapping[str, Mapping[str, Any]],
    known_fields: set[str],
    *,
    subject: str,
) -> None:
    for field in where:
        if field not in known_fields:
            known = ", ".join(sorted(known_fields)) or "(none)"
            raise ConfigError(f"Unknown where field for {subject}: {field}. Known fields: {known}")


def _query_predicate_from_list_where(
    where: Mapping[str, Mapping[str, Any]],
    *,
    scope: Literal["result", "edge"],
) -> QueryPredicateSpec:
    try:
        return QueryPredicateSpec.model_validate(
            {f"{scope}.properties.{field}": dict(operators) for field, operators in where.items()}
        )
    except ValidationError as exc:
        raise ConfigError(f"Invalid where predicate: {exc}") from exc


def _require_list_entity_type(config: CoreConfig, entity_type: str) -> None:
    if entity_type not in config.entity_types:
        raise EntityTypeNotFoundError(
            entity_type,
            known_entity_types=sorted(config.entity_types),
        )


def _require_list_relationship_type(config: CoreConfig, relationship_type: str) -> None:
    if config.get_relationship(relationship_type) is None:
        raise RelationshipNotFoundError(relationship_type)


def _relationship_matches_list_where(
    config: CoreConfig,
    graph: EntityGraph,
    relationship: RelationshipInstance,
    where: QueryPredicateSpec | None,
) -> bool:
    if where is None:
        return True
    # `list edges` is a stored-relationship inspection surface (see
    # docs/cli-reference.md, docs/mcp-tools.md): a stored edge stays visible even
    # when an endpoint entity is missing, and the where-is-None path above keeps
    # it unconditionally. The where predicate is edge-scoped (edge.properties.*),
    # so endpoint entities are never read by the filter; synthesize placeholders
    # for any missing endpoint so a missing endpoint never silently drops an edge
    # that a property filter would otherwise match.
    source = graph.get_entity(relationship.from_type, relationship.from_id)
    target = graph.get_entity(relationship.to_type, relationship.to_id)
    source = (
        entity_with_identity_properties(config, source)
        if source is not None
        else EntityInstance(
            entity_type=relationship.from_type,
            entity_id=relationship.from_id,
        )
    )
    target = (
        entity_with_identity_properties(config, target)
        if target is not None
        else EntityInstance(
            entity_type=relationship.to_type,
            entity_id=relationship.to_id,
        )
    )
    segment = QueryPathSegment(
        relationship_type=relationship.relationship_type,
        from_type=relationship.from_type,
        from_id=relationship.from_id,
        to_type=relationship.to_type,
        to_id=relationship.to_id,
        edge_key=relationship.edge_key,
        properties=dict(relationship.properties),
        metadata=relationship.metadata,
    )
    context = replace(
        build_predicate_context(
            entry=source,
            current=source,
            candidate=target,
            segment=segment,
            path=(segment,),
            entities=(source, target),
        ),
        result=segment,
    )
    return evaluate_query_predicates(config, where, context, {})


def _relationship_list_payload(relationship: RelationshipInstance) -> dict[str, Any]:
    metadata = relationship.metadata
    if not isinstance(metadata, RelationshipMetadata):
        metadata = RelationshipMetadata.model_validate(metadata)
    return {
        "from_type": relationship.from_type,
        "from_id": relationship.from_id,
        "to_type": relationship.to_type,
        "to_id": relationship.to_id,
        "relationship_type": relationship.relationship_type,
        "edge_key": relationship.edge_key,
        "properties": dict(relationship.properties),
        "metadata": metadata.model_dump(mode="json", exclude_none=True),
    }


def _query_param_hints(
    config: CoreConfig,
    graph: Any,
    query_name: str,
    *,
    include_examples: bool = True,
) -> QueryParamHints | None:
    query_schema = config.named_queries.get(query_name)
    if query_schema is None:
        return None
    return _query_param_hints_for_schema(
        config,
        graph,
        query_schema,
        include_examples=include_examples,
    )


def _query_param_hints_for_schema(
    config: CoreConfig,
    graph: Any,
    query_schema: NamedQuerySchema,
    *,
    include_examples: bool = True,
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
    if include_examples and primary_key is not None:
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
    *,
    include_examples: bool = True,
) -> QueryDefinitionServiceResult:
    query_schema = config.named_queries[query_name]
    hints = _query_param_hints(config, graph, query_name, include_examples=include_examples)
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
    offset: int = 0,
) -> tuple[list[Any], int | None, bool, bool]:
    windowed = result.items[offset:] if offset else result.items
    visible = windowed
    response_truncated = False
    if surface_limit is not None and len(windowed) > surface_limit:
        visible = windowed[:surface_limit]
        response_truncated = True
    query_limit = result.limit
    limits = [value for value in (query_limit, surface_limit) if value is not None]
    effective_limit = min(limits) if limits else None
    return visible, effective_limit, result.truncated or response_truncated, response_truncated


def _query_result_with_response_limit(
    result: QueryServiceResult,
    *,
    surface_limit: int | None,
    offset: int = 0,
    resource: str = "query",
) -> QueryServiceResult:
    visible, effective_limit, truncated, response_truncated = _apply_response_limit(
        result,
        surface_limit=surface_limit,
        offset=offset,
    )
    final_reasons = _merge_truncation_reasons(
        result.truncation_reasons,
        "response_limit" if response_truncated else None,
    )
    _warn_on_dropped_read(
        resource=resource,
        total=result.total,
        returned=len(visible),
        limit=effective_limit,
        offset=offset,
        truncation_reasons=final_reasons,
    )
    return replace(
        result,
        items=visible,
        limit=effective_limit,
        offset=offset,
        truncated=truncated,
        limit_truncated=result.limit_truncated or response_truncated,
        truncation_reasons=final_reasons,
    )


def _merge_truncation_reasons(
    reasons: list[str],
    extra: str | None,
) -> list[str]:
    merged = list(reasons)
    if extra is not None and extra not in merged:
        merged.append(extra)
    return merged


def _warn_on_dropped_read(
    *,
    resource: str,
    total: int,
    returned: int,
    limit: int | None = None,
    offset: int = 0,
    truncation_reasons: Sequence[str] = (),
) -> None:
    """Emit a diagnostic warning when a read drops rows it should have returned.

    Read-pipeline drop detection (diagnostic invariant). When a result reports
    ``total > 0`` yet the window that should contain rows comes back empty and no
    truncation reason explains the shortfall, a consumer cannot distinguish a
    genuine empty page from a silent serialization/render drop. This guard makes
    such a drop loud and localized at the service result boundary so every
    consumer (CLI, client, MCP) benefits.

    Never raises: a diagnostic must never break a legitimate read. It stays
    silent for ``total == 0`` (legitimate empty), for normal/expected results,
    and when the empty window is explained by pagination (the window starts at or
    past ``total``) or by an existing truncation reason.
    """
    try:
        if total <= 0:
            return
        if returned > 0:
            return
        # The window genuinely contains no rows: legitimate empty page.
        if offset >= total:
            return
        # A truncation reason explains the (possibly empty) window.
        if truncation_reasons:
            return
        logger.warning(
            "read_pipeline_drop",
            resource=resource,
            total=total,
            returned=returned,
            limit=limit,
            offset=offset,
        )
    except Exception:  # pragma: no cover - diagnostics must never break a read
        return
