"""Published state release and pull routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    StateOverlayRequest,
    StatePublishRequest,
    StatePullApplyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["state"])


@router.post("/states/overlays", response_model=contracts.StateOverlayResult)
async def create_state_overlay(req: StateOverlayRequest) -> contracts.StateOverlayResult:
    """Create a new governed overlay from a published state release."""
    return api.create_state_overlay_governed(
        transport_ref=req.transport_ref,
        state_ref=req.state_ref,
        kit=req.kit,
        no_kit=req.no_kit,
        root_dir=req.root_dir,
    )


@router.post("/{instance_id}/state/publish", response_model=contracts.StatePublishResult)
async def state_publish(
    instance_id: str,
    req: StatePublishRequest,
) -> contracts.StatePublishResult:
    """Publish a root state instance to a release transport."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.state_publish(
        resolved_instance_id,
        transport_ref=req.transport_ref,
        state_id=req.state_id,
        release_id=req.release_id,
        compatibility=req.compatibility,
    )


@router.get("/{instance_id}/state/status", response_model=contracts.StateStatusResult)
async def state_status(instance_id: str) -> contracts.StateStatusResult:
    """Read upstream tracking metadata for a release-backed overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.state_status(resolved_instance_id)


@router.post(
    "/{instance_id}/state/pull/preview",
    response_model=contracts.StatePullPreviewResult,
)
async def state_pull_preview(instance_id: str) -> contracts.StatePullPreviewResult:
    """Preview pulling a new upstream release into an overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.state_pull_preview(resolved_instance_id)


@router.post(
    "/{instance_id}/state/pull/apply",
    response_model=contracts.StatePullApplyResult,
)
async def state_pull_apply(
    instance_id: str,
    req: StatePullApplyRequest,
) -> contracts.StatePullApplyResult:
    """Apply a previewed upstream release into an overlay."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.state_pull_apply(
        resolved_instance_id,
        expected_apply_digest=req.expected_apply_digest,
    )
