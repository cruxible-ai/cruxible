"""The daemon a stdio MCP adapter talks to: configured, found, or started.

Every MCP tool runs through a daemon; there is no in-process mode. The adapter
selects its daemon in this order and stops at the first that applies:

1. ``environment``: ``CRUXIBLE_SERVER_URL`` or ``CRUXIBLE_SERVER_SOCKET`` in the
   adapter's own environment.
2. ``workspace``: the transport the MCP workspace's ``.cruxible/coverage.json``
   binds together with an instance (a binding to the default socket below is
   that daemon, so it falls through to 3 and 4).
3. ``default_socket``: ``<state root>/run/daemon.sock`` when a daemon answers there.
4. auto-start under the state root (``CRUXIBLE_STATE_ROOT``, else ``~/.cruxible``),
   holding ``<state root>/run/autostart.lock`` so two adapters never race two
   starts: the live daemon already holding the state root is reused
   (``state_root_holder``); else the installed user service is started
   (``service``); else ``cruxible server start --socket <default socket>`` is
   spawned detached (``started``).

A configured transport (1 or 2) that does not answer is refused by name; the
adapter never starts a daemon in its place.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from cruxible_client.authoring.context import ContextResolutionError, resolve_context
from cruxible_core.errors import ConfigError
from cruxible_core.mcp.workspace import mcp_workspace_root
from cruxible_core.server.config import get_server_state_root, resolve_server_settings
from cruxible_core.server.service_install import (
    installed_service_config,
    start_installed_service,
)
from cruxible_core.server.state_lock import read_state_lock, state_lock_holder_is_alive

DaemonSource = Literal[
    "environment",
    "workspace",
    "default_socket",
    "state_root_holder",
    "service",
    "started",
]

#: The default daemon socket, relative to the state root.
DEFAULT_SOCKET_RELATIVE = Path("run") / "daemon.sock"
AUTOSTART_LOCK_RELATIVE = Path("run") / "autostart.lock"
AUTOSTART_LOG_RELATIVE = Path("run") / "daemon.out"
_TARGET_ENV = ("CRUXIBLE_SERVER_URL", "CRUXIBLE_SERVER_SOCKET")
_PROBE_TIMEOUT_S = 2.0
_START_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.1


class DaemonUnavailableError(ConfigError):
    """No daemon answers and none could be started; the message names the repair."""

    error_code = "cruxible.mcp.daemon_unavailable"


@dataclass(frozen=True)
class DaemonTarget:
    """One daemon transport and why the adapter selected it."""

    server_url: str | None
    server_socket: str | None
    source: DaemonSource

    @property
    def label(self) -> str:
        return self.server_url if self.server_url else f"unix socket {self.server_socket}"


def default_socket_path(state_root: Path) -> Path:
    return state_root / DEFAULT_SOCKET_RELATIVE


_local_lock = threading.Lock()
_local_target: tuple[Path, DaemonTarget] | None = None


def forget_local_daemon() -> None:
    """Drop the found or started daemon, so the next call looks for one again."""

    global _local_target
    with _local_lock:
        _local_target = None


def resolve_daemon_target(environ: Mapping[str, str] | None = None) -> DaemonTarget:
    """The daemon this adapter talks to, starting a local one when nothing is configured."""

    global _local_target
    env = os.environ if environ is None else environ
    configured = configured_daemon_target(env)
    if configured is not None:
        return configured
    state_root = get_server_state_root(env)
    with _local_lock:
        if _local_target is not None and _local_target[0] == state_root:
            return _local_target[1]
        target = ensure_local_daemon(state_root, environ=env)
        _local_target = (state_root, target)
        return target


def configured_daemon_target(environ: Mapping[str, str] | None = None) -> DaemonTarget | None:
    """The environment's transport, else the workspace binding's; never starts anything."""

    env = os.environ if environ is None else environ
    settings = resolve_server_settings(environ=env)
    if settings.enabled:
        return DaemonTarget(settings.server_url, settings.server_socket, "environment")
    return _workspace_transport(env)


def _workspace_transport(env: Mapping[str, str]) -> DaemonTarget | None:
    """The transport the MCP workspace binds with an instance, if any."""

    bare = {key: value for key, value in env.items() if key not in _TARGET_ENV}
    try:
        resolved = resolve_context(
            environ=bare,
            remembered={},
            workspace=mcp_workspace_root(env),
        )
    except (ContextResolutionError, ConfigError, OSError, RuntimeError, ValueError):
        # An unreadable or malformed binding selects nothing, as for the instance.
        return None
    if resolved.transport_source != "workspace":
        return None
    if resolved.server_socket is not None and _is_default_socket(resolved.server_socket, env):
        # A binding to the default socket names the daemon auto-start provides:
        # it is found or started there, not refused while it is down.
        return None
    return DaemonTarget(resolved.server_url, resolved.server_socket, "workspace")


def _is_default_socket(server_socket: str, env: Mapping[str, str]) -> bool:
    try:
        default = default_socket_path(get_server_state_root(env))
        return Path(server_socket).expanduser().resolve() == default.resolve()
    except (ConfigError, OSError, RuntimeError):
        return False


def _answers(*, server_url: str | None = None, server_socket: str | None = None) -> bool:
    """Whether a Cruxible daemon answers its public version probe there."""

    try:
        if server_socket is not None:
            if not os.path.exists(server_socket):
                return False
            probe = httpx.Client(
                base_url="http://cruxible",
                transport=httpx.HTTPTransport(uds=server_socket),
                timeout=_PROBE_TIMEOUT_S,
            )
        else:
            assert server_url is not None
            probe = httpx.Client(base_url=server_url, timeout=_PROBE_TIMEOUT_S)
        with probe:
            response = probe.get("/version")
    except (httpx.HTTPError, OSError):
        return False
    return response.status_code == 200


def _holder_target(transport: str) -> DaemonTarget:
    """The lock record's transport (``unix socket PATH`` or ``HOST:PORT``) as a target."""

    if transport.startswith("unix socket "):
        return DaemonTarget(None, transport.removeprefix("unix socket "), "state_root_holder")
    return DaemonTarget(f"http://{transport}", None, "state_root_holder")


@contextmanager
def _autostart_lock(state_root: Path) -> Iterator[None]:
    path = state_root / AUTOSTART_LOCK_RELATIVE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _repair(state_root: Path, socket_path: Path) -> str:
    return (
        f"start a daemon with `cruxible server start --state-root {state_root} "
        f"--socket {socket_path}`, or install one with `cruxible server install-service "
        f"--state-root {state_root} --socket {socket_path}`; or point this MCP server at a "
        "running daemon with CRUXIBLE_SERVER_SOCKET or CRUXIBLE_SERVER_URL"
    )


def ensure_local_daemon(
    state_root: Path, *, environ: Mapping[str, str] | None = None
) -> DaemonTarget:
    """Reuse the daemon on the default socket or state root, or start one."""

    env = os.environ if environ is None else environ
    socket_path = default_socket_path(state_root)
    if _answers(server_socket=str(socket_path)):
        return DaemonTarget(None, str(socket_path), "default_socket")
    with _autostart_lock(state_root):
        # Another adapter may have started it while this one waited for the lock.
        if _answers(server_socket=str(socket_path)):
            return DaemonTarget(None, str(socket_path), "default_socket")
        holder = read_state_lock(state_root) if state_lock_holder_is_alive(state_root) else None
        if holder is not None:
            target = _holder_target(holder.transport)
            if _answers(server_url=target.server_url, server_socket=target.server_socket):
                return target
            raise DaemonUnavailableError(
                f"state root {state_root} is held by daemon pid {holder.pid} on "
                f"{holder.transport}, which does not answer; repair: wait for it to finish "
                "starting, or stop it with `cruxible server stop` and retry"
            )
        service = installed_service_config(state_root)
        if service is not None:
            target = (
                DaemonTarget(None, service.socket_path, "service")
                if service.socket_path is not None
                else DaemonTarget(f"http://{service.host}:{service.port}", None, "service")
            )
            try:
                start_installed_service(service)
            except (OSError, subprocess.CalledProcessError) as exc:
                raise DaemonUnavailableError(
                    f"the installed Cruxible service for {state_root} did not start: {exc}; "
                    f"repair: {_repair(state_root, socket_path)}"
                ) from exc
            _wait_until_answering(target, state_root, socket_path, process=None)
            return target
        process = _spawn_daemon(state_root, socket_path, env)
        target = DaemonTarget(None, str(socket_path), "started")
        _wait_until_answering(target, state_root, socket_path, process=process)
        return target


def _cruxible_executable() -> str | None:
    beside = Path(sys.executable).parent / "cruxible"
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    return shutil.which("cruxible")


def _spawn_daemon(
    state_root: Path, socket_path: Path, env: Mapping[str, str]
) -> subprocess.Popen[bytes]:
    executable = _cruxible_executable()
    if executable is None:
        raise DaemonUnavailableError(
            "no daemon answers and the cruxible executable is not installed beside this "
            f"interpreter or on PATH, so none can be started; repair: "
            f"{_repair(state_root, socket_path)}"
        )
    child_env = {
        key: value
        for key, value in env.items()
        if key not in (*_TARGET_ENV, "CRUXIBLE_MODE", "CRUXIBLE_INSTANCE_ID")
    }
    log_path = state_root / AUTOSTART_LOG_RELATIVE
    with open(log_path, "ab") as log:
        return subprocess.Popen(
            [
                executable,
                "server",
                "start",
                "--state-root",
                str(state_root),
                "--socket",
                str(socket_path),
                "--capability-ceiling",
                "admin",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=child_env,
            start_new_session=True,
        )


def _wait_until_answering(
    target: DaemonTarget,
    state_root: Path,
    socket_path: Path,
    *,
    process: subprocess.Popen[bytes] | None,
) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        if _answers(server_url=target.server_url, server_socket=target.server_socket):
            return
        if process is not None and process.poll() is not None:
            raise DaemonUnavailableError(
                f"the daemon started for {state_root} exited with status {process.returncode} "
                f"(its output is in {state_root / AUTOSTART_LOG_RELATIVE}); repair: "
                f"{_repair(state_root, socket_path)}"
            )
        time.sleep(_POLL_INTERVAL_S)
    raise DaemonUnavailableError(
        f"the daemon started for {state_root} did not answer on {target.label} within "
        f"{_START_TIMEOUT_S:.0f}s; repair: {_repair(state_root, socket_path)}"
    )


__all__ = [
    "DEFAULT_SOCKET_RELATIVE",
    "DaemonSource",
    "DaemonTarget",
    "DaemonUnavailableError",
    "configured_daemon_target",
    "default_socket_path",
    "ensure_local_daemon",
    "forget_local_daemon",
    "resolve_daemon_target",
]
