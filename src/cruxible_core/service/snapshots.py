"""Snapshot and clone service functions."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.runtime.instance import CruxibleInstance
from cruxible_core.service.types import (
    CloneSnapshotResult,
    SnapshotCreateResult,
    SnapshotListResult,
)


def service_create_snapshot(
    instance: InstanceProtocol,
    label: str | None = None,
    *,
    actor_context: GovernedActorContext | None = None,
) -> SnapshotCreateResult:
    """Create an immutable full snapshot for the current instance."""
    snapshot = instance.create_snapshot(label=label, actor_context=actor_context)
    return SnapshotCreateResult(snapshot=snapshot)


def service_list_snapshots(
    instance: InstanceProtocol,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> SnapshotListResult:
    """List snapshots for the current instance, newest first.

    Snapshots are ordered by created_at then snapshot_id (descending);
    ``total`` reflects the full count before limit/offset are applied.
    """
    snapshots = sorted(
        instance.list_snapshots(),
        key=lambda snapshot: (snapshot.created_at, snapshot.snapshot_id),
        reverse=True,
    )
    total = len(snapshots)
    end = None if limit is None else offset + limit
    return SnapshotListResult(items=snapshots[offset:end], total=total)


def service_clone_snapshot(
    instance: InstanceProtocol,
    snapshot_id: str,
    root_dir: str | Path,
    *,
    instance_mode: str = CruxibleInstance.DEV_MODE,
) -> CloneSnapshotResult:
    """Create a new local instance from a selected snapshot."""
    if not isinstance(instance, CruxibleInstance):
        raise ConfigError("Snapshot clone currently supports only local filesystem instances")

    cloned, snapshot = CruxibleInstance.clone_from_snapshot(
        instance,
        snapshot_id,
        root_dir,
        instance_mode=instance_mode,
    )
    return CloneSnapshotResult(instance=cloned, snapshot=snapshot)
