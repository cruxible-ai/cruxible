"""Read-only HTTP routes for the install ledger.

READS ONLY, ON PURPOSE. Creating an install, claiming ownership, and advancing
a phase are steps of an ORDERED install sequence — exposing them individually
over HTTP would invite half-installs that no orchestrator is driving and no
preflight has cleared. They stay service-internal until the installer (which
owns that ordering) lands. What is useful now is visibility: what has been
installed, what it owns, and how far each attempt got.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["installs"])


@router.get(
    "/{instance_id}/installs",
    response_model=contracts.ListResult,
)
async def list_installs(
    instance_id: str,
    phase: str | None = Query(default=None),
    artifact_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.list_installs(
        resolve_server_instance_id(instance_id),
        phase=phase,
        artifact_id=artifact_id,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/installs/{install_id}",
    response_model=contracts.InstallDetailResult,
)
async def get_install(
    instance_id: str,
    install_id: str,
) -> contracts.InstallDetailResult:
    return api.get_install(resolve_server_instance_id(instance_id), install_id)
