"""Graph-snapshot, clone, and instance-backup routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.auth import get_current_auth_context
from cruxible_core.server.auth_managed_entities import materialize_auth_managed_entities
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.request_models import (
    CloneSnapshotRequest,
    InstanceBackupRequest,
    InstanceRelocateRequest,
    SnapshotCreateRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["snapshots"])


@router.post("/{instance_id}/snapshots", response_model=contracts.SnapshotCreateResult)
async def create_snapshot(
    instance_id: str,
    req: SnapshotCreateRequest,
) -> contracts.SnapshotCreateResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.create_snapshot(resolved_instance_id, req.label, actor_context=req.actor_context)


@router.post("/{instance_id}/instance/backup", response_model=contracts.InstanceBackupResult)
async def backup_instance(
    instance_id: str,
    req: InstanceBackupRequest,
) -> contracts.InstanceBackupResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.backup_instance(
        resolved_instance_id,
        artifact_path=req.artifact_path,
        label=req.label,
        actor_context=req.actor_context,
    )


@router.post("/{instance_id}/instance/relocate", response_model=contracts.InstanceRelocateResult)
async def relocate_instance(
    instance_id: str,
    req: InstanceRelocateRequest,
) -> contracts.InstanceRelocateResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.relocate_instance(
        resolved_instance_id,
        to_dir=req.to_dir,
        remove_source=req.remove_source,
    )


@router.get("/{instance_id}/snapshots", response_model=contracts.SnapshotListResult)
async def list_snapshots(
    instance_id: str,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.SnapshotListResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_snapshots(resolved_instance_id, limit=limit, offset=offset)


@router.post("/{instance_id}/snapshots/clone", response_model=contracts.CloneSnapshotResult)
async def clone_snapshot(
    instance_id: str,
    req: CloneSnapshotRequest,
) -> contracts.CloneSnapshotResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    result = api.clone_snapshot_governed(
        resolved_instance_id,
        req.snapshot_id,
        req.root_dir,
    )
    if not is_server_auth_enabled():
        return result
    return result.model_copy(
        update={"admin_credential": _mint_clone_admin_credential(result.instance_id)}
    )


def _mint_clone_admin_credential(
    clone_instance_id: str,
) -> contracts.RuntimeCredentialBootstrapResult:
    """Mint the one-time initial ADMIN credential for a freshly cloned instance.

    Runtime credentials are scoped to exactly one instance, so the credential
    that authorized the clone cannot reach the new instance, and the daemon's
    one-time bootstrap secret is typically already claimed. Without this mint a
    clone on an auth-enabled daemon would be unreachable through normal auth
    (wi-snapshot-clone-credential-lockout). Mirrors the claim-bootstrap flow:
    prepare, materialize auth-managed entities, commit; the plaintext token is
    returned exactly once and only its hash is stored.
    """
    auth_context = get_current_auth_context()
    store = get_runtime_credential_store()
    prepared = store.prepare_credential(
        instance_id=clone_instance_id,
        label="clone-admin",
        permission_mode=PermissionMode.ADMIN,
        created_by=auth_context.principal_id if auth_context else None,
    )
    materialize_auth_managed_entities(get_manager().get(clone_instance_id), prepared.record)
    created = store.commit_prepared_credential(
        prepared,
        reason="runtime_clone_credential_created",
    )
    return contracts.RuntimeCredentialBootstrapResult(
        credential_id=created.record.credential_id,
        instance_id=created.record.instance_id,
        permission_mode="admin",
        token=created.token,
    )
