"""Operator-configured local workspace owned by the stdio MCP adapter."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from cruxible_client.contracts.workspace_layout import (
    WorkspaceDirectoryConflict,
    ensure_workspace_directory,
)
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root
from cruxible_core.workspace_root import WorkspaceBoundaryRefused, resolve_workspace_root

MCP_WORKSPACE_ROOT_ENV = "CRUXIBLE_MCP_WORKSPACE_ROOT"
MCP_KEY_DIR_ENV = "CRUXIBLE_MCP_KEY_DIR"

_CLIENT_ROOTS: ContextVar[tuple[Path, ...] | None] = ContextVar(
    "cruxible_mcp_client_roots",
    default=None,
)


class PluginDirectoryConflict(ConfigError):
    """The implicit MCP cwd is a Cruxible plugin package directory, not a project."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        super().__init__(
            f"MCP server cwd is a Cruxible plugin directory ({directory}), not a project "
            "workspace; nothing was read or written there. Repair: configure your MCP client "
            f"to provide workspace roots, or set {MCP_WORKSPACE_ROOT_ENV}=/path/to/your/project "
            "in the MCP server environment."
        )


def file_uri_to_path(uri: str) -> Path | None:
    """Resolve one local ``file://`` MCP root URI to an existing directory, or None."""

    parsed = urlparse(uri)
    if parsed.scheme.lower() != "file" or parsed.netloc not in ("", "localhost"):
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return None
    if os.name == "nt" and len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    try:
        resolved = Path(raw_path).resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


@contextmanager
def client_roots_scope(roots: Sequence[Path] | None) -> Iterator[None]:
    """Bind MCP client-provided workspace roots for the current tool call."""

    value = tuple(roots) if roots else None
    token = _CLIENT_ROOTS.set(value)
    try:
        yield
    finally:
        _CLIENT_ROOTS.reset(token)


def _is_cruxible_plugin_package(directory: Path) -> bool:
    """True when ``directory`` is an installed or bundled Cruxible agent plugin package."""

    for manifest, mcp_cfg in (
        (directory / "plugin.json", directory / "mcp.json"),
        (directory / ".claude-plugin" / "plugin.json", directory / ".mcp.json"),
    ):
        if not (manifest.is_file() and mcp_cfg.is_file()):
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == "cruxible":
            return True
    return False


def _enclosing_plugin_directory(directory: Path) -> Path | None:
    for candidate in (directory, *directory.parents):
        if _is_cruxible_plugin_package(candidate):
            return candidate
    return None


def _resolve_implicit_root(candidate: Path, *, guard: bool) -> Path:
    plugin_dir = _enclosing_plugin_directory(candidate)
    if plugin_dir is not None:
        if guard:
            raise PluginDirectoryConflict(plugin_dir)
        return candidate
    try:
        found = resolve_workspace_root(candidate)
    except (WorkspaceDirectoryConflict, WorkspaceBoundaryRefused):
        if guard:
            raise
        return candidate
    assert found is not None
    return found.root


def _resolve_client_roots(roots: tuple[Path, ...], *, guard: bool) -> Path | None:
    """Pick the workspace root from MCP client ``roots``: bound ``.cruxible/`` first."""

    usable: list[Path] = []
    first_error: Exception | None = None
    for candidate in roots:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        try:
            workspace = _resolve_implicit_root(resolved, guard=guard)
        except (
            WorkspaceDirectoryConflict,
            WorkspaceBoundaryRefused,
            PluginDirectoryConflict,
        ) as exc:
            if first_error is None:
                first_error = exc
            continue
        if (workspace / ".cruxible").is_dir():
            return workspace
        usable.append(workspace)
    if usable:
        return usable[0]
    if first_error is not None and guard:
        raise first_error
    return None


def mcp_workspace_root(environ: Mapping[str, str] | None = None, *, guard: bool = True) -> Path:
    """Resolve the adapter workspace: ``CRUXIBLE_MCP_WORKSPACE_ROOT``, client roots, else cwd.

    Precedence:
    1. ``CRUXIBLE_MCP_WORKSPACE_ROOT`` when set in the environment.
    2. MCP client ``roots`` provided by the host session (preferring a root
       whose resolved workspace holds ``.cruxible/``).
    3. The process cwd, resolved the way the CLI resolves it
       (`cruxible_core.workspace_root`): the nearest ancestor holding
       ``.cruxible/``, else the enclosing Git worktree, else cwd itself.

    An implicit root is refused when it is a Cruxible plugin package directory
    (``PluginDirectoryConflict``), the home directory, a filesystem root, the
    state root, a 0.3 instance or inside a ``.cruxible`` directory
    (``WorkspaceDirectoryConflict``). ``guard=False`` is only for a caller that
    reads nothing under the root's ``.cruxible`` and handles a conflicting root
    itself (an optional observation, a forbidden-root list); it gets the bare
    start directory when nothing usable is found.
    """

    env = os.environ if environ is None else environ
    raw = env.get(MCP_WORKSPACE_ROOT_ENV)
    if raw is not None:
        candidate = Path(raw).expanduser()
        try:
            root = candidate.resolve(strict=True)
        except OSError as exc:
            raise ConfigError(f"MCP workspace root is unavailable: {candidate}: {exc}") from exc
        if not root.is_dir():
            raise ConfigError(f"MCP workspace root is not a directory: {root}")
        return ensure_workspace_directory(root) if guard else root
    client_roots = _CLIENT_ROOTS.get()
    if client_roots:
        chosen = _resolve_client_roots(client_roots, guard=guard)
        if chosen is not None:
            return chosen
    candidate = Path.cwd()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"MCP workspace root is unavailable: {candidate}: {exc}") from exc
    if not root.is_dir():
        raise ConfigError(f"MCP workspace root is not a directory: {root}")
    return _resolve_implicit_root(root, guard=guard)


def mcp_floor_workspace_root(environ: Mapping[str, str] | None = None) -> Path:
    """The workspace floor operations write under; any folder, Git or not.

    Required, so a root that is no workspace (the home directory, a 0.3
    instance) refuses with ``WorkspaceDirectoryConflict`` rather than reading
    as "no workspace".
    """

    env = os.environ if environ is None else environ
    root = mcp_workspace_root(env)
    if MCP_WORKSPACE_ROOT_ENV in env:
        _refuse_nested_configured_root(root)
    return root


def _refuse_nested_configured_root(root: Path) -> None:
    """A configured root must be the workspace root the shared resolver finds from it.

    A directory inside a Git worktree or below a folder holding ``.cruxible/``
    is not a second workspace: floor operations there would write a nested one.
    """

    found = resolve_workspace_root(root)
    if found is not None and found.root != root:
        raise ConfigError(
            "CRUXIBLE_MCP_WORKSPACE_ROOT must name the workspace root for floor operations "
            f"(the Git worktree root, or the folder holding .cruxible/): {found.root}"
        )


def optional_mcp_workspace_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The MCP workspace, or None when the implicit (cwd) start is no usable root.

    A configured root that is no workspace is refused; an implicit one means
    there is no workspace here.
    """

    env = os.environ if environ is None else environ
    if MCP_WORKSPACE_ROOT_ENV in env:
        root = mcp_workspace_root(env)
        _refuse_nested_configured_root(root)
        return root
    try:
        return mcp_workspace_root(env)
    except (WorkspaceDirectoryConflict, PluginDirectoryConflict):
        return None


def resolve_workspace_path(
    value: str,
    *,
    root: Path | None = None,
    kind: str = "any",
) -> Path:
    """Resolve one normalized relative path without allowing a symlink escape."""

    pure = PurePosixPath(value)
    if not value or pure.is_absolute() or pure.as_posix() != value or ".." in pure.parts:
        raise DataValidationError("workspace path must be normalized, relative POSIX text")
    workspace = (
        mcp_workspace_root()
        if root is None
        else ensure_workspace_directory(root.resolve(strict=True))
    )
    try:
        resolved = (workspace / value).resolve(strict=kind in {"file", "directory"})
    except OSError as exc:
        raise DataValidationError(f"workspace path is unavailable: {value}: {exc}") from exc
    if not resolved.is_relative_to(workspace):
        raise DataValidationError(f"workspace path escapes the configured root: {value}")
    if kind == "file" and not resolved.is_file():
        raise DataValidationError(f"workspace path is not a file: {value}")
    if kind == "directory" and not resolved.is_dir():
        raise DataValidationError(f"workspace path is not a directory: {value}")
    return resolved


__all__ = [
    "MCP_WORKSPACE_ROOT_ENV",
    "PluginDirectoryConflict",
    "client_roots_scope",
    "file_uri_to_path",
    "mcp_floor_workspace_root",
    "mcp_workspace_root",
    "optional_mcp_workspace_root",
    "resolve_workspace_path",
]


def mcp_approval_key_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The operator-configured directory of local approval keys, `<signer_id>.ed25519`.

    Only the server's environment names it; no tool argument can point signing
    at another path.
    """

    env = os.environ if environ is None else environ
    raw = env.get(MCP_KEY_DIR_ENV)
    if not raw:
        raise ConfigError(
            "cruxible_proposal_approve has no local approval key: run `cruxible init` in "
            "this folder (an MCP server started there signs as its owner), or set "
            f"{MCP_KEY_DIR_ENV} in this MCP server's environment (the env block of the MCP "
            "client config) to an absolute directory outside the workspace holding "
            "<signer_id>.ed25519, as `cruxible principal add --key-dir` writes it. "
            "A remote signer uses cruxible_proposal_approve_prepare and "
            "cruxible_proposal_approve_submit instead (profile full)."
        )
    directory = Path(raw).expanduser()
    if not directory.is_absolute():
        raise ConfigError(f"{MCP_KEY_DIR_ENV} must be an absolute directory path")
    if not directory.is_dir():
        raise ConfigError(f"{MCP_KEY_DIR_ENV} is not a directory: {directory}")
    return directory


def mcp_custody_forbidden_roots(environ: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Workspace roots a local signing key must stay outside, as the CLI refuses.

    A forbidden-root list reads nothing under the root, so it is unguarded.
    """

    root = mcp_workspace_root(environ, guard=False)
    git_root = containing_git_workspace_root(root)
    return (root,) if git_root is None or git_root == root else (root, git_root)
