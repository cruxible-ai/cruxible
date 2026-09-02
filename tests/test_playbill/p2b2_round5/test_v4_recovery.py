"""Round-5: attack U-6/U-7/U-8 -- peer identity, race handling, and budgets."""

from __future__ import annotations

import contextlib
import os
import platform
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderProcessLeaseStore,
    ProviderProcessLeaseV1,
    _socket_peer_pid,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

ECHO_SERVER = r"""
import os, socket, sys, threading, time
invocation_id, control_path = sys.argv[1:3]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(4)
open(control_path + ".ready", "w").write(str(os.getpid()))
def serve():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            data = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if data == invocation_id else b"")
threading.Thread(target=serve, daemon=True).start()
while True:
    time.sleep(0.05)
"""


def _record(store: ProviderProcessLeaseStore, invocation: str, **fields: object) -> Path:
    record_path, _control = store.paths(invocation)
    document = {
        "invocation_id": invocation,
        "pid": 99_999_991,
        "process_group_id": 99_999_991,
        "session_id": None,
        "boot_id": None,
        "process_start_time": None,
    }
    document.update(fields)
    record_path.write_bytes(canonical_bytes(document))
    return record_path


# ------------------------------------------------------------------ U-6 peer pid


def test_the_peer_pid_helper_returns_the_real_kernel_pid(short_root: Path) -> None:
    path = short_root / "peer.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        accepted, _ = server.accept()
        with accepted:
            assert _socket_peer_pid(accepted) == os.getpid()
    finally:
        client.close()
        server.close()


def test_a_live_echoing_child_is_authorised_by_its_peer_pid_alone(short_root: Path) -> None:
    """boot_id/start token are absent, so ONLY the peer pid can authorise the kill."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "a" * 64
    _record_path, control_path = store.paths(invocation)
    script = short_root / "echo.py"
    script.write_text(ECHO_SERVER, encoding="utf-8")
    child = subprocess.Popen(
        [os.sys.executable, str(script), invocation, str(control_path)],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        ready = Path(str(control_path) + ".ready")
        deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "echo server never came up"
        record = _record(store, invocation, pid=child.pid, process_group_id=os.getpgid(child.pid))
        assert record.exists()
        lease = ProviderProcessLeaseV1(
            invocation_id=invocation,
            pid=child.pid,
            process_group_id=os.getpgid(child.pid),
            session_id=None,
            boot_id=None,
            process_start_time=None,
            control_path=control_path,
            record_path=record,
        )
        assert store.require_echo(lease) == child.pid
        assert not ProviderProcessLeaseStore._live_identity_matches(lease)
        result = store.recover_all()
        assert result.recovered == (invocation,), (
            platform.system(),
            result,
        )
        deadline = time.monotonic() + 3.0
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child.poll() is not None, "the echo-authorised group was not killed"
    finally:
        with contextlib.suppress(Exception):
            child.kill()
            child.wait()


# ------------------------------------------------------------------ U-7 races


def test_a_record_that_vanishes_between_glob_and_read_is_skipped(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    doomed = _record(store, "sha256:" + "b" * 64)
    survivor_id = "sha256:" + "c" * 64
    survivor = _record(store, survivor_id)
    real_read = Path.read_bytes

    def read(self: Path) -> bytes:
        if self == doomed:
            doomed.unlink()
        return real_read(self)

    original = Path.read_bytes
    Path.read_bytes = read  # type: ignore[method-assign, assignment]
    try:
        result = store.recover_all()
    finally:
        Path.read_bytes = original  # type: ignore[method-assign]
    assert result.could_not_clean == ()
    assert [item.invocation_id for item in result.removed] == [survivor_id]
    assert not survivor.exists()


def test_a_release_failure_lands_only_in_could_not_clean(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "d" * 64
    _record(store, invocation)

    def refuse(_lease: ProviderProcessLeaseV1) -> None:
        raise PermissionError(13, "planted")

    store.release = refuse  # type: ignore[method-assign]
    result = store.recover_all()
    assert result.recovered == ()
    assert [item.invocation_id for item in result.removed] == []
    assert [item.invocation_id for item in result.could_not_clean] == [invocation]
    assert result.completion_invocation_ids == ()


# ------------------------------------------------------------------ U-8 budget


def test_the_aggregate_budget_marks_the_remainder_not_attempted_and_keeps_it(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cruxible_core.playbill.provider_process_leases as lease_module

    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        recovery_timeout_seconds=0.3,
        recovery_aggregate_timeout_seconds=0.45,
    )
    survivors = [
        subprocess.Popen(
            [os.sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],  # type: ignore[attr-defined]
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(5)
    ]
    paths: list[Path] = []
    try:
        boot = lease_module._current_boot_id()
        for index, process in enumerate(survivors):
            invocation = "sha256:" + f"{index:064x}"
            paths.append(
                _record(
                    store,
                    invocation,
                    pid=process.pid,
                    process_group_id=process.pid,
                    session_id=os.getsid(process.pid),
                    boot_id=boot,
                    process_start_time=lease_module._process_start_time(process.pid),
                )
            )
        monkeypatch.setattr(lease_module.os, "killpg", lambda *args: None)
        started = time.monotonic()
        result = store.recover_all()
        elapsed = time.monotonic() - started
        statuses = [item.attempt_status for item in result.could_not_clean]
        assert len(result.could_not_clean) == 5
        assert statuses.count("not_attempted") >= 2, (elapsed, statuses)
        assert elapsed < 5 * 0.3, elapsed
        for item in result.could_not_clean:
            if item.attempt_status == "not_attempted":
                assert item.invocation_id is None
                assert item.code == "provider_process_group_survived_recovery"
        assert all(path.exists() for path in paths), "an unattempted record was removed"
        assert result.completion_invocation_ids == ()
    finally:
        for process in survivors:
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(process.pid), 9)
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=2)


def test_lane_status_never_blocks_while_a_rearm_recovery_runs(short_root: Path) -> None:
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)
    real = operator.process_leases.recover_all

    def slow(**kwargs: object):  # type: ignore[no-untyped-def]
        time.sleep(0.6)
        return real(**kwargs)  # type: ignore[arg-type]

    operator.process_leases.recover_all = slow  # type: ignore[method-assign]
    latencies: list[float] = []

    def poll() -> None:
        for _ in range(20):
            started = time.monotonic()
            operator.lane_status()
            latencies.append(time.monotonic() - started)
            time.sleep(0.02)

    reader = threading.Thread(target=poll)
    reader.start()
    operator._begin_invocation()
    operator._end_invocation()
    reader.join()
    assert max(latencies) < 0.05, max(latencies)
