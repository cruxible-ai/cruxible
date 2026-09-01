"""Minimal daemon-host facade required to reach Playbill.

Host allocation and transport credentials create no semantic authority. They
only allocate an opaque daemon-owned storage root and control which endpoints
a caller may reach; Playbill bootstrap establishes governed state separately.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client import contracts
from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.playbill.workspace_advertisement import workspace_git_object_format
from cruxible_core.runtime.permissions import check_permission, require_unscoped_operator
from cruxible_core.server.config import (
    get_server_state_root,
    is_server_auth_enabled,
    is_server_required,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry


def create_playbill_host(
    *,
    instance_id: str | None = None,
    workspace_root: str | None = None,
    workspace_attachment_authorized: bool = False,
) -> contracts.PlaybillHostResult:
    """Allocate one empty daemon-owned host record for later Playbill bootstrap."""

    registry = get_registry()
    selected = (instance_id or "").strip() or registry.generate_governed_instance_id()
    check_permission("cruxible_playbill_host_create", instance_id=selected)
    require_unscoped_operator("cruxible_playbill_host_create")
    if workspace_root is not None and not workspace_attachment_authorized:
        raise ConfigError(
            "Workspace attachment requires a caller connected directly through the local "
            "Unix socket"
        )
    if workspace_root is not None:
        try:
            workspace_git_object_format(Path(workspace_root))
        except ValueError as exc:
            raise ConfigError("Workspace attachment requires one local Git worktree") from exc
        attached = registry.get_governed_instance_by_workspace_root(workspace_root)
        if attached is not None and attached.instance_id != selected:
            raise ConfigError(
                f"Workspace {str(Path(workspace_root).expanduser().resolve())!r} is already "
                f"attached to Playbill host {attached.instance_id!r}; archive/rebuild that "
                f"host or choose another Git worktree before creating {selected!r}"
            )

    existing = registry.get(selected)
    if existing is not None:
        if existing.backend != GOVERNED_DAEMON_BACKEND:
            raise ConfigError(f"Instance '{selected}' is not a governed daemon host")
        if workspace_root is not None:
            if Path(existing.location).exists() and existing.workspace_root is None:
                raise ConfigError(
                    f"Playbill host {selected!r} is already initialized without workspace "
                    "attachment; archive/rebuild an attached host, record attachment before "
                    "init, then re-seed"
                )
            registry.attach_governed_workspace(selected, workspace_root)
        return contracts.PlaybillHostResult(instance_id=selected, status="already_exists")

    registered = registry.create_governed_instance_with_id(
        selected,
        workspace_root=workspace_root,
    )
    if registered.record.instance_id != selected:
        raise ConfigError(
            f"Workspace {registered.record.workspace_root!r} is already attached to Playbill "
            f"host {registered.record.instance_id!r}; archive/rebuild that host or choose "
            f"another Git worktree before creating {selected!r}"
        )
    return contracts.PlaybillHostResult(
        instance_id=selected,
        status="created" if registered.created else "already_exists",
    )


def server_info() -> contracts.ServerInfoResult:
    """Return daemon metadata without loading any semantic instance."""

    check_permission("cruxible_server_info")
    require_unscoped_operator("cruxible_server_info")
    store = get_runtime_credential_store()
    return contracts.ServerInfoResult(
        server_required=is_server_required(),
        state_root=str(get_server_state_root()),
        version=__version__,
        instance_count=get_registry().count_instances(),
        auth_enabled=is_server_auth_enabled(),
        auth_required=store.is_auth_required(),
    )


def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place daemon re-exec."""

    check_permission("cruxible_server_restart")
    require_unscoped_operator("cruxible_server_restart")
    from cruxible_core.server.restart import schedule_server_restart

    schedule_server_restart()
    return contracts.ServerRestartResult(
        scheduled=True,
        version=__version__,
        state_root=str(get_server_state_root()),
    )


__all__ = ["create_playbill_host", "server_info", "server_restart"]
