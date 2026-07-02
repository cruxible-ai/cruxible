"""Lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.auth_managed_entities import materialize_auth_managed_entities
from cruxible_core.server.config import get_runtime_bootstrap_secret
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.request_models import (
    BootstrapClaimRequest,
    InitRequest,
    InstanceRestoreRequest,
    ValidateRequest,
)
from cruxible_core.server.route_paths import RUNTIME_BOOTSTRAP_CLAIM_PATH
from cruxible_core.server.routes import (
    authorize_governed_instance_lifecycle,
    resolve_server_instance_id,
)

router = APIRouter(prefix="/api/v1", tags=["instances"])


@router.post("/instances", response_model=contracts.InitResult)
async def init_instance(req: InitRequest) -> contracts.InitResult:
    """Create or reload an instance, returning an opaque server ID."""
    authorize_governed_instance_lifecycle(req.root_dir)
    return api.init_governed(
        root_dir=req.root_dir,
        config_path=req.config_path,
        config_yaml=req.config_yaml,
        data_dir=req.data_dir,
        kit=req.kit,
    )


@router.post("/validate", response_model=contracts.ValidateResult)
async def validate_instance(req: ValidateRequest) -> contracts.ValidateResult:
    """Validate a config file or inline YAML."""
    return api.validate(
        config_path=req.config_path,
        config_yaml=req.config_yaml,
    )


@router.post("/instances/restore", response_model=contracts.InstanceRestoreResult)
async def restore_instance(req: InstanceRestoreRequest) -> contracts.InstanceRestoreResult:
    """Restore a same-identity daemon-backed instance from a local artifact path."""
    return api.restore_instance(artifact_path=req.artifact_path, root_dir=req.root_dir)


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
    store = get_runtime_credential_store()
    prepared = store.prepare_bootstrap_credential(
        instance_id=resolved_instance_id,
        bootstrap_secret=req.bootstrap_secret,
        expected_bootstrap_secret=get_runtime_bootstrap_secret(),
    )
    materialize_auth_managed_entities(get_manager().get(resolved_instance_id), prepared.record)
    created = store.claim_prepared_bootstrap_credential(
        prepared,
        bootstrap_secret=req.bootstrap_secret,
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
    return api.server_info()


@router.post("/server/restart", response_model=contracts.ServerRestartResult)
async def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place daemon re-exec, preserving port, state dir, and env."""
    return api.server_restart()
