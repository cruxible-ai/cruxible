"""Published world release and pull routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import local_api
from cruxible_core.server.request_models import (
    WorldOverlayRequest,
    WorldPublishRequest,
    WorldPullApplyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["world"])


@router.post("/worlds/overlays", response_model=contracts.WorldOverlayResult)
async def create_world_overlay(req: WorldOverlayRequest) -> contracts.WorldOverlayResult:
    """Create a new governed overlay from a published world release."""
    return local_api.create_world_overlay_governed(
        transport_ref=req.transport_ref,
        world_ref=req.world_ref,
        kit=req.kit,
        no_kit=req.no_kit,
        root_dir=req.root_dir,
    )


@router.post("/{instance_id}/world/publish", response_model=contracts.WorldPublishResult)
async def world_publish(
    instance_id: str,
    req: WorldPublishRequest,
) -> contracts.WorldPublishResult:
    """Publish a root world-model instance to a release transport."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.world_publish(
        resolved_instance_id,
        transport_ref=req.transport_ref,
        world_id=req.world_id,
        release_id=req.release_id,
        compatibility=req.compatibility,
    )


@router.get("/{instance_id}/world/status", response_model=contracts.WorldStatusResult)
async def world_status(instance_id: str) -> contracts.WorldStatusResult:
    """Read upstream tracking metadata for a release-backed overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.world_status(resolved_instance_id)


@router.post(
    "/{instance_id}/world/pull/preview",
    response_model=contracts.WorldPullPreviewResult,
)
async def world_pull_preview(instance_id: str) -> contracts.WorldPullPreviewResult:
    """Preview pulling a new upstream release into an overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.world_pull_preview(resolved_instance_id)


@router.post(
    "/{instance_id}/world/pull/apply",
    response_model=contracts.WorldPullApplyResult,
)
async def world_pull_apply(
    instance_id: str,
    req: WorldPullApplyRequest,
) -> contracts.WorldPullApplyResult:
    """Apply a previewed upstream release into an overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.world_pull_apply(
        resolved_instance_id,
        expected_apply_digest=req.expected_apply_digest,
    )
