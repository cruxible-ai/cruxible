"""Who the MCP server acts as: its environment, else the folder's record, else no one.

Precedence, first that applies:

1. ``environment``: any of ``CRUXIBLE_PRINCIPAL_ID``, ``CRUXIBLE_PRINCIPAL_KEY``,
   ``CRUXIBLE_MCP_KEY_DIR`` or a bearer token in the server's own environment.
   The environment then decides alone; nothing is mixed in from the folder.
2. ``folder``: the workspace the shared resolver chooses binds an instance on
   this server's daemon over a local socket, and its ``.cruxible/identity.json``
   (written by ``cruxible init``) names that instance's principal and key
   directory. The record is checked against the daemon's principal registry
   and the key's custody before the server acts as that principal; a record
   that cannot be used refuses with its typed error
   (`cruxible_client.authoring.folder_identity`), never reads as "no one".
3. ``none``: the implicit local operator, which cannot author.

A TCP daemon (``CRUXIBLE_SERVER_URL`` or a binding naming a URL) ignores the
record: there the bearer credential decides who acts.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.authoring.context import ContextResolutionError, resolve_context
from cruxible_client.authoring.folder_identity import (
    FolderIdentity,
    FolderIdentityRefused,
    IdentitySource,
    VerifiedFolderIdentity,
    folder_identity_path,
    folder_key_custody,
    read_folder_identity,
    verify_folder_identity,
)
from cruxible_client.contracts.principals import PRINCIPAL_ID_ENV, PRINCIPAL_KEY_ENV
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.workspace_layout import WorkspaceDirectoryConflict
from cruxible_client.transport.http import configured_principal_id
from cruxible_core.errors import ConfigError
from cruxible_core.mcp.daemon import DaemonTarget
from cruxible_core.mcp.workspace import (
    MCP_KEY_DIR_ENV,
    mcp_custody_forbidden_roots,
    mcp_workspace_root,
)
from cruxible_core.workspace_root import WorkspaceBoundaryRefused

#: Variables that make the environment the identity's only source.
IDENTITY_ENV = (PRINCIPAL_ID_ENV, PRINCIPAL_KEY_ENV, MCP_KEY_DIR_ENV)
_TARGET_ENV = ("CRUXIBLE_SERVER_URL", "CRUXIBLE_SERVER_SOCKET", "CRUXIBLE_INSTANCE_ID")


@dataclass(frozen=True)
class FolderCandidate:
    """A folder record found at the bound workspace, not yet checked against the daemon."""

    record: FolderIdentity
    path: Path


@dataclass(frozen=True)
class McpIdentity:
    """The principal this server acts as and where that came from."""

    source: IdentitySource
    principal_id: str | None
    #: Set when ``source`` is ``folder``, once the daemon's registry confirmed it.
    folder: VerifiedFolderIdentity | None = None


def environment_names_identity(environ: Mapping[str, str], *, token: str | None) -> bool:
    return token is not None or any((environ.get(name) or "").strip() for name in IDENTITY_ENV)


def _socket(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def folder_candidate(environ: Mapping[str, str], *, target: DaemonTarget) -> FolderCandidate | None:
    """The bound workspace's record, when this server's daemon is that binding's socket.

    The record is read only at the root the shared resolver chooses
    (`mcp_workspace_root`); an other-owned marker refuses
    (``WorkspaceBoundaryRefused``). A root that is no workspace, an unbound
    folder, a binding naming another daemon, or a TCP daemon selects none.
    """

    if target.server_socket is None:
        return None
    try:
        root = mcp_workspace_root(environ)
    except WorkspaceBoundaryRefused:
        raise
    except (WorkspaceDirectoryConflict, ConfigError):
        return None
    bare = {key: value for key, value in environ.items() if key not in _TARGET_ENV}
    try:
        binding = resolve_context(environ=bare, remembered={}, workspace=root)
    except WorkspaceBoundaryRefused:
        raise
    except (ContextResolutionError, ConfigError, OSError, RuntimeError, ValueError):
        # An unreadable binding selects no instance (`mcp.target`), so no identity.
        return None
    if (
        binding.instance_source != "workspace"
        or binding.instance_id is None
        or binding.server_socket is None
    ):
        return None
    try:
        if _socket(binding.server_socket) != _socket(target.server_socket):
            return None
    except (OSError, RuntimeError):
        return None
    path = folder_identity_path(binding.workspace)
    record = read_folder_identity(binding.workspace)
    if record is None:
        return None
    if record.instance_id != binding.instance_id:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid",
            path=path,
            detail=(
                f"it names instance {record.instance_id}, but this folder's binding selects "
                f"{binding.instance_id}"
            ),
        )
    return FolderCandidate(record=record, path=path)


def local_identity(
    environ: Mapping[str, str] | None = None,
    *,
    target: DaemonTarget,
    token: str | None,
) -> tuple[IdentitySource, str | None, FolderCandidate | None]:
    """Where the identity comes from and the principal ID to claim, before any daemon check."""

    env = os.environ if environ is None else environ
    if environment_names_identity(env, token=token):
        return "environment", configured_principal_id(env), None
    candidate = folder_candidate(env, target=target)
    if candidate is None:
        return "none", None, None
    return "folder", candidate.record.principal_id, candidate


_verified_lock = threading.Lock()
_verified: dict[tuple[object, ...], VerifiedFolderIdentity] = {}


def forget_verified_identities() -> None:
    with _verified_lock:
        _verified.clear()


def verified_folder_identity(
    candidate: FolderCandidate,
    *,
    target: DaemonTarget,
    principals: Callable[[str], Sequence[PrincipalRecord]],
    environ: Mapping[str, str] | None = None,
) -> VerifiedFolderIdentity:
    """Check the key's custody on every call; its public-key match once per custody state.

    The custody check (`folder_key_custody`) is one ``lstat`` of the key and
    one of its directory, so a key made unreadable, or opened to group or world,
    or handed to another owner refuses on the next tool call. Only the registry
    read and the public-key comparison are cached, keyed on that custody stamp.
    """

    stamp = folder_key_custody(candidate.record, path=candidate.path)
    cache_key = (target.server_socket, candidate.path, candidate.record, *stamp)
    with _verified_lock:
        cached = _verified.get(cache_key)
    if cached is not None:
        return cached
    verified = verify_folder_identity(
        candidate.record,
        path=candidate.path,
        principals=principals(candidate.record.instance_id),
        forbidden_roots=mcp_custody_forbidden_roots(environ),
    )
    with _verified_lock:
        _verified[cache_key] = verified
    return verified


__all__ = [
    "IDENTITY_ENV",
    "FolderCandidate",
    "McpIdentity",
    "environment_names_identity",
    "folder_candidate",
    "forget_verified_identities",
    "local_identity",
    "verified_folder_identity",
]
