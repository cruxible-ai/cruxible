"""Source artifact routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    DereferenceSourceEvidenceRequest,
    RegisterSourceArtifactRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["source-artifacts"])


@router.get(
    "/{instance_id}/source-artifacts",
    response_model=contracts.SourceArtifactListResult,
)
async def list_source_artifacts(
    instance_id: str,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.SourceArtifactListResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_source_artifacts(
        instance_id=resolved_instance_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/source-artifacts/{artifact_id}",
    response_model=contracts.SourceArtifactReadResult,
    response_model_exclude_none=True,
)
async def get_source_artifact(
    instance_id: str,
    artifact_id: str,
) -> contracts.SourceArtifactReadResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.get_source_artifact(
        instance_id=resolved_instance_id,
        source_artifact_id=artifact_id,
    )


@router.post(
    "/{instance_id}/source-artifacts/register",
    response_model=contracts.RegisterSourceArtifactResult,
)
async def register_source_artifact(
    instance_id: str,
    req: RegisterSourceArtifactRequest,
) -> contracts.RegisterSourceArtifactResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.register_source_artifact(
        instance_id=resolved_instance_id,
        source_path=req.source_path,
        source_kind=req.source_kind,
        source_retention=req.source_retention,
        original_uri=req.original_uri,
        label=req.label,
        source_artifact_id=req.source_artifact_id,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/source-evidence/dereference",
    response_model=contracts.DereferenceSourceEvidenceResult,
)
async def dereference_source_evidence(
    instance_id: str,
    req: DereferenceSourceEvidenceRequest,
) -> contracts.DereferenceSourceEvidenceResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.dereference_source_evidence(
        instance_id=resolved_instance_id,
        source_artifact_id=req.source_artifact_id,
        chunk_id=req.chunk_id,
        heading_path=req.heading_path,
        block_selector=req.block_selector,
        expected_content_hash=req.expected_content_hash,
    )
