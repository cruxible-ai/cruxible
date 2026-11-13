"""`cruxible_init`: make the MCP workspace folder an initialized instance, by tool alone.

The same first run as `cruxible init`, for an agent with no shell: over the
local daemon socket the adapter already finds or auto-starts, it creates the
host attached to the workspace folder (the shared resolver's root, with its
refusals: home, a filesystem root, the state root, inside a ``.cruxible``),
writes the binding, creates the owner principal and its key in the default key
directory outside the project, initializes the instance under the default
``self_approval_allowed`` policy, and records the folder identity, so the same
MCP session authors as the owner on its next call. A folder already
initialized reports its instance and changes nothing. No key material ever
leaves this process: the result names the key directory only.
"""

from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from cruxible_client import CruxibleClient, contracts
from cruxible_client.authoring.context import ContextResolutionError, resolve_context
from cruxible_client.authoring.folder_identity import (
    folder_identity_path,
    read_folder_identity,
    write_folder_identity,
)
from cruxible_client.authoring.workspace import (
    validate_workspace_config_write,
    write_workspace_config,
)
from cruxible_client.authoring.world_stub import render_world_stub_for, write_world_stub
from cruxible_client.contracts.approval_policy import ApprovalPolicyMode
from cruxible_client.contracts.principals import is_canonical_principal_id
from cruxible_client.errors import CoreError as ClientCoreError
from cruxible_core.cli.principal_settings import default_key_dir, write_principal_settings
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.governance.init_custody import prepare_init_custody
from cruxible_core.governance.keys import validate_client_principal_key_target
from cruxible_core.mcp.daemon import DaemonTarget
from cruxible_core.mcp.results import McpInitResult
from cruxible_core.mcp.workspace import mcp_custody_forbidden_roots, mcp_floor_workspace_root
from cruxible_core.server.config import get_runtime_bearer_token, get_server_state_root

ResultT = TypeVar("ResultT")
DaemonCall = Callable[[Callable[[CruxibleClient], ResultT], str], ResultT]

_TARGET_ENV = ("CRUXIBLE_SERVER_URL", "CRUXIBLE_SERVER_SOCKET", "CRUXIBLE_INSTANCE_ID")


def _socket(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def default_principal_id() -> str:
    """The owner a bare init makes: the OS username, lowercased, as `cruxible init`."""

    try:
        name = getpass.getuser().strip().lower()
    except (KeyError, OSError):  # pragma: no cover - no passwd entry and no LOGNAME/USER
        name = ""
    if not is_canonical_principal_id(name):
        raise DataValidationError(
            f"cruxible_init: your OS username {name!r} is not a principal ID (a lowercase "
            "letter, then letters, digits, '_', '.' or '-'); repair: pass principal_id"
        )
    return name


def _bound_instance(root: Path, socket: str) -> str | None:
    """The instance this folder's binding names on this socket, if any.

    A binding naming another daemon refuses: the folder belongs to that one.
    """

    import os

    bare = {key: value for key, value in os.environ.items() if key not in _TARGET_ENV}
    try:
        binding = resolve_context(environ=bare, remembered={}, workspace=root)
    except ContextResolutionError as exc:
        raise ConfigError(f"cruxible_init: {exc}") from exc
    if binding.instance_source != "workspace" or binding.instance_id is None:
        return None
    bound = binding.server_socket
    if bound is None or _socket(bound) != socket:
        raise ConfigError(
            f"cruxible_init: {binding.workspace_binding_path} binds instance "
            f"{binding.instance_id} on {binding.server_url or bound}, not this server's "
            f"daemon (unix socket {socket}); nothing was written. Repair: run this MCP "
            "server against that daemon, or attach the folder elsewhere with "
            "`cruxible workspace attach --replace`"
        )
    return binding.instance_id


def _approval_policy(client: CruxibleClient, instance_id: str) -> ApprovalPolicyMode | None:
    """The accepted approval policy mode, from orient's policies section; None if unread."""

    try:
        page = client.orient(instance_id, section="policies")
    except (ClientCoreError, ValueError):
        return None
    return _find_mode(page.model_dump(mode="json"))


def _find_mode(value: Any) -> ApprovalPolicyMode | None:
    if isinstance(value, dict):
        mode = value.get("mode")
        if mode in ("self_approval_allowed", "independent_approval_required"):
            return mode  # type: ignore[no-any-return]
        for child in value.values():
            found = _find_mode(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_mode(child)
            if found is not None:
                return found
    return None


def run_mcp_init(
    *,
    principal_id: str | None,
    target: DaemonTarget,
    call: DaemonCall[Any],
) -> McpInitResult:
    """Initialize the MCP workspace folder; report its instance when it already is."""

    root = mcp_floor_workspace_root()
    if target.server_socket is None:
        raise ConfigError(
            "cruxible_init attaches the folder over a local daemon socket; this MCP server's "
            f"daemon is {target.server_url}. Repair: on a remote daemon an operator creates "
            "the host and credentials (`cruxible host create`, `cruxible init`)"
        )
    if get_runtime_bearer_token() is not None:
        raise ConfigError(
            "cruxible_init runs with daemon auth off; CRUXIBLE_SERVER_BEARER_TOKEN is set in "
            "this MCP server's environment, so nothing was created. Repair: unset it for a "
            "local socket daemon, or initialize with `cruxible init` and its bootstrap secret"
        )
    socket = _socket(target.server_socket)
    if principal_id is not None and not is_canonical_principal_id(principal_id):
        raise DataValidationError(
            f"cruxible_init: principal_id {principal_id!r} is not a principal ID (a lowercase "
            "letter, then letters, digits, '_', '.' or '-')"
        )
    selected = _bound_instance(root, socket)
    if selected is not None:
        bound: str = selected
        inspection: contracts.HostInspection | None
        try:
            inspection = call(lambda client: client.show_host(bound), "cruxible_init")
        except ClientCoreError:
            inspection = None
        if inspection is not None and inspection.compatibility != "uninitialized":
            record = read_folder_identity(root)
            return McpInitResult(
                status="already_initialized",
                instance_id=bound,
                principal_id=None if record is None else record.principal_id,
                key_dir=None if record is None else record.key_dir,
                approval_policy=call(
                    lambda client: _approval_policy(client, bound), "cruxible_init"
                ),
                workspace_root=str(root),
                folder_identity_path=None if record is None else str(folder_identity_path(root)),
            )
    owner = principal_id or default_principal_id()
    validate_workspace_config_write(root, instance_id=selected, server_socket=socket)
    roots = tuple(
        dict.fromkeys(
            path.expanduser().resolve()
            for path in (*mcp_custody_forbidden_roots(), get_server_state_root())
        )
    )
    if selected is not None:
        # A retry of an interrupted init resumes with its marker; key material
        # left without one is refused by the custody check below.
        validate_client_principal_key_target(
            default_key_dir(selected, owner),
            principal_id=owner,
            kind="ordinary",
            forbidden_roots=roots,
        )
    else:
        created = call(
            lambda client: client.create_host(workspace_root=str(root), dry_run=False),
            "cruxible_init",
        )
        selected = created.instance_id
        # Bound at once, so a retry after a failure initializes this same host.
        write_workspace_config(root, instance_id=selected, server_socket=socket)
    instance_id = selected
    key_dir = default_key_dir(instance_id, owner)
    materials, markers = prepare_init_custody(
        transport=f"unix://{socket}",
        instance_id=instance_id,
        specifications=((key_dir, owner, "ordinary"),),
        forbidden_roots=roots,
    )
    material = materials[0]
    # With daemon auth off the init request claims the owner it creates, as
    # `cruxible init` does; this one client carries that claim.
    with CruxibleClient(socket_path=socket, principal_id=owner) as client:
        result = client.init(
            instance_id,
            principals=[material.principal.model_dump(mode="json")],
            operating_profile="local",
            workspace_root=str(root),
        )
    write_workspace_config(root, instance_id=result.instance_id, server_socket=socket)
    for marker in markers:
        marker.unlink()
    write_principal_settings(
        key_dir,
        ctx_obj={"server_socket": socket},
        instance_id=result.instance_id,
        principal_id=owner,
        private_key_path=material.private_key_path,
        token=None,
        written_by="cruxible_init",
    )
    identity_path, _status = write_folder_identity(
        root, instance_id=result.instance_id, principal_id=owner, key_dir=key_dir, replace=True
    )
    rendered = call(
        lambda client: render_world_stub_for(client, result.instance_id, workspace=root),
        "cruxible_init",
    )
    write_world_stub(root, rendered)
    return McpInitResult(
        status="initialized",
        instance_id=result.instance_id,
        principal_id=owner,
        key_dir=str(key_dir),
        approval_policy=result.approval_policy_mode,
        workspace_root=str(root),
        folder_identity_path=str(identity_path),
    )


__all__ = ["default_principal_id", "run_mcp_init"]
