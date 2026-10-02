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
from cruxible_core.service.change_preview import state_change_scope

router = APIRouter(prefix="/api/v1", tags=["instances"])


@router.post(
    RUNTIME_BOOTSTRAP_CLAIM_PATH,
    response_model=contracts.RuntimeCredentialBootstrapResult,
)
def claim_runtime_bootstrap(
    instance_id: str,
    req: BootstrapClaimRequest,
) -> contracts.RuntimeCredentialBootstrapResult:
    """Exchange the bootstrap secret for one host's initial ADMIN runtime token.

    The secret is claimable once per host. ``dry_run`` checks the claim and
    claims nothing (R12).
    """
    resolved_instance_id = resolve_server_instance_id(instance_id)
    check_permission("cruxible_runtime_credentials", instance_id=resolved_instance_id)
    store = get_runtime_credential_store()
    with state_change_scope(
        dry_run=req.dry_run,
        at=req.at,
        kind="direct",
        operation="credential.claim-bootstrap",
        describe=f"claiming the bootstrap credential of {resolved_instance_id}",
    ) as change:
        created = store.prepare_bootstrap_credential(
            instance_id=resolved_instance_id,
            bootstrap_secret=req.bootstrap_secret,
            expected_bootstrap_secret=get_runtime_bootstrap_secret(),
            observe=change.observe if change.previewing else None,
        )
        if not change.previewing:
            created = store.claim_prepared_bootstrap_credential(
                created, bootstrap_secret=req.bootstrap_secret, observe=change.observe
            )
    return contracts.RuntimeCredentialBootstrapResult(
        status="would_claim" if change.previewing else "claimed",
        credential_id=created.record.credential_id,
        instance_id=created.record.instance_id,
        permission_mode="admin",
        token=None if change.previewing else created.token,
        coordinate=change.coordinate,
    )


@router.get("/server/info", response_model=contracts.ServerInfoResult)
def server_info() -> contracts.ServerInfoResult:
    """Return live daemon metadata for clients and agent skills."""
    return host_api.server_info()


@router.post("/server/restart", response_model=contracts.ServerRestartResult)
def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place daemon re-exec, preserving port, state dir, and env."""
    return host_api.server_restart()


@router.post("/server/stop", response_model=contracts.ServerStopResult)
def server_stop() -> contracts.ServerStopResult:
    """Schedule a graceful daemon shutdown that releases the state-root lock."""
    return host_api.server_stop()
