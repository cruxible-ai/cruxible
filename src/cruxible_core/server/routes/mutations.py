"""Mutation routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import local_api
from cruxible_core.server.request_models import (
    AddConstraintRequest,
    AddDecisionPolicyRequest,
    AddEntitiesRequest,
    AddRelationshipsRequest,
    ReloadConfigRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["mutations"])


@router.post("/{instance_id}/entities", response_model=contracts.AddEntityResult)
async def add_entities(
    instance_id: str,
    req: AddEntitiesRequest,
) -> contracts.AddEntityResult:
    return local_api.add_entities(
        instance_id=resolve_server_instance_id(instance_id),
        entities=req.entities,
    )


@router.post("/{instance_id}/relationships", response_model=contracts.AddRelationshipResult)
async def add_relationships(
    instance_id: str,
    req: AddRelationshipsRequest,
) -> contracts.AddRelationshipResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.add_relationships_with_provenance(
        instance_id=resolved_instance_id,
        relationships=req.relationships,
        provenance_source="http_api",
        provenance_source_ref="cruxible_add_relationship",
    )


@router.post("/{instance_id}/constraints", response_model=contracts.AddConstraintResult)
async def add_constraint(
    instance_id: str,
    req: AddConstraintRequest,
) -> contracts.AddConstraintResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.add_constraint(
        instance_id=resolved_instance_id,
        name=req.name,
        rule=req.rule,
        severity=req.severity,
        description=req.description,
    )


@router.post(
    "/{instance_id}/decision-policies",
    response_model=contracts.AddDecisionPolicyResult,
)
async def add_decision_policy(
    instance_id: str,
    req: AddDecisionPolicyRequest,
) -> contracts.AddDecisionPolicyResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.add_decision_policy(
        instance_id=resolved_instance_id,
        name=req.name,
        applies_to=req.applies_to,
        relationship_type=req.relationship_type,
        effect=req.effect,
        match=req.match,
        description=req.description,
        rationale=req.rationale,
        query_name=req.query_name,
        workflow_name=req.workflow_name,
        expires_at=req.expires_at,
    )


@router.post("/{instance_id}/config/reload", response_model=contracts.ReloadConfigResult)
async def reload_config(
    instance_id: str,
    req: ReloadConfigRequest,
) -> contracts.ReloadConfigResult:
    return local_api.reload_config(
        instance_id=resolve_server_instance_id(instance_id),
        config_path=req.config_path,
        config_yaml=req.config_yaml,
    )
