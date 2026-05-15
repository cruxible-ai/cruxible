"""Read/query routes."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.errors import ConfigError
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    EvaluateRequest,
    LintRequest,
    QueryRequest,
    RenderWikiRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["queries"])


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


@router.post("/{instance_id}/query", response_model=contracts.QueryToolResult)
async def query(instance_id: str, req: QueryRequest) -> contracts.QueryToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.query(
        instance_id=resolved_instance_id,
        query_name=req.query_name,
        params=req.params,
        limit=req.limit,
        decision_record_id=req.decision_record_id,
        surface="http",
    )


@router.post("/{instance_id}/wiki/render", response_model=contracts.WikiRenderResult)
async def render_wiki(
    instance_id: str,
    req: RenderWikiRequest,
) -> contracts.WikiRenderResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.render_wiki(
        instance_id=resolved_instance_id,
        focus=req.focus,
        include_types=req.include_types,
        scope=req.scope,
        max_per_type=req.max_per_type,
        all_subjects=req.all_subjects,
    )


@router.get("/{instance_id}/receipts/{receipt_id}")
async def receipt(instance_id: str, receipt_id: str) -> dict[str, Any]:
    return api.receipt(
        instance_id=resolve_server_instance_id(instance_id),
        receipt_id=receipt_id,
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
    property_filter: str | None = None,
    operation_type: str | None = None,
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
        property_filter=_parse_property_filter(property_filter),
        operation_type=operation_type,
    )


@router.get("/{instance_id}/schema")
async def schema(instance_id: str) -> dict[str, Any]:
    return api.schema(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/queries", response_model=contracts.QueryListResult)
async def list_queries(instance_id: str) -> contracts.QueryListResult:
    return api.list_queries(resolve_server_instance_id(instance_id))


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
async def sample(instance_id: str, entity_type: str, limit: int = 5) -> contracts.SampleResult:
    return api.sample(
        resolve_server_instance_id(instance_id),
        entity_type,
        limit=limit,
    )


@router.post("/{instance_id}/evaluate", response_model=contracts.EvaluateResult)
async def evaluate(instance_id: str, req: EvaluateRequest) -> contracts.EvaluateResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.evaluate(
        instance_id=resolved_instance_id,
        max_findings=req.max_findings,
        exclude_orphan_types=req.exclude_orphan_types,
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
) -> contracts.ListGroupsToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_groups(
        resolved_instance_id,
        relationship_type=relationship_type,
        status=status,
        limit=limit,
    )


@router.get("/{instance_id}/resolutions", response_model=contracts.ListResolutionsToolResult)
async def list_resolutions(
    instance_id: str,
    relationship_type: str | None = None,
    action: contracts.GroupAction | None = None,
    limit: int = 50,
) -> contracts.ListResolutionsToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_resolutions(
        resolved_instance_id,
        relationship_type=relationship_type,
        action=action,
        limit=limit,
    )
