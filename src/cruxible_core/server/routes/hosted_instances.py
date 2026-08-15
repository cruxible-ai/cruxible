"""Generic daemon host allocation required before Playbill bootstrap."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import host_api
from cruxible_core.server.request_models import PlaybillHostCreateRequest
from cruxible_core.server.route_paths import PLAYBILL_HOST_CREATE_PATH

router = APIRouter(prefix="/api/v1", tags=["playbill-hosts"])


@router.post(
    PLAYBILL_HOST_CREATE_PATH,
    response_model=contracts.PlaybillHostResult,
)
async def create_playbill_host(req: PlaybillHostCreateRequest) -> contracts.PlaybillHostResult:
    """Allocate an empty daemon-owned host; no config or state is adopted."""
    return host_api.create_playbill_host(instance_id=req.instance_id)
