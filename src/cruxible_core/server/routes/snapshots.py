"""Snapshot and clone routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import local_api
from cruxible_core.server.request_models import CloneSnapshotRequest, SnapshotCreateRequest
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["snapshots"])


@router.post("/{instance_id}/snapshots", response_model=contracts.SnapshotCreateResult)
async def create_snapshot(
    instance_id: str,
    req: SnapshotCreateRequest,
) -> contracts.SnapshotCreateResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.create_snapshot(resolved_instance_id, req.label)


@router.get("/{instance_id}/snapshots", response_model=contracts.SnapshotListResult)
async def list_snapshots(instance_id: str) -> contracts.SnapshotListResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.list_snapshots(resolved_instance_id)


@router.post("/{instance_id}/clone", response_model=contracts.CloneSnapshotResult)
async def clone_snapshot(
    instance_id: str,
    req: CloneSnapshotRequest,
) -> contracts.CloneSnapshotResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return local_api.clone_snapshot_governed(
        resolved_instance_id,
        req.snapshot_id,
        req.root_dir,
    )
