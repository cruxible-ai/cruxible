"""Shared server-mode configuration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cruxible_core.errors import ConfigError


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_server_required(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether local adapters must use a configured server transport."""
    env = environ or os.environ
    return _is_truthy(env.get("CRUXIBLE_REQUIRE_SERVER"))


@dataclass(frozen=True)
class ServerSettings:
    """Resolved server transport settings."""

    require_server: bool = False
    server_url: str | None = None
    server_socket: str | None = None

    @property
    def enabled(self) -> bool:
        return self.server_url is not None or self.server_socket is not None


def resolve_server_settings(
    *,
    server_url: str | None = None,
    server_socket: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> ServerSettings:
    """Resolve and validate server transport settings from env/overrides."""
    env = environ or os.environ

    resolved_url = server_url if server_url is not None else env.get("CRUXIBLE_SERVER_URL")
    resolved_socket = (
        server_socket if server_socket is not None else env.get("CRUXIBLE_SERVER_SOCKET")
    )
    require_server = is_server_required(env)

    if resolved_url and resolved_socket:
        raise ConfigError(
            "Configure exactly one of CRUXIBLE_SERVER_URL or CRUXIBLE_SERVER_SOCKET, not both"
        )
    if require_server and not (resolved_url or resolved_socket):
        raise ConfigError(
            "Server mode is required. Set CRUXIBLE_SERVER_SOCKET or CRUXIBLE_SERVER_URL."
        )

    return ServerSettings(
        require_server=require_server,
        server_url=resolved_url,
        server_socket=resolved_socket,
    )


def get_server_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the server-owned state directory."""
    env = environ or os.environ
    raw = env.get("CRUXIBLE_SERVER_STATE_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".cruxible" / "server").resolve()


def is_server_auth_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether bearer-token auth is enabled for the HTTP server."""
    env = environ or os.environ
    return _is_truthy(env.get("CRUXIBLE_SERVER_AUTH"))


def get_server_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured legacy bearer token, if any."""
    env = environ or os.environ
    token = env.get("CRUXIBLE_SERVER_TOKEN")
    if token:
        return token
    return None


def get_runtime_bearer_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured runtime bearer credential for CLI/MCP clients.

    ``CRUXIBLE_SERVER_BEARER_TOKEN`` is the preferred env var. ``CRUXIBLE_SERVER_TOKEN``
    remains as a backward-compatible alias for local/dev workflows.
    """
    env = environ or os.environ
    token = env.get("CRUXIBLE_SERVER_BEARER_TOKEN") or env.get("CRUXIBLE_SERVER_TOKEN")
    if token:
        return token
    return None
