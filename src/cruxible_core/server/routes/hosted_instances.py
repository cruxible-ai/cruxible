"""Generic daemon host allocation required before Playbill bootstrap."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cruxible_client import contracts
from cruxible_core.runtime import host_api
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.server.request_models import PlaybillHostCreateRequest
from cruxible_core.server.route_paths import PLAYBILL_HOST_CREATE_PATH

router = APIRouter(prefix="/api/v1", tags=["playbill-hosts"])


@router.post(
    PLAYBILL_HOST_CREATE_PATH,
    response_model=contracts.PlaybillHostResult,
)
def create_playbill_host(
    req: PlaybillHostCreateRequest,
    request: Request,
) -> contracts.PlaybillHostResult:
    """Allocate an empty daemon-owned host; no config or state is adopted."""
    return host_api.create_playbill_host(
        instance_id=req.instance_id,
        workspace_root=req.workspace_root,
        workspace_attachment_authorized=(
            request.scope.get("client") is None
            and resolve_server_settings().server_socket is not None
        ),
    )
