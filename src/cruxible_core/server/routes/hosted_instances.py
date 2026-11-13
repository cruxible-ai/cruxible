"""Generic daemon host allocation required before Cruxible bootstrap."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from cruxible_client import contracts
from cruxible_client.contracts.change_control import ChangeControlRequest
from cruxible_core.runtime import host_api
from cruxible_core.runtime.admission import FLOOR_ADMISSION
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.server.request_models import (
    HostCreateRequest,
    HostWorkspaceAttachRequest,
)
from cruxible_core.server.route_paths import (
    PLAYBILL_FLOOR_DELIVER_NOW_PATH,
    PLAYBILL_FLOOR_DELIVERY_PATH,
    PLAYBILL_HOST_CREATE_PATH,
    PLAYBILL_HOST_SHOW_PATH,
    PLAYBILL_WORKSPACE_ATTACH_PATH,
    PLAYBILL_WORKSPACE_DETACH_PATH,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["hosts"])


@router.get(PLAYBILL_HOST_SHOW_PATH, response_model=contracts.HostInspection)
def show_playbill_host(instance_id: str) -> contracts.HostInspection:
    """Inspect one daemon host without acquiring semantic authority."""

    return host_api.show_playbill_host(resolve_server_instance_id(instance_id))


@router.post(
    PLAYBILL_HOST_CREATE_PATH,
    response_model=contracts.HostResult,
    response_model_exclude={"git_workspace_note"},
)
def create_playbill_host(
    req: HostCreateRequest,
    request: Request,
) -> contracts.HostResult:
    """Allocate an empty daemon-owned host; no config or state is adopted."""
    return host_api.create_playbill_host(
        instance_id=req.instance_id,
        workspace_root=req.workspace_root,
        workspace_attachment_authorized=_local_socket(request),
        dry_run=req.dry_run,
        at=req.at,
    )


def _local_socket(request: Request) -> bool:
    return request.scope.get("client") is None and (
        resolve_server_settings().server_socket is not None
    )


@router.post(
    PLAYBILL_WORKSPACE_ATTACH_PATH,
    response_model=contracts.HostWorkspaceAttachResult,
)
def playbill_host_workspace_attach(
    instance_id: str,
    req: HostWorkspaceAttachRequest,
    request: Request,
) -> contracts.HostWorkspaceAttachResult:
    """Attach a host to a Git worktree, initialized or not; local-socket callers only."""

    return host_api.playbill_host_workspace_attach(
        resolve_server_instance_id(instance_id),
        workspace_root=req.workspace_root,
        workspace_attachment_authorized=_local_socket(request),
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    PLAYBILL_WORKSPACE_DETACH_PATH,
    response_model=contracts.WorkspaceDetachResult,
)
async def playbill_host_workspace_detach(
    instance_id: str,
    request: Request,
    req: ChangeControlRequest | None = None,
) -> contracts.WorkspaceDetachResult:
    """Release one host's Git worktree; only local-socket callers may ask."""

    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def detach() -> contracts.WorkspaceDetachResult:
        return host_api._playbill_host_workspace_detach_admitted(
            resolved,
            workspace_attachment_authorized=_local_socket(request),
            dry_run=None if req is None else req.dry_run,
            at=None if req is None else req.at,
        )

    async with FLOOR_ADMISSION.admit(resolved) as ticket:
        return await run_in_threadpool(ticket.run, detach)


@router.get(
    "/{instance_id}/workspace-registration",
    response_model=contracts.HostWorkspaceRegistration,
)
def playbill_host_workspace_registration(
    instance_id: str,
    request: Request,
    workspace_root: str | None = None,
) -> contracts.HostWorkspaceRegistration:
    """Report daemon attachment; only local-socket callers receive its path.

    A caller naming ``workspace_root`` learns whether the daemon delivers that
    workspace's floor (``delivers_here``) on any transport.
    """

    return host_api.playbill_host_workspace_registration(
        resolve_server_instance_id(instance_id),
        expose_workspace_path=(
            request.scope.get("client") is None
            and resolve_server_settings().server_socket is not None
        ),
        workspace_root=workspace_root,
    )


@router.post(PLAYBILL_FLOOR_DELIVERY_PATH, response_model=contracts.HostWorkspaceRegistration)
async def set_playbill_floor_delivery(
    instance_id: str,
    req: contracts.FloorDeliveryRequest,
    request: Request,
) -> contracts.HostWorkspaceRegistration:
    """Set the local workspace's floor writer through attachment authority."""

    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def toggle() -> contracts.HostWorkspaceRegistration:
        return host_api.set_playbill_floor_delivery_admitted(
            resolved,
            enabled=req.enabled,
            workspace_attachment_authorized=(
                request.scope.get("client") is None
                and resolve_server_settings().server_socket is not None
            ),
        )

    async with FLOOR_ADMISSION.admit(resolved) as ticket:
        return await run_in_threadpool(ticket.run, toggle)


@router.post(PLAYBILL_FLOOR_DELIVER_NOW_PATH, response_model=contracts.FloorDeliveryResult)
async def deliver_playbill_floor_now(
    instance_id: str, request: Request, req: contracts.FloorDeliverNowRequest
) -> contracts.FloorDeliveryResult:
    """Deliver under the floor consumer's admission and return its write receipt."""

    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def deliver() -> contracts.FloorDeliveryResult:
        return host_api.deliver_playbill_floor_now_admitted(
            resolved,
            include=req.include,
            at=req.at,
            workspace_attachment_authorized=(
                request.scope.get("client") is None
                and resolve_server_settings().server_socket is not None
            ),
        )

    async with FLOOR_ADMISSION.admit(resolved) as ticket:
        return await run_in_threadpool(ticket.run, deliver)
