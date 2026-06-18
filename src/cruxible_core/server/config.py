"""Shared server-mode configuration helpers."""

from __future__ import annotations

import ipaddress
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


def get_runtime_bootstrap_secret(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured one-time runtime bootstrap secret, if any."""
    env = environ or os.environ
    secret = env.get("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET")
    if secret and secret.strip():
        return secret.strip()
    return None


def get_runtime_bearer_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the configured runtime bearer credential for CLI/MCP clients."""
    env = environ or os.environ
    token = env.get("CRUXIBLE_SERVER_BEARER_TOKEN")
    if token:
        return token
    return None


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_server_startup_settings(
    environ: Mapping[str, str] | None = None,
    *,
    runtime_credentials_available: bool = False,
    auth_required: bool = False,
) -> None:
    """Validate HTTP server startup settings that are unsafe when miscombined."""
    env = environ or os.environ

    auth_enabled = is_server_auth_enabled(env)
    bootstrap_secret = get_runtime_bootstrap_secret(env)

    if auth_required and not auth_enabled:
        raise ConfigError(
            "Refusing to start cruxible-server without auth because this server "
            "state dir previously required auth. Set CRUXIBLE_SERVER_AUTH=true."
        )

    if (
        auth_enabled
        and bootstrap_secret is None
        and not runtime_credentials_available
    ):
        raise ConfigError(
            "CRUXIBLE_SERVER_AUTH=true requires CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET "
            "or stored runtime credentials."
        )

    if env.get("CRUXIBLE_SERVER_SOCKET"):
        return

    host = env.get("CRUXIBLE_HOST", "127.0.0.1")
    if not _is_loopback_host(host) and not auth_enabled:
        raise ConfigError(
            "Refusing to bind cruxible-server to a non-loopback host without auth. "
            "Set CRUXIBLE_SERVER_AUTH=true with CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET "
            "or stored runtime credentials, "
            "or bind CRUXIBLE_HOST to 127.0.0.1/localhost."
        )
