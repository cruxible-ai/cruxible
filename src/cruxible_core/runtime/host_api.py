"""Minimal daemon-host facade required to reach Playbill.

Host allocation and transport credentials create no semantic authority. They
only allocate an opaque daemon-owned storage root and control which endpoints
a caller may reach; Playbill bootstrap establishes governed state separately.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from cruxible_client import contracts
from cruxible_client.contracts.change_control import PlaybillStateCoordinateV1
from cruxible_client.contracts.errors import (
    PlaybillObjectFormatConflict,
    PlaybillReseedRequired,
)
from cruxible_core import __version__
from cruxible_core.compiler.compiler import (
    COMPILER_REVISION_LABELS,
    PC_HR_ARTIFACT_CODEC_COMPILERS,
    current_compiler_coordinate,
)
from cruxible_core.errors import ConfigError, InstanceLocationRefusedError
from cruxible_core.floor.workspace_advertisement import workspace_git_object_format
from cruxible_core.runtime.execution_policy import registered_isolated_executors
from cruxible_core.runtime.permissions import (
    check_permission,
    current_request_instance_scope,
    require_unscoped_operator,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.config import (
    get_server_state_root,
    is_server_auth_enabled,
    is_server_required,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry
from cruxible_core.service.change_preview import state_change_scope
from cruxible_core.storage.preview_fence import is_previewing


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
    try:
        managed_root = get_registry().instance_root(record)
    except InstanceLocationRefusedError as exc:
        return contracts.PlaybillHostInspectionV1(
            instance_id=instance_id,
            managed_root=record.location,
            workspace_root=record.workspace_root,
            compatibility="refused",
            writable=False,
            reason=contracts.PlaybillHostCompatibilityReasonV1(
                code="location_outside_state_root",
                detail=str(exc),
                repair_commands=("cruxible server start --state-root <the root that holds it>",),
            ),
        )
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
        instance = get_playbill_manager().get(instance_id)
        compiler = instance.inspect().compiler
        terminal = instance.descriptor.decommissioned
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
    except Exception as exc:
        return contracts.PlaybillHostInspectionV1(
            **common,
            compatibility="reseed_required",
            writable=False,
            reason=_reseed_reason(
                "host_state_malformed",
                f"The persisted host state is malformed ({type(exc).__name__}): {exc}",
            ),
        )
    revision = COMPILER_REVISION_LABELS.get(compiler)
    if terminal is not None:
        # Decommissioning is terminal: the host keeps serving reads but no
        # governed write is ever accepted again, whatever its compiler lineage.
        return contracts.PlaybillHostInspectionV1(
            **common,
            compiler_coordinate=compiler.rule_digest,
            compiler_revision=revision,
            compatibility="decommissioned",
            writable=False,
            reason=contracts.PlaybillHostCompatibilityReasonV1(
                code="instance_decommissioned",
                detail=(
                    f"Decommissioned at {terminal.decommissioned_at}: {terminal.reason}. "
                    "Reads keep serving; writes are refused."
                ),
                repair_commands=("cruxible playbill host create",),
            ),
        )
    writable = compiler in PC_HR_ARTIFACT_CODEC_COMPILERS
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
    if current_request_instance_scope() is not None:
        result = result.model_copy(update={"managed_root": None})
    return result


def create_playbill_host(
    *,
    instance_id: str | None = None,
    workspace_root: str | None = None,
    workspace_attachment_authorized: bool = False,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.PlaybillHostResult:
    """Allocate one empty daemon-owned host record for later Playbill bootstrap.

    An existing host named again with a workspace is attached to it, initialized
    or not (`attach_workspace`). ``dry_run`` registers nothing (R12); the
    outcome is pinned to the host's registry row, which ``at`` carries back.
    """

    with state_change_scope(
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="playbill.host.create",
        describe="allocating a Playbill host",
    ) as change:
        registry = get_registry()
        selected = (instance_id or "").strip() or registry.generate_governed_instance_id()
        # The daemon-wide scope gate runs FIRST. Allocating a host is not an
        # access to the instance it allocates -- that instance does not exist
        # yet -- so an instance-scoped credential reaching this route must be
        # told the real boundary it crossed and the credential that clears it,
        # rather than the cross-instance message it happened to trip on the way.
        require_unscoped_operator("cruxible_playbill_host_create")
        check_permission("cruxible_playbill_host_create", instance_id=selected)
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
                    f"attached to Playbill host {attached.instance_id!r}; release it with "
                    f"`cruxible playbill workspace detach --instance-id {attached.instance_id}` "
                    f"or choose another Git worktree before creating {selected!r}"
                )

        existing = registry.get(selected)
        if existing is not None:
            if existing.backend != GOVERNED_DAEMON_BACKEND:
                raise ConfigError(f"Instance '{selected}' is not a governed daemon host")
            change.observe(registry.host_state(selected))
            if workspace_root is not None:
                attach_workspace(selected, workspace_root)
            return contracts.PlaybillHostResult(
                instance_id=selected, status="already_exists", coordinate=change.coordinate
            )
        # Validation is shared: the preview answers from the same prepared row
        # the commit inserts, so both refuse the same IDs and conflicts.
        prepared = registry.prepare_governed_instance(selected, workspace_root=workspace_root)
        if change.previewing:
            change.observe(registry.host_state(selected))
            return contracts.PlaybillHostResult(
                instance_id=selected, status="would_create", coordinate=change.coordinate
            )
        registered = registry.create_governed_instance(prepared, observe=change.observe)
    if registered.record.instance_id != selected:
        raise ConfigError(
            f"Workspace {registered.record.workspace_root!r} is already attached to Playbill "
            f"host {registered.record.instance_id!r}; release it with `cruxible playbill "
            f"workspace detach --instance-id {registered.record.instance_id}` or choose "
            f"another Git worktree before creating {selected!r}"
        )
    return contracts.PlaybillHostResult(
        instance_id=selected,
        status="created" if registered.created else "already_exists",
        coordinate=change.coordinate,
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


def attach_workspace(
    instance_id: str,
    workspace_root: str,
    *,
    observe: Callable[[PlaybillStateCoordinateV1], None] | None = None,
) -> bool:
    """Attach one host to a Git worktree, before or after its init; True when newly.

    The one attach path. An initialized host attaches when the worktree is in
    its ledger's Git object format (the advisory remote refuses across formats)
    and holds no part of its managed root; nothing is rebuilt, and the open
    instance starts advertising to the worktree at once. Inside a preview it
    checks everything and registers nothing. ``observe`` sees the host's
    binding where the attach writes it (or, previewing, as it reads it).
    """

    registry = get_registry()
    record = registry.get(instance_id)
    if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
        raise ConfigError(f"Instance '{instance_id}' is not a governed daemon host")
    try:
        resolved = Path(workspace_root).expanduser().resolve(strict=True)
        workspace_format = workspace_git_object_format(resolved)
    except (OSError, ValueError) as exc:
        raise ConfigError("Workspace attachment requires one local Git worktree") from exc
    other = registry.get_governed_instance_by_workspace_root(resolved)
    if other is not None and other.instance_id != instance_id:
        raise ConfigError(
            f"Workspace {str(resolved)!r} is already attached to Playbill host "
            f"{other.instance_id!r}; release it with `cruxible playbill workspace detach "
            f"--instance-id {other.instance_id}` first"
        )
    if record.workspace_root is not None:
        if Path(record.workspace_root) == resolved:
            if observe is not None:
                observe(registry.workspace_state(instance_id))
            return False
        raise ConfigError(
            f"Playbill host {instance_id!r} is attached to {record.workspace_root}; release "
            f"it with `cruxible playbill workspace detach --instance-id {instance_id}` first"
        )
    instance = get_playbill_manager().initialized(instance_id)
    if instance is not None:
        managed_root = registry.instance_root(record)
        if managed_root == resolved or managed_root.is_relative_to(resolved):
            raise ConfigError(
                f"Workspace {str(resolved)!r} contains host {instance_id!r}'s managed root; "
                "an agent workspace may hold no part of it"
            )
        ledger_format = instance.descriptor.git_object_format
        if workspace_format != ledger_format:
            raise PlaybillObjectFormatConflict(
                f"{PlaybillObjectFormatConflict.error_code}: host {instance_id!r} keeps a "
                f"{ledger_format} ledger and the worktree is {workspace_format}; repair: "
                f"attach a worktree in {ledger_format}",
                workspace_format=workspace_format,
            )
    if is_previewing():
        if observe is not None:
            observe(registry.workspace_state(instance_id))
        return True
    registry.attach_governed_workspace(instance_id, resolved, observe=observe)
    get_playbill_manager().rebind_workspace(instance_id)
    return True


def playbill_host_workspace_attach(
    instance_id: str,
    *,
    workspace_root: str,
    workspace_attachment_authorized: bool = False,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.PlaybillHostWorkspaceAttachResultV1:
    """Attach a host to a Git worktree, including a host already initialized (Q16).

    Local-socket callers only, as for detaching: the daemon must be able to see
    the path it is asked to attach. ``dry_run`` registers nothing (R12); the
    outcome is pinned to the host's binding, which ``at`` carries back.
    """

    check_permission("cruxible_playbill_host_workspace_attach", instance_id=instance_id)
    if not workspace_attachment_authorized:
        raise ConfigError(
            "Workspace attachment requires a caller connected directly through the local "
            "Unix socket"
        )
    with state_change_scope(
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="playbill.workspace.attach",
        describe=f"attaching host {instance_id}",
    ) as change:
        # Opened behind the preview's guards: a cold open may not repair on disk.
        instance = get_playbill_manager().initialized(instance_id)
        attached = attach_workspace(instance_id, workspace_root, observe=change.observe)
    return contracts.PlaybillHostWorkspaceAttachResultV1(
        instance_id=instance_id,
        status=(
            "already_attached"
            if not attached
            else "would_attach"
            if change.previewing
            else "attached"
        ),
        workspace_root=str(Path(workspace_root).expanduser().resolve()),
        initialized=instance is not None,
        coordinate=change.coordinate,
    )


def playbill_host_workspace_detach(
    instance_id: str,
    *,
    workspace_attachment_authorized: bool = False,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.PlaybillWorkspaceDetachResultV1:
    """Release one governed host from the Git worktree it is attached to.

    The exclusivity is a UNIQUE index on `(backend, workspace_root)` in the
    daemon registry, so a worktree belongs to exactly one host and re-binding it
    to a second one refused with two repairs: "archive/rebuild that host", which
    has no verb, and "choose another Git worktree", which splits a repository in
    two. The rollback that performs exactly this operation already existed and
    was reachable only from an initialization failure. This is that rollback,
    sanctioned.

    It changes no governed state and deletes nothing: the host keeps its whole
    ledger and every read it ever served, and stops being the host of this
    directory. Local-socket callers only, for the same reason attaching is: the
    daemon has to be able to prove the path it is being asked about is one it
    can see.

    It refuses while the host still has publication registrations naming blocks
    in that worktree. Those are markers this host published and still expects to
    find; detaching under them leaves a page carrying markers no host owns,
    which is the state that has no repair from inside the workspace.
    """

    # Detaching acts on this one host's registration, so it is an instance act:
    # the instance's own ADMIN may take it, and the scope check below refuses a
    # credential scoped to any other instance. The unscoped operator (bootstrap
    # secret) may too, as it may for every host.
    check_permission("cruxible_playbill_host_workspace_detach", instance_id=instance_id)
    if not workspace_attachment_authorized:
        raise ConfigError(
            "Workspace detachment requires a caller connected directly through the local "
            "Unix socket"
        )
    with state_change_scope(
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="playbill.workspace.detach",
        describe=f"detaching host {instance_id}",
    ) as change:
        registry = get_registry()
        record = registry.get(instance_id)
        if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
            raise ConfigError(f"Instance '{instance_id}' is not a governed daemon host")
        if record.workspace_root is None:
            return contracts.PlaybillWorkspaceDetachResultV1(
                instance_id=instance_id,
                status="not_registered",
            )
        _refuse_detach_with_registered_blocks(instance_id)
        if change.previewing:
            change.observe(registry.workspace_state(instance_id))
            return contracts.PlaybillWorkspaceDetachResultV1(
                instance_id=instance_id,
                status="would_detach",
                workspace_root=record.workspace_root,
                coordinate=change.coordinate,
            )
        detached = registry.detach_governed_workspace(
            instance_id,
            expected_workspace_root=record.workspace_root,
            observe=change.observe,
        )
    assert detached.workspace_root is None
    return contracts.PlaybillWorkspaceDetachResultV1(
        instance_id=instance_id,
        status="detached",
        workspace_root=record.workspace_root,
        coordinate=change.coordinate,
    )


def _refuse_detach_with_registered_blocks(instance_id: str) -> None:
    """Refuse while this host still registers blocks in the worktree.

    Both declaration roads count, and the check keys on the pair the page names
    rather than on a block id's spelling: a block an agent declared with
    `block repin` is exactly as stranded by a detachment as one the retired
    publication road minted, and it was invisible here because it carried no
    `pub-` prefix.

    The one failure this reads as "registered nothing" is Playbill never having
    been initialized under the host: there is no ledger, so there is no
    registration, so a detachment strands nothing. Every OTHER way of failing to
    open the host means the registrations could not be READ, and reading an
    unreadable host as an empty one would let exactly the state this refusal
    exists to prevent through on a transient fault. Those refuse instead, and
    say which host could not be opened.
    """

    from cruxible_client.contracts.errors import PlaybillBootstrapError, PlaybillError
    from cruxible_core.service.proposals.publications import registered_projection_blocks

    try:
        instance = get_playbill_manager().get(instance_id)
    except PlaybillObjectFormatConflict as exc:
        # A bootstrap error by inheritance, but it means the host is THERE and
        # unreadable, not absent.
        raise _detach_cannot_read_host(instance_id, exc) from exc
    except PlaybillBootstrapError:
        return
    except (ConfigError, PlaybillError) as exc:
        raise _detach_cannot_read_host(instance_id, exc) from exc
    registrations = registered_projection_blocks(instance)
    if registrations is None:
        raise _detach_cannot_read_host(
            instance_id,
            ConfigError("the block registration fold could not be read"),
        )
    if not registrations:
        return
    pairs = sorted(f"{source}#{block}" for source, block in registrations)
    named = ", ".join(pairs[:5])
    if len(pairs) > 5:
        named = f"{named}, and {len(pairs) - 5} more"
    raise ConfigError(
        f"Playbill host {instance_id!r} still registers {len(registrations)} governed "
        f"block(s) in this workspace ({named}); detaching would leave markers no host "
        "owns. Repair: run `cruxible playbill block depublish <source> <block>` for each, "
        "or retire their backing Claims, then detach"
    )


def _detach_cannot_read_host(instance_id: str, exc: Exception) -> ConfigError:
    return ConfigError(
        f"Playbill host {instance_id!r} could not be opened, so the blocks it published "
        f"cannot be read and a detachment cannot be shown to strand nothing ({exc}). "
        "Repair: make the host readable, then detach"
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
    from cruxible_core.consumers.runner import consumer_statuses

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
            isolated_executors=tuple(
                sorted(registered_isolated_executors(), key=lambda item: item.encode("utf-8"))
            ),
        ),
        compiler_coordinate=current.rule_digest,
        compiler_revision=COMPILER_REVISION_LABELS[current],
        hosts=hosts,
        consumers=consumer_statuses(get_playbill_manager()),
    )


def server_restart() -> contracts.ServerRestartResult:
    """Schedule an in-place daemon re-exec."""

    check_permission("cruxible_server_restart")
    require_unscoped_operator("cruxible_server_restart")
    from cruxible_core.server.restart import PROCESS_BOOT_ID, schedule_server_restart

    schedule_server_restart()
    return contracts.ServerRestartResult(
        scheduled=True,
        version=__version__,
        state_root=str(get_server_state_root()),
        boot_id=PROCESS_BOOT_ID,
    )


def server_stop() -> contracts.ServerStopResult:
    """Schedule a graceful daemon shutdown that releases the state-root lock."""

    check_permission("cruxible_server_stop")
    require_unscoped_operator("cruxible_server_stop")
    from cruxible_core.server.shutdown import schedule_server_stop

    schedule_server_stop()
    return contracts.ServerStopResult(
        scheduled=True,
        version=__version__,
        state_root=str(get_server_state_root()),
        pid=os.getpid(),
    )


__all__ = [
    "attach_workspace",
    "create_playbill_host",
    "playbill_host_workspace_attach",
    "playbill_host_workspace_detach",
    "playbill_host_workspace_registration",
    "show_playbill_host",
    "server_info",
    "server_restart",
    "server_stop",
]
