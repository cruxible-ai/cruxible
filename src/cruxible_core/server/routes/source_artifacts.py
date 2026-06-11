"""Source artifact routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    DereferenceSourceEvidenceRequest,
    RegisterSourceArtifactRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["source-artifacts"])


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
