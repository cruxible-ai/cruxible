"""Exclusive daemon ownership of one state root, and the graceful stop that frees it."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from cruxible_core.server import shutdown as shutdown_module
from cruxible_core.server.app import run_server
from cruxible_core.server.state_lock import (
    ServerStateRootLocked,
    StateRootLock,
    read_state_lock,
    state_lock_holder_is_alive,
    state_lock_path,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "daemon-state"
    root.mkdir()
    return root


def test_a_second_daemon_over_one_state_root_refuses_typed_and_names_the_holder(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    with StateRootLock(root, transport="unix socket /run/a.sock"):
        with pytest.raises(ServerStateRootLocked) as refused:
            StateRootLock(root, transport="127.0.0.1:8100").acquire()

    message = str(refused.value)
    assert refused.value.error_code == "cruxible.server.state_root_locked"
    assert f"pid {os.getpid()}" in message
    assert "unix socket /run/a.sock" in message
    assert "cruxible server stop" in message


def test_run_server_refuses_the_held_root_before_it_opens_any_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal must land before the registry or credential store is touched."""

    root = _root(tmp_path)
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(root))
    monkeypatch.setattr(
        "cruxible_core.server.app._serve",
        lambda _socket: pytest.fail("the second daemon reached uvicorn"),
    )

    with StateRootLock(root, transport="127.0.0.1:8100"):
        with pytest.raises(ServerStateRootLocked):
            run_server(state_root=str(root))

    assert not (root / "daemon" / "registry.db").exists()
    assert not (root / "daemon" / "runtime_credentials.db").exists()


def test_the_lock_records_the_holders_pid_and_transport(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with StateRootLock(root, transport="unix socket /run/a.sock"):
        record = read_state_lock(root)
        assert record is not None
        assert record.pid == os.getpid()
        assert record.transport == "unix socket /run/a.sock"
        assert state_lock_holder_is_alive(root)
        assert state_lock_path(root).stat().st_mode & 0o777 == 0o600

    assert not state_lock_holder_is_alive(root)


def test_a_stale_lock_from_a_dead_holder_is_reclaimed(tmp_path: Path) -> None:
    """The kernel releases a dead holder's flock, so the file alone never blocks."""

    root = _root(tmp_path)
    path = state_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A pid that cannot be running: pid 0 is never a user process.
    path.write_text(
        json.dumps({"tag": "cruxible-server-state-lock-v1", "pid": 0, "transport": "gone"}),
        encoding="utf-8",
    )

    assert not state_lock_holder_is_alive(root)
    with StateRootLock(root, transport="127.0.0.1:8100"):
        record = read_state_lock(root)
        assert record is not None
        assert record.pid == os.getpid()
        assert record.transport == "127.0.0.1:8100"


def test_a_truncated_lock_body_does_not_block_a_new_daemon(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = state_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{not json")

    assert read_state_lock(root) is None
    with StateRootLock(root, transport="127.0.0.1:8100"):
        assert read_state_lock(root) is not None


def test_releasing_the_lock_lets_the_next_daemon_take_the_root(tmp_path: Path) -> None:
    root = _root(tmp_path)

    first = StateRootLock(root, transport="127.0.0.1:8100").acquire()
    first.release()
    second = StateRootLock(root, transport="127.0.0.1:8101").acquire()
    try:
        record = read_state_lock(root)
        assert record is not None
        assert record.transport == "127.0.0.1:8101"
    finally:
        second.release()


def test_the_lock_descriptor_is_close_on_exec_so_restart_cannot_deadlock(
    tmp_path: Path,
) -> None:
    """`server restart` re-execs in place; the lock must not outlive the old image.

    An inheritable descriptor would survive `os.execv` and the replacement image
    would refuse its own state root, turning the dev-loop restart into a
    permanent outage.
    """

    import fcntl

    root = _root(tmp_path)
    lock = StateRootLock(root, transport="127.0.0.1:8100").acquire()
    try:
        descriptor = lock._descriptor
        assert descriptor is not None
        assert os.get_inheritable(descriptor) is False
        assert bool(fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
    finally:
        lock.release()


def test_schedule_server_stop_signals_this_process_after_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fired = threading.Event()
    shutdown_module.set_signal_self(fired.set)
    monkeypatch.setattr(shutdown_module, "_STOP_DELAY_SECONDS", 0.0)
    try:
        shutdown_module.schedule_server_stop()
        assert fired.wait(timeout=2.0)
    finally:
        shutdown_module.reset_signal_self()


def test_reset_signal_self_restores_the_default() -> None:
    shutdown_module.set_signal_self(lambda: None)
    shutdown_module.reset_signal_self()
    assert shutdown_module._signal_self is shutdown_module._default_signal_self


def test_the_stop_route_schedules_a_graceful_shutdown_and_names_the_daemon_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`server stop` reaches the daemon over its own transport, not a stray kill."""

    from fastapi.testclient import TestClient

    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.server.app import create_app
    from cruxible_core.server.credentials import reset_runtime_credential_store
    from cruxible_core.server.registry import reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(_root(tmp_path)))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    # `create_app` binds the process-wide Playbill manager (and its Provider
    # runtime operator) to this temporary state root. Leaving it bound after the
    # directory is gone makes a later suite read a lane rooted at a path that no
    # longer exists, so clear it on both sides exactly as tests/test_server's
    # own conftest does.
    get_playbill_manager().clear()
    signalled = threading.Event()
    shutdown_module.set_signal_self(signalled.set)
    monkeypatch.setattr(shutdown_module, "_STOP_DELAY_SECONDS", 0.0)
    try:
        with TestClient(create_app()) as client:
            response = client.post("/api/v1/server/stop")
    finally:
        shutdown_module.reset_signal_self()
        get_playbill_manager().clear()
        reset_runtime_credential_store()
        reset_registry()
        reset_permissions()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scheduled"] is True
    assert payload["pid"] == os.getpid()
    assert signalled.wait(timeout=2.0)
