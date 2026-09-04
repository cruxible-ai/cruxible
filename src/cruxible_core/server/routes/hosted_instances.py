"""Generic daemon host allocation required before Playbill bootstrap."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cruxible_client import contracts
from cruxible_core.runtime import host_api
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.server.request_models import PlaybillHostCreateRequest
from cruxible_core.server.route_paths import PLAYBILL_HOST_CREATE_PATH, PLAYBILL_HOST_SHOW_PATH
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["playbill-hosts"])


@router.get(PLAYBILL_HOST_SHOW_PATH, response_model=contracts.PlaybillHostInspectionV1)
def show_playbill_host(instance_id: str) -> contracts.PlaybillHostInspectionV1:
    """Inspect one daemon host without acquiring semantic authority."""

    return host_api.show_playbill_host(resolve_server_instance_id(instance_id))


@router.post(
    PLAYBILL_HOST_CREATE_PATH,
    response_model=contracts.PlaybillHostResult,
    response_model_exclude={"git_workspace_note"},
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


@router.post(
    "/{instance_id}/playbill/workspace-detach",
    response_model=contracts.PlaybillWorkspaceDetachResultV1,
)
def playbill_host_workspace_detach(
    instance_id: str,
    request: Request,
) -> contracts.PlaybillWorkspaceDetachResultV1:
    """Release one host's Git worktree; only local-socket callers may ask."""

    return host_api.playbill_host_workspace_detach(
        resolve_server_instance_id(instance_id),
        workspace_attachment_authorized=(
            request.scope.get("client") is None
            and resolve_server_settings().server_socket is not None
        ),
    )


@router.get(
    "/{instance_id}/playbill/workspace-registration",
    response_model=contracts.PlaybillHostWorkspaceRegistrationV1,
)
def playbill_host_workspace_registration(
    instance_id: str,
    request: Request,
) -> contracts.PlaybillHostWorkspaceRegistrationV1:
    """Report daemon attachment; only local-socket callers receive its path."""

    return host_api.playbill_host_workspace_registration(
        resolve_server_instance_id(instance_id),
        expose_workspace_path=(
            request.scope.get("client") is None
            and resolve_server_settings().server_socket is not None
        ),
    )
