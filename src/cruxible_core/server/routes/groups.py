"""Candidate group routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    ProposeGroupRequest,
    ResolveGroupRequest,
    UpdateTrustStatusRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["groups"])


@router.post("/{instance_id}/groups/propose", response_model=contracts.ProposeGroupToolResult)
async def propose_group(
    instance_id: str,
    req: ProposeGroupRequest,
) -> contracts.ProposeGroupToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.propose_group(
        instance_id=resolved_instance_id,
        relationship_type=req.relationship_type,
        members=req.members,
        thesis_text=req.thesis_text,
        thesis_facts=req.thesis_facts,
        analysis_state=req.analysis_state,
        signal_sources_used=req.signal_sources_used,
        proposed_by=req.proposed_by,
        suggested_priority=req.suggested_priority,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/groups/{group_id}/resolve",
    response_model=contracts.ResolveGroupToolResult,
)
async def resolve_group(
    instance_id: str,
    group_id: str,
    req: ResolveGroupRequest,
) -> contracts.ResolveGroupToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.resolve_group(
        instance_id=resolved_instance_id,
        group_id=group_id,
        action=req.action,
        rationale=req.rationale,
        resolved_by=req.resolved_by,
        expected_pending_version=req.expected_pending_version,
        actor_context=req.actor_context,
    )


@router.patch(
    "/{instance_id}/resolutions/{resolution_id}/trust",
    response_model=contracts.UpdateTrustStatusToolResult,
)
async def update_trust_status(
    instance_id: str,
    resolution_id: str,
    req: UpdateTrustStatusRequest,
) -> contracts.UpdateTrustStatusToolResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.update_trust_status(
        instance_id=resolved_instance_id,
        resolution_id=resolution_id,
        trust_status=req.trust_status,
        reason=req.reason,
        actor_context=req.actor_context,
    )
