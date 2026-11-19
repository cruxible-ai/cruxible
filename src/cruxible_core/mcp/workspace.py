"""Operator-configured local workspace owned by the stdio MCP adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from cruxible_client.contracts.workspace_layout import (
    WorkspaceDirectoryConflict,
    ensure_workspace_directory,
)
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root
from cruxible_core.workspace_root import WorkspaceBoundaryRefused, resolve_workspace_root

MCP_WORKSPACE_ROOT_ENV = "CRUXIBLE_MCP_WORKSPACE_ROOT"
MCP_KEY_DIR_ENV = "CRUXIBLE_MCP_KEY_DIR"


def mcp_workspace_root(environ: Mapping[str, str] | None = None, *, guard: bool = True) -> Path:
    """Resolve the adapter workspace: ``CRUXIBLE_MCP_WORKSPACE_ROOT``, else found from cwd.

    With no configured root the process cwd is the start, resolved the way the
    CLI resolves it (`cruxible_core.workspace_root`): the nearest ancestor
    holding ``.cruxible/``, else the enclosing Git worktree, else cwd itself.
    The root is refused when it is the home directory, a filesystem root, the
    state root, a 0.3 instance or inside a ``.cruxible`` directory
    (``WorkspaceDirectoryConflict``). ``guard=False`` is only for a caller that
    reads nothing under the root's ``.cruxible`` and handles a conflicting root
    itself (an optional observation, a forbidden-root list); it gets the bare
    cwd when nothing usable is found.
    """

    env = os.environ if environ is None else environ
    raw = env.get(MCP_WORKSPACE_ROOT_ENV)
    candidate = Path.cwd() if raw is None else Path(raw).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"MCP workspace root is unavailable: {candidate}: {exc}") from exc
    if not root.is_dir():
        raise ConfigError(f"MCP workspace root is not a directory: {root}")
    if raw is not None:
        return ensure_workspace_directory(root) if guard else root
    try:
        found = resolve_workspace_root(root)
    except (WorkspaceDirectoryConflict, WorkspaceBoundaryRefused):
        if guard:
            raise
        return root
    assert found is not None
    return found.root


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
    except WorkspaceDirectoryConflict:
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
