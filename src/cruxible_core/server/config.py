"""Shared server-mode configuration helpers."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from cruxible_client.contracts.errors import PlaybillReseedRequired
from cruxible_core.errors import ConfigError

_VOLATILE_STATE_ROOTS = (
    Path("/tmp"),
    Path("/private/tmp"),
    Path("/var/tmp"),
    Path("/private/var/tmp"),
    Path("/private/var/folders"),
)


class ServerStateConfigurationError(ConfigError):
    """A server state-root setting is present but cannot be used safely."""

    error_code = "cruxible.server.state_configuration_invalid"


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


def get_server_state_root(environ: Mapping[str, str] | None = None) -> Path:
    """Return the canonical server-owned state root."""
    env = os.environ if environ is None else environ
    if "CRUXIBLE_SERVER_STATE_DIR" in env:
        raise ServerStateConfigurationError(
            "CRUXIBLE_SERVER_STATE_DIR is obsolete; use CRUXIBLE_STATE_ROOT and re-seed "
            "pre-PC-HR instances"
        )
    raw = env.get("CRUXIBLE_STATE_ROOT")
    if raw is not None:
        if not raw.strip():
            raise ServerStateConfigurationError("CRUXIBLE_STATE_ROOT may not be empty")
        state_root = Path(raw).expanduser().resolve()
    else:
        state_root = (Path.home() / ".cruxible").resolve()
    legacy = state_root / "server"
    for path in (
        legacy / "registry.db",
        legacy / "runtime_credentials.db",
        state_root / "registry.db",
        state_root / "runtime_credentials.db",
    ):
        _refuse_legacy_state_file(path)
    for entry in STATE_ROOT_OWN_ENTRIES:
        within_state_root(state_root, *entry)
    return state_root


#: Paths every daemon component opens under its state root. Each must be the
#: root's own: a copied root whose ``daemon/`` (or registry, or credential DB,
#: or ``instances/``) links back into the original would serve and write the
#: original's state.
STATE_ROOT_OWN_ENTRIES: tuple[tuple[str, ...], ...] = (
    ("daemon",),
    ("daemon", "registry.db"),
    ("daemon", "runtime_credentials.db"),
    ("instances",),
)


def contained_relative(path: Path, root: Path) -> PurePath | None:
    """``path`` relative to ``root`` when its real path lies strictly under root's.

    Both sides are resolved through every symlink first, then compared one
    component at a time with ``casefold``: a case-insensitive volume names one
    directory by many spellings, and a byte comparison would let a spelling
    other than the root's read as outside it (or, the other way round, an
    alias of the original read as inside a copy).
    """

    parts = Path(os.path.realpath(path)).parts
    root_parts = Path(os.path.realpath(root)).parts
    if len(parts) <= len(root_parts):
        return None
    if any(a.casefold() != b.casefold() for a, b in zip(root_parts, parts, strict=False)):
        return None
    return PurePath(*parts[len(root_parts) :])


def within_state_root(state_root: Path, *parts: str) -> Path:
    """``state_root / parts``, refused when any existing step resolves elsewhere.

    Each existing prefix must resolve to the same place under the state
    root's real path (compared case-insensitively). A symlink alias of the
    WHOLE state root is fine -- the root is resolved first -- but a link
    inside it that leads out of it, or onto a different entry inside it, is
    refused before anything is opened or migrated through it.
    """

    real_root = Path(os.path.realpath(state_root))
    path = real_root
    for depth, part in enumerate(parts, start=1):
        path = path / part
        if not os.path.lexists(path):
            continue
        expected = [step.casefold() for step in parts[:depth]]
        relative = contained_relative(path, real_root)
        if relative is None or [step.casefold() for step in relative.parts] != expected:
            raise ServerStateConfigurationError(
                f"{path} resolves to {os.path.realpath(path)!r}, which is not this state "
                f"root's own {'/'.join(parts[:depth])}; a state root's daemon and instance "
                "stores must live inside it (a copied state root may not link back to the "
                "original's). Repair: replace the link with a real copy, or point "
                "CRUXIBLE_STATE_ROOT at the root it names"
            )
    return path


_SQLITE_HEADER = b"SQLite format 3\x00"


def _refuse_legacy_state_file(path: Path) -> None:
    """Refuse a pre-PC-HR database at ``path``, naming it; ignore an empty file.

    Only a SQLite database there is the old layout. An empty file is not a
    layout at all: a read-looking probe such as ``sqlite3 <path>`` creates one,
    and treating it as the old layout took a daemon down. Anything else sitting
    at a legacy path is refused by name, as neither.
    """

    try:
        with path.open("rb") as handle:
            head = handle.read(len(_SQLITE_HEADER))
    except FileNotFoundError:
        return
    except IsADirectoryError as exc:
        raise ServerStateConfigurationError(
            f"{path} is a directory where the layout from before PC-HR kept a database; "
            "repair: move it out of the state root"
        ) from exc
    except OSError as exc:
        raise ServerStateConfigurationError(
            f"{path} sits where the layout from before PC-HR kept a database and cannot be "
            f"read ({exc.strerror}); repair: move it out of the state root"
        ) from exc
    if not head:
        return
    if head == _SQLITE_HEADER:
        raise PlaybillReseedRequired(found=str(path))
    raise ServerStateConfigurationError(
        f"{path} sits where the layout from before PC-HR kept a database, but it is "
        "neither empty nor a SQLite database; repair: move it out of the state root"
    )


def get_server_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the durable server request log path."""
    env = os.environ if environ is None else environ
    raw = env.get("CRUXIBLE_SERVER_LOG_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    return (get_server_state_root(env) / "daemon" / "logs" / "server.log").resolve()


def get_server_fatal_log_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the file a fatal fault writes its traceback to.

    A sibling of the request log rather than the request log itself: the
    request log is structured JSON lines written through a rotating sink, and a
    C-level fault handler writes plain text through a raw file descriptor with
    no idea that either is true. Interleaving them would corrupt the one an
    operator parses. Same directory, so an operator who found one has found the
    other.
    """

    return (get_server_log_path(environ).parent / "fatal.log").resolve()


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
    state_dir = get_server_state_root(environ)
    warnings: list[str] = []
    if is_volatile_state_path(state_dir):
        warnings.append(
            "CRUXIBLE_STATE_ROOT resolves under a volatile temp path "
            f"({state_dir}). Use a durable directory such as ~/.cruxible "
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


def get_disabled_consumers(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """Built-in consumer kinds the operator turned off (``CRUXIBLE_DISABLED_CONSUMERS``)."""
    env = environ or os.environ
    raw = env.get("CRUXIBLE_DISABLED_CONSUMERS", "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


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


class ServerAuthRequired(ConfigError):
    """The daemon refuses to serve without auth on this transport or state root.

    Carries one code per cause, so a refusal names the exact repair instead of
    a shared sentence covering several different situations.
    """

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(f"{error_code}: {message}")


#: The one repair every auth-off refusal names: the explicit local opt-in.
SERVER_AUTH_OPT_IN = "cruxible server start --auth"


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
        raise ServerAuthRequired(
            "cruxible.server.auth_latched",
            "this state root previously required auth, so the daemon refuses to serve "
            f"it without auth; repair: `{SERVER_AUTH_OPT_IN}` (or set "
            "CRUXIBLE_SERVER_AUTH=true)",
        )

    if auth_enabled and bootstrap_secret is None and not runtime_credentials_available:
        raise ConfigError(
            "CRUXIBLE_SERVER_AUTH=true requires CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET "
            "or stored runtime credentials."
        )

    if env.get("CRUXIBLE_SERVER_SOCKET"):
        # A Unix socket is reachable only through a 0700 directory this OS user
        # owns, so every process that can connect already runs as the operator.
        return

    if not auth_enabled:
        # A TCP port has no such boundary: loopback is reachable by every local
        # user, and anything bound wider by the network. Without auth, any of
        # them could claim any principal.
        host = env.get("CRUXIBLE_HOST", "127.0.0.1")
        port = env.get("CRUXIBLE_PORT", "8100")
        raise ServerAuthRequired(
            "cruxible.server.tcp_requires_auth",
            f"a TCP daemon ({host}:{port}) refuses to start without auth, because any "
            "process that can reach the port could act as any principal; repair: "
            f"`{SERVER_AUTH_OPT_IN}`, or listen on a Unix socket with "
            "`cruxible server start --socket PATH`",
        )


def auth_off_startup_notice() -> str:
    """The one line an auth-off daemon prints when it starts."""

    return (
        "Auth off: Unix-socket daemon; every process running as this OS user is equally "
        "trusted and a principal ID is a claim, not authentication. "
        f"Opt in with `{SERVER_AUTH_OPT_IN}`."
    )
