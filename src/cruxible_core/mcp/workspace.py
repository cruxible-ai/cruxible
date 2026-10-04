"""Operator-configured local workspace owned by the stdio MCP adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from cruxible_client.contracts.workspace_layout import (
    ensure_workspace_directory,
    workspace_directory_conflict,
)
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.floor.workspace_advertisement import containing_git_workspace_root

MCP_WORKSPACE_ROOT_ENV = "CRUXIBLE_MCP_WORKSPACE_ROOT"
MCP_KEY_DIR_ENV = "CRUXIBLE_MCP_KEY_DIR"


def mcp_workspace_root(environ: Mapping[str, str] | None = None, *, guard: bool = True) -> Path:
    """Resolve the adapter workspace; absence deliberately means process cwd.

    The root is refused when it is the home directory or holds a 0.3 instance
    (``WorkspaceDirectoryConflict``). ``guard=False`` is only for a caller that
    reads nothing under the root's ``.cruxible`` and handles a conflicting root
    itself (an optional observation, a forbidden-root list).
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
    return ensure_workspace_directory(root) if guard else root


def mcp_git_workspace_root(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the canonical worktree without escaping an explicit MCP root.

    Required, so a worktree that is no workspace (the home directory, a 0.3
    instance) refuses with ``WorkspaceDirectoryConflict`` rather than reading as
    "no worktree".
    """

    env = os.environ if environ is None else environ
    configured_root = mcp_workspace_root(env)
    git_root = containing_git_workspace_root(configured_root)
    if git_root is None:
        raise ConfigError("MCP workspace floor export must run inside one Git worktree")
    if MCP_WORKSPACE_ROOT_ENV in env and git_root != configured_root:
        raise ConfigError(
            "CRUXIBLE_MCP_WORKSPACE_ROOT must name the Git worktree root for floor operations"
        )
    return ensure_workspace_directory(git_root)


def optional_mcp_git_workspace_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """Resolve the canonical worktree, or None when the MCP root is in no Git worktree."""

    env = os.environ if environ is None else environ
    explicit = MCP_WORKSPACE_ROOT_ENV in env
    configured_root = mcp_workspace_root(env, guard=explicit)
    git_root = containing_git_workspace_root(configured_root)
    if git_root is None:
        return None
    if explicit and git_root != configured_root:
        raise ConfigError(
            "CRUXIBLE_MCP_WORKSPACE_ROOT must name the Git worktree root for floor operations"
        )
    # An implicit (process cwd) root that is no workspace means no workspace here;
    # a configured one was refused above.
    return None if workspace_directory_conflict(git_root) is not None else git_root


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
    "mcp_git_workspace_root",
    "mcp_workspace_root",
    "optional_mcp_git_workspace_root",
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
            "cruxible_approve has no local approval key: set "
            f"{MCP_KEY_DIR_ENV} in this MCP server's environment (the env block of the MCP "
            "client config) to an absolute directory outside the workspace holding "
            "<signer_id>.ed25519, as `cruxible principal add --key-dir` writes it. "
            "A remote signer uses cruxible_prepare_approval and "
            "cruxible_submit_approval instead (profile full)."
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
