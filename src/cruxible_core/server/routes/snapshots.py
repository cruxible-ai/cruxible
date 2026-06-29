"""Graph-snapshot, clone, and instance-backup routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
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
    return api.clone_snapshot_governed(
        resolved_instance_id,
        req.snapshot_id,
        req.root_dir,
    )
