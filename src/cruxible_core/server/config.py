"""Shared server-mode configuration helpers."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from cruxible_core.errors import ConfigError

_VOLATILE_STATE_ROOTS = (
    Path("/tmp"),
    Path("/private/tmp"),
    Path("/var/tmp"),
    Path("/private/var/tmp"),
    Path("/private/var/folders"),
)


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


def get_server_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the durable server request log path."""
    env = environ or os.environ
    raw = env.get("CRUXIBLE_SERVER_LOG_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return (get_server_state_dir(env) / "logs" / "server.log").resolve()


def is_volatile_state_path(path: str | Path) -> bool:
    """Return whether *path* resolves under a known volatile temp location."""
    resolved = Path(path).expanduser().resolve()
    for root in _VOLATILE_STATE_ROOTS:
        volatile_root = root.resolve()
        if resolved == volatile_root or resolved.is_relative_to(volatile_root):
            return True
    return False


def volatile_state_path_warnings(
    *,
    environ: Mapping[str, str] | None = None,
    instance_locations: Iterable[tuple[str, str]] = (),
) -> list[str]:
    """Return startup warnings for durable state paths under volatile dirs."""
    state_dir = get_server_state_dir(environ)
    warnings: list[str] = []
    if is_volatile_state_path(state_dir):
        warnings.append(
            "CRUXIBLE_SERVER_STATE_DIR resolves under a volatile temp path "
            f"({state_dir}). Use a durable directory such as ~/.cruxible/server "
            "or /var/lib/cruxible for long-lived daemon state."
        )

    for instance_id, location in instance_locations:
        if is_volatile_state_path(location):
            warnings.append(
                f"Instance {instance_id} is registered under a volatile temp path "
                f"({Path(location).expanduser().resolve()}). Move or restore it to "
                "durable storage before relying on it for long-lived state."
            )
    return warnings


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


def get_origin_allowlist(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the configured extra browser-origin allowlist.

    ``CRUXIBLE_ORIGIN_ALLOWLIST`` is a comma-separated list of origins (e.g.
    ``https://console.example.com``) permitted to drive the HTTP API from a
    browser, in addition to the always-allowed loopback origins. Entries are
    normalized to ``scheme://host[:port]`` (path/query/fragment stripped) and
    lowercased on scheme+host.
    """
    env = environ or os.environ
    raw = env.get("CRUXIBLE_ORIGIN_ALLOWLIST", "")
    allowlist: list[str] = []
    for entry in raw.split(","):
        normalized = _normalize_origin(entry)
        if normalized is not None:
            allowlist.append(normalized)
    return tuple(allowlist)


def _normalize_origin(origin: str | None) -> str | None:
    """Normalize an Origin/Referer value to ``scheme://host[:port]`` or ``None``.

    Returns ``None`` for empty or unparseable values and for the literal
    ``"null"`` origin (opaque origins from sandboxed iframes / ``file://`` /
    data URIs), which must never be treated as allowlisted.
    """
    if origin is None:
        return None
    candidate = origin.strip()
    if not candidate or candidate.lower() == "null":
        return None
    split = urlsplit(candidate)
    if not split.scheme or not split.hostname:
        return None
    scheme = split.scheme.lower()
    host = split.hostname.lower()
    # urlsplit lowercases nothing but the scheme is case-insensitive; host is too.
    netloc = f"[{host}]" if ":" in host else host
    if split.port is not None:
        netloc = f"{netloc}:{split.port}"
    return f"{scheme}://{netloc}"


def _origin_host(origin: str) -> str | None:
    """Return the lowercased hostname of a normalized origin, if parseable."""
    split = urlsplit(origin)
    if not split.hostname:
        return None
    return split.hostname.lower()


def is_origin_allowed(origin: str | None, environ: Mapping[str, str] | None = None) -> bool:
    """Return whether a browser-supplied ``Origin`` may drive the HTTP API.

    Programmatic clients (CLI/SDK/curl) send no ``Origin`` header; only browsers
    attach one. The allowlist therefore exists to block the DNS-rebinding /
    malicious-webpage-hits-localhost threat without affecting non-browser clients.

    Policy:

    * No origin (``None``/empty) → ALLOW. A missing ``Origin`` is a non-browser
      (or same-origin navigation) request; rejecting it would break every CLI/SDK
      client.
    * Loopback origin (``localhost``/``127.0.0.1``/``[::1]``, any port/scheme) →
      ALLOW. This keeps the daemon-served same-origin UI and local dev working.
    * An origin in ``CRUXIBLE_ORIGIN_ALLOWLIST`` → ALLOW.
    * Anything else (a real cross-origin browser request) → REJECT.
    """
    if origin is None or not origin.strip():
        # Absent (or empty) Origin: a non-browser / same-origin navigation request.
        return True
    normalized = _normalize_origin(origin)
    if normalized is None:
        # Present but unparseable / opaque ("null") origin from a browser: reject.
        return False
    host = _origin_host(normalized)
    if host is not None and _is_loopback_host(host):
        return True
    return normalized in get_origin_allowlist(environ)


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

    if auth_enabled and bootstrap_secret is None and not runtime_credentials_available:
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
