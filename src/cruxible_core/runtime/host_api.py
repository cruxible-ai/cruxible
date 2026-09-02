"""Minimal daemon-host facade required to reach Playbill.

Host allocation and transport credentials create no semantic authority. They
only allocate an opaque daemon-owned storage root and control which endpoints
a caller may reach; Playbill bootstrap establishes governed state separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from cruxible_client import contracts
from cruxible_client.contracts.errors import (
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillReseedRequired,
)
from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.playbill.compiler import (
    COMPILER_REVISION_LABELS,
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    current_compiler_coordinate,
)
from cruxible_core.playbill.workspace_advertisement import workspace_git_object_format
from cruxible_core.runtime.permissions import check_permission, require_unscoped_operator
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.config import (
    get_server_state_root,
    is_server_auth_enabled,
    is_server_required,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry


class _HostCommon(TypedDict):
    instance_id: str
    managed_root: str
    workspace_root: str | None


def _reseed_reason(
    code: contracts.PlaybillHostCompatibilityReasonCodeV1,
    detail: str,
) -> contracts.PlaybillHostCompatibilityReasonV1:
    return contracts.PlaybillHostCompatibilityReasonV1(
        code=code,
        detail=detail,
        repair_commands=("cruxible playbill host create",),
    )


def _inspect_registered_host(instance_id: str) -> contracts.PlaybillHostInspectionV1:
    record = get_registry().get(instance_id)
    if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
        raise ConfigError(
            f"Instance {instance_id!r} is not a governed daemon host; run "
            "`cruxible playbill host create` first"
        )
    managed_root = Path(record.location).resolve(strict=False)
    trust_root = get_registry().state_root / "trust" / f"{instance_id}.json"
    legacy_root = managed_root / ".cruxible"
    common: _HostCommon = {
        "instance_id": instance_id,
        "managed_root": str(managed_root),
        "workspace_root": record.workspace_root,
    }
    if not managed_root.exists() and not trust_root.exists():
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="uninitialized",
            writable=False,
        )
    if (legacy_root / "playbill-v1").exists() or (
        legacy_root / "playbill-trust-root-v1.json"
    ).exists():
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="reseed_required",
            writable=False,
            reason=_reseed_reason(
                "legacy_layout_requires_reseed",
                "The host uses a retired nested Playbill layout and must be reseeded.",
            ),
        )
    if managed_root.exists() != trust_root.exists():
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="reseed_required",
            writable=False,
            reason=_reseed_reason(
                "host_state_incomplete",
                "The managed root and pinned trust root do not both exist.",
            ),
        )
    try:
        compiler = get_playbill_manager().get(instance_id).inspect().compiler
    except PlaybillReseedRequired:
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="reseed_required",
            writable=False,
            reason=_reseed_reason(
                "host_state_incomplete",
                "The host state cannot be opened without reseeding.",
            ),
        )
    except (PlaybillBootstrapError, PlaybillFormatError, OSError, ValueError) as exc:
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="reseed_required",
            writable=False,
            reason=_reseed_reason(
                "host_state_malformed",
                f"The persisted host state is malformed: {exc}",
            ),
        )
    writable = compiler in PC_HR_ARTIFACT_CODEC_COMPILERS
    revision = COMPILER_REVISION_LABELS.get(compiler)
    return contracts.PlaybillHostInspectionV1(
        **common,
        compiler_coordinate=compiler.rule_digest,
        compiler_revision=revision,
        compatibility="writable" if writable else "reseed_required",
        writable=writable,
        reason=(
            None
            if writable
            else _reseed_reason(
                "compiler_lineage_not_writable",
                "The accepted compiler is outside the retained writable lineage.",
            )
        ),
    )


def show_playbill_host(instance_id: str) -> contracts.PlaybillHostInspectionV1:
    """Inspect one governed host without creating or changing any state."""

    check_permission("cruxible_playbill_host_show", instance_id=instance_id)
    result = _inspect_registered_host(instance_id)
    if result.compatibility == "uninitialized":
        require_unscoped_operator("cruxible_playbill_host_show")
    return result


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


def playbill_host_workspace_registration(
    instance_id: str,
    *,
    expose_workspace_path: bool = False,
) -> contracts.PlaybillHostWorkspaceRegistrationV1:
    """Report daemon registration separately from client workspace configuration."""

    check_permission(
        "cruxible_playbill_host_workspace_registration",
        instance_id=instance_id,
    )
    record = get_registry().get(instance_id)
    if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
        raise ConfigError(f"Instance '{instance_id}' is not a governed daemon host")
    return contracts.PlaybillHostWorkspaceRegistrationV1(
        instance_id=instance_id,
        status="registered" if record.workspace_root is not None else "not_registered",
        workspace_path=(
            record.workspace_root
            if expose_workspace_path and record.workspace_root is not None
            else None
        ),
    )


def server_info() -> contracts.ServerInfoResult:
    """Return daemon metadata without loading any semantic instance."""

    check_permission("cruxible_server_info")
    require_unscoped_operator("cruxible_server_info")
    store = get_runtime_credential_store()
    lane_state, lane_code, lane_detail = (
        get_playbill_manager().provider_runtime_operator().lane_status()
    )
    hosts = tuple(
        _inspect_registered_host(record.instance_id)
        for record in get_registry().list_governed_instances()
    )
    current = current_compiler_coordinate()
    return contracts.ServerInfoResult(
        server_required=is_server_required(),
        state_root=str(get_server_state_root()),
        version=__version__,
        instance_count=len(hosts),
        auth_enabled=is_server_auth_enabled(),
        auth_required=store.is_auth_required(),
        provider_lane=contracts.ProviderLaneStatusV1(
            state=lane_state,
            code=lane_code,
            detail=lane_detail,
        ),
        compiler_coordinate=current.rule_digest,
        compiler_revision=COMPILER_REVISION_LABELS[current],
        hosts=hosts,
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


__all__ = [
    "create_playbill_host",
    "playbill_host_workspace_registration",
    "show_playbill_host",
    "server_info",
    "server_restart",
]
