"""The instance a stdio MCP adapter acts on when a tool call names none."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.authoring.context import (
    ContextResolutionError,
    resolve_context,
)
from cruxible_core.errors import ConfigError
from cruxible_core.mcp.workspace import mcp_workspace_root

MCP_INSTANCE_ENV = "CRUXIBLE_INSTANCE_ID"
_TARGET_ENV = ("CRUXIBLE_SERVER_URL", "CRUXIBLE_SERVER_SOCKET", MCP_INSTANCE_ENV)

NO_INSTANCE_MESSAGE = (
    "No Cruxible instance selected: pass instance_id, or launch the MCP server with "
    f"{MCP_INSTANCE_ENV}=<instance id> in its environment (the `env` block of the MCP "
    "client config), or run it in a workspace whose .playbill/coverage.json binds an "
    "instance on this server's daemon."
)


def _transport(server_url: object, server_socket: object) -> str | None:
    if isinstance(server_url, str) and server_url:
        return server_url.rstrip("/")
    if isinstance(server_socket, str) and server_socket:
        return f"unix://{Path(server_socket).expanduser().resolve()}"
    return None


def _workspace_instance_id(env: Mapping[str, str]) -> str | None:
    """The instance the MCP workspace binds, when it lives on this adapter's daemon.

    The adapter's daemon comes only from its own environment, so a binding that
    names another daemon is refused rather than followed.
    """

    bare = {key: value for key, value in env.items() if key not in _TARGET_ENV}
    try:
        binding = resolve_context(
            environ=bare,
            remembered={},
            workspace=mcp_workspace_root(env),
        )
    except (
        ContextResolutionError,
        ConfigError,
        OSError,
        RuntimeError,
        UnicodeError,
        ValueError,
    ):
        # An unreadable or malformed binding selects nothing; it never breaks tools.
        return None
    if binding.instance_source != "workspace" or binding.instance_id is None:
        return None
    try:
        bound = _transport(binding.server_url, binding.server_socket)
        adapter = _transport(env.get("CRUXIBLE_SERVER_URL"), env.get("CRUXIBLE_SERVER_SOCKET"))
    except (OSError, RuntimeError, ValueError):
        return None
    if adapter is None or adapter != bound:
        raise ConfigError(
            f"The workspace binding {binding.workspace_binding_path} selects instance "
            f"{binding.instance_id} on {bound}, but this MCP server's daemon is "
            f"{adapter or 'not configured'}. Pass instance_id, or set {MCP_INSTANCE_ENV}."
        )
    return binding.instance_id


def configured_instance_id(
    instance_id: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve explicit > environment > workspace binding.

    Remembered CLI context never retargets the adapter. The MCP workspace's
    binding counts only when it names an instance on this adapter's own daemon.
    """

    try:
        resolved = resolve_context(
            instance_id=instance_id,
            environ=environ,
            remembered={},
            no_workspace=True,
        )
    except ContextResolutionError as exc:
        raise ConfigError(f"{exc}. {NO_INSTANCE_MESSAGE}") from exc
    if resolved.instance_id is not None:
        return resolved.instance_id
    return _workspace_instance_id(os.environ if environ is None else environ)


def require_instance_id(
    instance_id: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the instance a tool acts on, or refuse with the repair."""

    resolved = configured_instance_id(instance_id, environ=environ)
    if resolved is None:
        raise ConfigError(NO_INSTANCE_MESSAGE)
    return resolved


__all__ = [
    "MCP_INSTANCE_ENV",
    "NO_INSTANCE_MESSAGE",
    "configured_instance_id",
    "require_instance_id",
]
