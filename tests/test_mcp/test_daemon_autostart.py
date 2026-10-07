"""The MCP adapter's daemon: configured, found, or started (ruling cleanup-decisions-rest-1006).

Precedence: environment transport, the workspace binding's socket, the default
socket when a daemon answers there, then auto-start under the state root
(reusing the live holder, preferring an installed service), serialized by a
state-root lock. A configured transport that does not answer is refused, never
replaced by a started daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.errors import ServerUnreachableError
from cruxible_core.errors import ConfigError
from cruxible_core.mcp import daemon, handlers
from cruxible_core.mcp.daemon import (
    DaemonTarget,
    DaemonUnavailableError,
    configured_daemon_target,
    default_socket_path,
)
from cruxible_core.mcp.daemon import ensure_local_daemon as real_ensure_local_daemon
from cruxible_core.server.service_install import ServiceInstallConfigV1
from cruxible_core.server.state_lock import StateRootLock, read_state_lock
from tests.support.short_temporary_root import short_temporary_directory


def _bind_workspace(root: Path, **binding: str) -> None:
    (root / ".cruxible").mkdir(parents=True, exist_ok=True)
    (root / ".cruxible" / "coverage.json").write_text(
        json.dumps({"tag": "playbill-coverage-workspace-config-v1", **binding}),
        encoding="utf-8",
    )


def _env(state_root: Path, workspace: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(state_root.parent),
        "CRUXIBLE_STATE_ROOT": str(state_root),
        "CRUXIBLE_MCP_WORKSPACE_ROOT": str(workspace),
        **extra,
    }


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return state_root, workspace


class _Answers:
    """A stand-in for the version probe: the transports that answer."""

    def __init__(self, *live: str) -> None:
        self.live = set(live)
        self.probed: list[str] = []

    def __call__(self, *, server_url: str | None = None, server_socket: str | None = None) -> bool:
        key = server_url if server_url is not None else str(server_socket)
        self.probed.append(key)
        return key in self.live


def test_the_environment_transport_wins_and_is_never_probed(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    _bind_workspace(workspace, server_socket="/elsewhere.sock", instance_id="inst_bound")
    answers = _Answers()
    monkeypatch.setattr(daemon, "_answers", answers)

    target = daemon.resolve_daemon_target(
        _env(state_root, workspace, CRUXIBLE_SERVER_SOCKET="/configured.sock")
    )

    assert target == DaemonTarget(None, "/configured.sock", "environment")
    assert answers.probed == []


def test_the_workspace_binding_socket_comes_next(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    _bind_workspace(workspace, server_socket="/bound.sock", instance_id="inst_bound")
    monkeypatch.setattr(daemon, "_answers", _Answers())

    target = daemon.resolve_daemon_target(_env(state_root, workspace))

    assert target == DaemonTarget(None, "/bound.sock", "workspace")


def test_a_binding_without_an_instance_is_not_configuration(
    roots: tuple[Path, Path],
) -> None:
    state_root, workspace = roots
    _bind_workspace(workspace, server_socket="/bound.sock")
    assert configured_daemon_target(_env(state_root, workspace)) is None


def test_a_binding_to_the_default_socket_is_configuration_too(
    roots: tuple[Path, Path],
) -> None:
    """The bound daemon is the binding's, even on the socket auto-start would use (F-001)."""

    state_root, workspace = roots
    socket = str(default_socket_path(state_root))
    _bind_workspace(workspace, server_socket=socket, instance_id="inst_bound")

    assert configured_daemon_target(_env(state_root, workspace)) == DaemonTarget(
        None, socket, "workspace"
    )


def test_an_unreachable_bound_default_socket_is_refused_not_restarted(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    socket = default_socket_path(state_root)
    _bind_workspace(workspace, server_socket=str(socket), instance_id="inst_bound")
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(
        daemon, "ensure_local_daemon", lambda *_a, **_k: pytest.fail("never started")
    )
    handlers.reset_client_cache()
    try:
        with pytest.raises(ServerUnreachableError) as refused:
            handlers.handle_server_info()
    finally:
        handlers.reset_client_cache()

    assert str(socket) in str(refused.value)
    assert "configured by workspace, so none is started in its place" in str(refused.value)


def test_the_default_socket_is_reused_when_a_daemon_answers_there(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    socket = str(default_socket_path(state_root))
    monkeypatch.setattr(daemon, "_answers", _Answers(socket))
    monkeypatch.setattr(
        daemon, "_spawn_daemon", lambda *_a: pytest.fail("a live default socket is reused")
    )

    target = real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    assert target == DaemonTarget(None, socket, "default_socket")


def test_the_live_state_root_holder_is_reused_on_its_own_transport(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    monkeypatch.setattr(daemon, "_answers", _Answers("/held.sock"))
    monkeypatch.setattr(
        daemon, "_spawn_daemon", lambda *_a: pytest.fail("a held state root is never started")
    )
    with StateRootLock(state_root, transport="unix socket /held.sock"):
        target = real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    assert target == DaemonTarget(None, "/held.sock", "state_root_holder")


def test_a_holder_that_does_not_answer_is_refused_rather_than_raced(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    monkeypatch.setattr(daemon, "_answers", _Answers())
    monkeypatch.setattr(daemon, "_spawn_daemon", lambda *_a: pytest.fail("never raced"))
    with StateRootLock(state_root, transport="127.0.0.1:8199"):
        with pytest.raises(DaemonUnavailableError, match="does not answer") as refused:
            real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    assert refused.value.error_code == "cruxible.mcp.daemon_unavailable"
    assert "cruxible server stop" in str(refused.value)


def test_an_installed_service_is_preferred_over_spawning(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    service = ServiceInstallConfigV1(
        platform="darwin",
        executable="/usr/bin/true",
        state_root=str(state_root),
        socket_path="/service.sock",
        capability_ceiling="admin",
        auth_enabled=False,
    )
    answers = _Answers()
    started: list[ServiceInstallConfigV1] = []

    def start(config: ServiceInstallConfigV1) -> None:
        started.append(config)
        answers.live.add("/service.sock")

    monkeypatch.setattr(daemon, "_answers", answers)
    monkeypatch.setattr(daemon, "installed_service_config", lambda _root: service)
    monkeypatch.setattr(daemon, "start_installed_service", start)
    monkeypatch.setattr(daemon, "_spawn_daemon", lambda *_a: pytest.fail("the service starts it"))

    target = real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    assert started == [service]
    assert target == DaemonTarget(None, "/service.sock", "service")


class _Process:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_with_nothing_to_reuse_it_spawns_the_daemon_on_the_default_socket(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    socket = str(default_socket_path(state_root))
    answers = _Answers()
    launched: list[dict[str, Any]] = []

    def popen(argv: list[str], **kwargs: Any) -> _Process:
        launched.append({"argv": argv, **kwargs})
        answers.live.add(socket)
        return _Process()

    monkeypatch.setattr(daemon, "_answers", answers)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    secrets = {
        "CRUXIBLE_SERVER_BEARER_TOKEN": "bearer-secret-value",
        "CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET": "bootstrap-secret-value",
        "CRUXIBLE_PRINCIPAL_KEY": "principal-key-value",
        "CRUXIBLE_REGISTRY_PASSWORD": "registry-password-value",
        "CRUXIBLE_MIRROR_TOKEN": "mirror-token-value",
    }
    env = _env(
        state_root,
        workspace,
        CRUXIBLE_MODE="read_only",
        CRUXIBLE_INSTANCE_ID="inst_x",
        CRUXIBLE_PRINCIPAL_ID="agent",
        CRUXIBLE_SERVER_AUTH="true",
        CRUXIBLE_SERVER_LOG_PATH=str(state_root / "daemon.log"),
        **secrets,
    )

    target = real_ensure_local_daemon(state_root, environ=env)

    assert target == DaemonTarget(None, socket, "started")
    ((call),) = launched
    argv = call["argv"]
    assert argv[1:] == [
        "server",
        "start",
        "--state-root",
        str(state_root),
        "--socket",
        socket,
        "--capability-ceiling",
        "admin",
    ]
    assert call["start_new_session"] is True
    # The adapter's tier, instance, principal and credentials never configure or
    # authenticate the shared daemon; its daemon configuration does (F-002).
    child = call["env"]
    assert {key for key in child if key.startswith("CRUXIBLE_")} == {"CRUXIBLE_SERVER_LOG_PATH"}
    assert child["PATH"] == env["PATH"] and child["HOME"] == env["HOME"]
    for value in secrets.values():
        assert value not in argv
        assert value not in child.values()
    assert (state_root / "run").stat().st_mode & 0o077 == 0


def test_a_start_that_fails_is_a_typed_refusal_naming_the_repair(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    monkeypatch.setattr(daemon, "_answers", _Answers())
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_a, **_k: _Process(returncode=1))

    with pytest.raises(DaemonUnavailableError) as refused:
        real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    message = str(refused.value)
    assert "exited with status 1" in message
    assert str(state_root / "run" / "daemon.out") in message
    assert "cruxible server start" in message and "cruxible server install-service" in message


@pytest.mark.parametrize(
    "failure",
    [OSError(8, "Exec format error"), PermissionError(13, "Permission denied")],
    ids=["enoexec", "denied"],
)
def test_a_launch_that_raises_is_a_typed_refusal(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, failure: OSError
) -> None:
    """F-004: an exec failure names the launch and the repair, and starts nothing else."""

    state_root, workspace = roots

    def popen(*_args: object, **_kwargs: object) -> _Process:
        raise failure

    monkeypatch.setattr(daemon, "_answers", _Answers())
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)

    with pytest.raises(DaemonUnavailableError) as refused:
        real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    message = str(refused.value)
    assert message.startswith("cruxible.mcp.daemon_unavailable: could not launch")
    assert failure.strerror in message
    assert "cruxible server start" in message


def test_an_unreadable_log_or_service_record_is_a_typed_refusal(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    monkeypatch.setattr(daemon, "_answers", _Answers())
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no launch"))
    (state_root / "run").mkdir(parents=True, mode=0o700)
    (state_root / "run" / "daemon.out").mkdir()  # cannot be opened for append

    with pytest.raises(DaemonUnavailableError, match="could not open the auto-start log"):
        real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))

    def broken(_root: Path) -> None:
        raise ConfigError("recorded service settings cannot be read")

    monkeypatch.setattr(daemon, "installed_service_config", broken)
    monkeypatch.setattr(daemon, "_spawn_daemon", lambda *_a: pytest.fail("never bypassed"))
    with pytest.raises(DaemonUnavailableError, match="service .* cannot be read"):
        real_ensure_local_daemon(state_root, environ=_env(state_root, workspace))


def test_the_mcp_caller_reads_the_daemon_unavailable_code(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-003: with no executable to start, the tool error carries the code end to end."""

    from mcp.server.fastmcp.exceptions import ToolError

    from cruxible_core.mcp.server import create_server

    state_root, workspace = roots
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state_root))
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(daemon, "ensure_local_daemon", real_ensure_local_daemon)
    monkeypatch.setattr(daemon, "_answers", _Answers())
    monkeypatch.setattr(daemon, "_cruxible_executable", lambda: None)
    handlers.reset_client_cache()
    try:
        with pytest.raises(ToolError) as refused:
            asyncio.run(create_server().call_tool("cruxible_server_info", {}))
    finally:
        handlers.reset_client_cache()

    assert "cruxible.mcp.daemon_unavailable" in str(refused.value)
    assert "cruxible server start" in str(refused.value)


def test_the_state_root_lock_lets_only_one_adapter_start_a_daemon(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root, workspace = roots
    socket = str(default_socket_path(state_root))
    answers = _Answers()
    spawned: list[int] = []

    def spawn(*_args: object) -> _Process:
        spawned.append(1)
        time.sleep(0.2)  # the other adapter is waiting on the lock meanwhile
        answers.live.add(socket)
        return _Process()

    monkeypatch.setattr(daemon, "_answers", answers)
    monkeypatch.setattr(daemon, "_spawn_daemon", spawn)
    results: list[DaemonTarget] = []
    env = _env(state_root, workspace)
    threads = [
        threading.Thread(
            target=lambda: results.append(real_ensure_local_daemon(state_root, environ=env))
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert spawned == [1]
    assert sorted(item.source for item in results) == ["default_socket", "started"]


def test_an_unreachable_configured_transport_is_refused_not_replaced(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "nobody.sock"
    monkeypatch.setenv("CRUXIBLE_SERVER_SOCKET", str(missing))
    monkeypatch.setattr(
        daemon, "ensure_local_daemon", lambda *_a, **_k: pytest.fail("never started")
    )
    handlers.reset_client_cache()

    with pytest.raises(ServerUnreachableError) as refused:
        handlers.handle_server_info()

    assert str(missing) in str(refused.value)
    assert "configured by environment, so none is started in its place" in str(refused.value)
    handlers.reset_client_cache()


def test_with_no_transport_the_handlers_use_the_found_daemon(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cruxible_client import CruxibleClient
    from cruxible_core.server.app import create_app
    from tests.support.mcp_daemon import IN_PROCESS_DAEMON_URL

    state_root, workspace = roots
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    served = CruxibleClient(base_url=IN_PROCESS_DAEMON_URL)
    served._client = TestClient(create_app())  # type: ignore[assignment]
    found = DaemonTarget(IN_PROCESS_DAEMON_URL, None, "started")
    starts: list[Path] = []

    def ensure(root: Path, **_: object) -> DaemonTarget:
        starts.append(root)
        return found

    monkeypatch.setattr(daemon, "ensure_local_daemon", ensure)
    monkeypatch.setattr(handlers, "CruxibleClient", lambda **_kwargs: served)
    handlers.reset_client_cache()
    try:
        assert handlers.handle_server_info().scope == "daemon"
        assert handlers.handle_server_info().scope == "daemon"
        # Found once, then reused until it stops answering.
        assert len(starts) == 1
    finally:
        handlers.reset_client_cache()


@pytest.mark.skipif(
    not (Path(sys.executable).parent / "cruxible").is_file() and shutil.which("cruxible") is None,
    reason="needs the cruxible executable to start a daemon",
)
def test_auto_start_runs_a_real_daemon_under_a_scratch_state_root() -> None:
    """End to end: one real `cruxible server start`, found again, then stopped."""

    scratch = short_temporary_directory("cxd")
    state_root = scratch / "s"
    workspace = scratch / "w"
    workspace.mkdir()
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")},
        "HOME": str(scratch),
        "CRUXIBLE_STATE_ROOT": str(state_root),
        "CRUXIBLE_MCP_WORKSPACE_ROOT": str(workspace),
    }
    try:
        started = real_ensure_local_daemon(state_root, environ=env)
        assert started == DaemonTarget(None, str(default_socket_path(state_root)), "started")
        again = real_ensure_local_daemon(state_root, environ=env)
        assert again.source == "default_socket"
        holder = read_state_lock(state_root)
        assert holder is not None and holder.transport == f"unix socket {started.server_socket}"
    finally:
        holder = read_state_lock(state_root)
        if holder is not None:
            _stop(holder.pid)
        shutil.rmtree(scratch, ignore_errors=True)


def _stop(pid: int) -> None:
    """Stop the daemon this test started and reap it (it is this process's child)."""

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return
        if reaped == pid:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
