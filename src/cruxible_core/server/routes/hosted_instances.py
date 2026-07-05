"""Hosted runtime instance initialization routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import HostedInstanceInitRequest
from cruxible_core.server.route_paths import HOSTED_INSTANCE_INIT_PATH

router = APIRouter(prefix="/api/v1", tags=["hosted-instances"])


@router.post(
    HOSTED_INSTANCE_INIT_PATH,
    response_model=contracts.HostedInstanceInitResult,
)
async def init_hosted_instance(
    req: HostedInstanceInitRequest,
) -> contracts.HostedInstanceInitResult:
    """Initialize a fresh hosted instance from a kit or reference model source."""
    return api.init_hosted_instance(
        instance_id=req.instance_id,
        source_type=req.source_type,
        kit_refs=req.kit_refs,
        transport_ref=req.transport_ref,
        state_ref=req.state_ref,
        overlay_kit_ref=req.overlay_kit_ref,
        no_overlay_kit=req.no_overlay_kit,
    )
