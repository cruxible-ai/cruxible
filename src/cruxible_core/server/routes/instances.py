"""Daemon lifecycle and runtime bootstrap routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import host_api
from cruxible_core.runtime.permissions import check_permission
from cruxible_core.server.config import get_runtime_bootstrap_secret
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.request_models import (
    BootstrapClaimRequest,
)
from cruxible_core.server.route_paths import RUNTIME_BOOTSTRAP_CLAIM_PATH
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["instances"])


@router.post(
    RUNTIME_BOOTSTRAP_CLAIM_PATH,
    response_model=contracts.RuntimeCredentialBootstrapResult,
)
async def claim_runtime_bootstrap(
    instance_id: str,
    req: BootstrapClaimRequest,
) -> contracts.RuntimeCredentialBootstrapResult:
    """Exchange a one-time bootstrap secret for the initial ADMIN runtime token."""
    resolved_instance_id = resolve_server_instance_id(instance_id)
    check_permission("cruxible_runtime_credentials", instance_id=resolved_instance_id)
    store = get_runtime_credential_store()
    created = store.claim_bootstrap_credential(
        instance_id=resolved_instance_id,
        bootstrap_secret=req.bootstrap_secret,
        expected_bootstrap_secret=get_runtime_bootstrap_secret(),
    )
    return contracts.RuntimeCredentialBootstrapResult(
        credential_id=created.record.credential_id,
        instance_id=created.record.instance_id,
        permission_mode="admin",
        token=created.token,
    )


@router.get("/server/info", response_model=contracts.ServerInfoResult)
async def server_info() -> contracts.ServerInfoResult:
    """Return live daemon metadata for clients and agent skills."""
    return host_api.server_info()


@router.post("/server/restart", response_model=contracts.ServerRestartResult)
async def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place daemon re-exec, preserving port, state dir, and env."""
    return host_api.server_restart()
