"""Operator-configured local workspace owned by the stdio MCP adapter."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from cruxible_core.errors import ConfigError, DataValidationError

MCP_WORKSPACE_ROOT_ENV = "CRUXIBLE_MCP_WORKSPACE_ROOT"


def mcp_workspace_root(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the adapter workspace; absence deliberately means process cwd."""

    env = environ or os.environ
    raw = env.get(MCP_WORKSPACE_ROOT_ENV)
    candidate = Path.cwd() if raw is None else Path(raw).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ConfigError(f"MCP workspace root is unavailable: {candidate}: {exc}") from exc
    if not root.is_dir():
        raise ConfigError(f"MCP workspace root is not a directory: {root}")
    return root


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
    workspace = mcp_workspace_root() if root is None else root.resolve(strict=True)
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


__all__ = ["MCP_WORKSPACE_ROOT_ENV", "mcp_workspace_root", "resolve_workspace_path"]
