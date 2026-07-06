"""Read/query routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request

from cruxible_client import contracts
from cruxible_core.errors import ConfigError
from cruxible_core.receipt.types import Receipt
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    EvaluateRequest,
    InlineQueryRequest,
    LintRequest,
    QueryRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["queries"])

# Query-string keys owned by the view surface itself; everything else is
# forwarded to the named query as a string-valued parameter.
VIEW_RESERVED_QUERY_KEYS = frozenset({"limit", "offset", "relationship_state"})


def _parse_property_filter(property_filter: str | None) -> dict[str, Any] | None:
    if property_filter is None:
        return None
    try:
        parsed = json.loads(property_filter)
    except json.JSONDecodeError as exc:
        raise ConfigError("property_filter must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("property_filter must decode to a JSON object")
    return parsed


def _parse_where_filter(where: str | None) -> dict[str, dict[str, Any]] | None:
    if where is None:
        return None
    try:
        parsed = json.loads(where)
    except json.JSONDecodeError as exc:
        raise ConfigError("where must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("where must decode to a JSON object")
    return parsed


@router.post("/{instance_id}/queries/run", response_model=contracts.QueryToolResult)
async def query(instance_id: str, req: QueryRequest) -> contracts.QueryToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.query(
        instance_id=resolved_instance_id,
        query_name=req.query_name,
        params=req.params,
        limit=req.limit,
        offset=req.offset,
        relationship_state=req.relationship_state,
        decision_record_id=req.decision_record_id,
        surface="http",
    )


@router.get("/{instance_id}/views/{query_name}", response_model=contracts.QueryToolResult)
async def view(
    instance_id: str,
    query_name: str,
    request: Request,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    relationship_state: contracts.QueryVisibilityState | None = None,
) -> contracts.QueryToolResult:
    """GET shim over named-query execution for read-model consumers.

    Non-reserved query-string keys are forwarded to the named query as
    string-valued parameters; results use the standard list envelope with
    deterministic ordering, so ``offset`` windows are stable per snapshot.
    Use GET ``/api/v1/{instance_id}/queries/{query_name}`` (describe_query)
    to inspect per-query parameter metadata such as required parameters,
    primary key hints, and example IDs.
    """
    resolved_instance_id = resolve_server_instance_id(instance_id)
    params = {
        key: value
        for key, value in request.query_params.items()
        if key not in VIEW_RESERVED_QUERY_KEYS
    }
    return api.query(
        instance_id=resolved_instance_id,
        query_name=query_name,
        params=params,
        limit=limit,
        offset=offset,
        relationship_state=relationship_state,
        surface="http",
    )


@router.post("/{instance_id}/queries/run-inline", response_model=contracts.QueryToolResult)
async def query_inline(
    instance_id: str,
    req: InlineQueryRequest,
) -> contracts.QueryToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.query_inline(
        instance_id=resolved_instance_id,
        definition=req.definition,
        params=req.params,
        limit=req.limit,
        relationship_state=req.relationship_state,
        decision_record_id=req.decision_record_id,
        surface="http",
    )


@router.get("/{instance_id}/receipts/{receipt_id}", response_model=Receipt)
async def receipt(instance_id: str, receipt_id: str) -> dict[str, Any]:
    return api.receipt(
        instance_id=resolve_server_instance_id(instance_id),
        receipt_id=receipt_id,
    )


@router.get(
    "/{instance_id}/receipts/{receipt_id}/explain",
    response_model=contracts.ReceiptExplanationResult,
)
async def explain_receipt(
    instance_id: str,
    receipt_id: str,
    format: contracts.ReceiptExplanationFormat = "markdown",
) -> contracts.ReceiptExplanationResult:
    return api.explain_receipt(
        instance_id=resolve_server_instance_id(instance_id),
        receipt_id=receipt_id,
        format=format,
    )


@router.get("/{instance_id}/traces/{trace_id}")
async def get_trace(instance_id: str, trace_id: str) -> dict[str, Any]:
    return api.get_trace(
        instance_id=resolve_server_instance_id(instance_id),
        trace_id=trace_id,
    )


@router.get("/{instance_id}/traces", response_model=contracts.TraceListResult)
async def list_traces(
    instance_id: str,
    workflow_name: str | None = None,
    provider_name: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> contracts.TraceListResult:
    return api.list_traces(
        instance_id=resolve_server_instance_id(instance_id),
        workflow_name=workflow_name,
        provider_name=provider_name,
        limit=limit,
        offset=offset,
    )


@router.get("/{instance_id}/list/{resource_type}", response_model=contracts.ListResult)
async def list_resources(
    instance_id: str,
    resource_type: contracts.ResourceType,
    entity_type: str | None = None,
    relationship_type: str | None = None,
    query_name: str | None = None,
    receipt_id: str | None = None,
    limit: int = 50,
    offset: int = Query(default=0, ge=0),
    property_filter: str | None = None,
    where: str | None = None,
    operation_type: str | None = None,
    fields: list[str] | None = Query(default=None),
    relationship_state: contracts.QueryVisibilityState | None = None,
) -> contracts.ListResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_resources(
        instance_id=resolved_instance_id,
        resource_type=resource_type,
        entity_type=entity_type,
        relationship_type=relationship_type,
        query_name=query_name,
        receipt_id=receipt_id,
        limit=limit,
        offset=offset,
        property_filter=_parse_property_filter(property_filter),
        where=_parse_where_filter(where),
        operation_type=operation_type,
        fields=fields,
        relationship_state=relationship_state,
    )


@router.get("/{instance_id}/schema")
async def schema(instance_id: str) -> dict[str, Any]:
    return api.schema(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/config/status", response_model=contracts.ConfigStatusResult)
async def config_status(instance_id: str) -> contracts.ConfigStatusResult:
    return api.config_status(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/queries", response_model=contracts.QueryListResult)
async def list_queries(
    instance_id: str,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.QueryListResult:
    return api.list_queries(
        resolve_server_instance_id(instance_id),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/queries/{query_name}",
    response_model=contracts.NamedQueryInfoResult,
)
async def describe_query(
    instance_id: str,
    query_name: str,
) -> contracts.NamedQueryInfoResult:
    return api.describe_query(
        resolve_server_instance_id(instance_id),
        query_name,
    )


@router.get("/{instance_id}/stats", response_model=contracts.StatsResult)
async def stats(instance_id: str) -> contracts.StatsResult:
    return api.stats(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/sample/{entity_type}", response_model=contracts.SampleResult)
async def sample(
    instance_id: str,
    entity_type: str,
    limit: int = 5,
    fields: list[str] | None = Query(default=None),
) -> contracts.SampleResult:
    return api.sample(
        resolve_server_instance_id(instance_id),
        entity_type,
        limit=limit,
        fields=fields,
    )


@router.post("/{instance_id}/evaluate", response_model=contracts.EvaluateResult)
async def evaluate(instance_id: str, req: EvaluateRequest) -> contracts.EvaluateResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.evaluate(
        instance_id=resolved_instance_id,
        max_findings=req.max_findings,
        exclude_orphan_types=req.exclude_orphan_types,
        severity_filter=req.severity_filter,
        category_filter=req.category_filter,
    )


@router.post("/{instance_id}/lint", response_model=contracts.LintResult)
async def lint(instance_id: str, req: LintRequest) -> contracts.LintResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.lint(
        instance_id=resolved_instance_id,
        max_findings=req.max_findings,
        analysis_limit=req.analysis_limit,
        min_support=req.min_support,
        exclude_orphan_types=req.exclude_orphan_types,
    )


@router.get(
    "/{instance_id}/entities/{entity_type}/{entity_id}",
    response_model=contracts.GetEntityResult,
)
async def get_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
) -> contracts.GetEntityResult:
    return api.get_entity(
        resolve_server_instance_id(instance_id),
        entity_type,
        entity_id,
    )


@router.get(
    "/{instance_id}/inspect/entity/{entity_type}/{entity_id}",
    response_model=contracts.InspectEntityResult,
)
async def inspect_entity(
    instance_id: str,
    entity_type: str,
    entity_id: str,
    direction: str = Query("both"),
    relationship_type: str | None = None,
    limit: int | None = None,
) -> contracts.InspectEntityResult:
    return api.inspect_entity(
        resolve_server_instance_id(instance_id),
        entity_type,
        entity_id,
        direction=direction,
        relationship_type=relationship_type,
        limit=limit,
    )


@router.get(
    "/{instance_id}/inspect/entity-history/{entity_type}",
    response_model=contracts.EntityChangeHistoryResult,
)
async def inspect_entity_history(
    instance_id: str,
    entity_type: str,
    entity_id: str | None = None,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
) -> contracts.EntityChangeHistoryResult:
    return api.inspect_entity_history(
        resolve_server_instance_id(instance_id),
        entity_type,
        entity_id=entity_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/inspect/{view}",
    response_model=contracts.CanonicalViewResult,
)
async def inspect_view(
    instance_id: str,
    view: str,
    limit: int = Query(200),
) -> contracts.CanonicalViewResult:
    return api.inspect_view(
        resolve_server_instance_id(instance_id),
        view,
        limit=limit,
    )


@router.get(
    "/{instance_id}/relationships/lineage",
    response_model=contracts.RelationshipLineageResult,
)
async def get_relationship_lineage(
    instance_id: str,
    from_type: str = Query(...),
    from_id: str = Query(...),
    relationship_type: str = Query(...),
    to_type: str = Query(...),
    to_id: str = Query(...),
    edge_key: int | None = None,
) -> contracts.RelationshipLineageResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.get_relationship_lineage(
        instance_id=resolved_instance_id,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )


@router.get(
    "/{instance_id}/relationships/lookup",
    response_model=contracts.GetRelationshipResult,
)
async def get_relationship(
    instance_id: str,
    from_type: str = Query(...),
    from_id: str = Query(...),
    relationship_type: str = Query(...),
    to_type: str = Query(...),
    to_id: str = Query(...),
    edge_key: int | None = None,
) -> contracts.GetRelationshipResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.get_relationship(
        instance_id=resolved_instance_id,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )


@router.get("/{instance_id}/groups/{group_id}", response_model=contracts.GetGroupToolResult)
async def get_group(instance_id: str, group_id: str) -> contracts.GetGroupToolResult:
    return api.get_group(resolve_server_instance_id(instance_id), group_id)


@router.get(
    "/{instance_id}/groups/{group_id}/status",
    response_model=contracts.GroupBucketStatusToolResult,
)
async def get_group_status_by_group(
    instance_id: str,
    group_id: str,
) -> contracts.GroupBucketStatusToolResult:
    return api.get_group_status(
        resolve_server_instance_id(instance_id),
        group_id=group_id,
    )


@router.get(
    "/{instance_id}/group-status/{signature}",
    response_model=contracts.GroupBucketStatusToolResult,
)
async def get_group_status_by_signature(
    instance_id: str,
    signature: str,
) -> contracts.GroupBucketStatusToolResult:
    return api.get_group_status(
        resolve_server_instance_id(instance_id),
        signature=signature,
    )


@router.get("/{instance_id}/groups", response_model=contracts.ListGroupsToolResult)
async def list_groups(
    instance_id: str,
    relationship_type: str | None = None,
    status: contracts.GroupStatus | None = None,
    limit: int = 50,
    offset: int = Query(default=0, ge=0),
) -> contracts.ListGroupsToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_groups(
        resolved_instance_id,
        relationship_type=relationship_type,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/{instance_id}/resolutions", response_model=contracts.ListResolutionsToolResult)
async def list_resolutions(
    instance_id: str,
    relationship_type: str | None = None,
    action: contracts.GroupAction | None = None,
    limit: int = 50,
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResolutionsToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_resolutions(
        resolved_instance_id,
        relationship_type=relationship_type,
        action=action,
        limit=limit,
        offset=offset,
    )
