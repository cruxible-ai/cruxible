"""Mutation routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.graph.provenance import (
    SOURCE_REF_ADD_RELATIONSHIP,
    SOURCE_REF_BATCH_DIRECT_WRITE,
)
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    AddConstraintRequest,
    AddDecisionPolicyRequest,
    AddEntitiesRequest,
    AddRelationshipsRequest,
    BatchDirectWriteRequest,
    ReloadConfigRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["mutations"])


@router.post("/{instance_id}/entities", response_model=contracts.AddEntityResult)
async def add_entities(
    instance_id: str,
    req: AddEntitiesRequest,
) -> contracts.AddEntityResult:
    return api.add_entities(
        instance_id=resolve_server_instance_id(instance_id),
        entities=req.entities,
        dry_run=req.dry_run,
    )


@router.post("/{instance_id}/relationships", response_model=contracts.AddRelationshipResult)
async def add_relationships(
    instance_id: str,
    req: AddRelationshipsRequest,
) -> contracts.AddRelationshipResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.add_relationships_with_provenance(
        instance_id=resolved_instance_id,
        relationships=req.relationships,
        dry_run=req.dry_run,
        provenance_source="http_api",
        provenance_source_ref=SOURCE_REF_ADD_RELATIONSHIP,
    )


@router.post(
    "/{instance_id}/direct-writes/batch",
    response_model=contracts.BatchDirectWriteResult,
)
async def batch_direct_write(
    instance_id: str,
    req: BatchDirectWriteRequest,
) -> contracts.BatchDirectWriteResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.batch_direct_write(
        instance_id=resolved_instance_id,
        payload=req.payload,
        dry_run=req.dry_run,
        provenance_source="http_api",
        provenance_source_ref=SOURCE_REF_BATCH_DIRECT_WRITE,
    )


@router.post("/{instance_id}/constraints", response_model=contracts.AddConstraintResult)
async def add_constraint(
    instance_id: str,
    req: AddConstraintRequest,
) -> contracts.AddConstraintResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.add_constraint(
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
    return api.add_decision_policy(
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
    return api.reload_config(
        instance_id=resolve_server_instance_id(instance_id),
        config_path=req.config_path,
        config_yaml=req.config_yaml,
    )
