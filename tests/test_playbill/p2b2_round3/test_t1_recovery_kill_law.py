"""Round 3 - attack the new recovery kill law (N-1's fix)."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    ProviderProcessRecoveryResultV1,
    _current_boot_id,
    _process_start_time,
)

MARKER_LOOP = (
    "import sys,time\n"
    "path=sys.argv[1]\n"
    "while True:\n"
    "    open(path,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)


def _spawn_marker(root: Path, name: str) -> tuple[subprocess.Popen[bytes], Path]:
    marker = root / name
    process = subprocess.Popen(
        [sys.executable, "-c", MARKER_LOOP, str(marker)],
        start_new_session=True,
    )
    for _ in range(300):
        if marker.exists():
            return process, marker
        time.sleep(0.01)
    raise AssertionError("marker process never started")


def _reap(process: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.wait(timeout=5)


def _write_record(
    store: ProviderProcessLeaseStore,
    invocation_id: str,
    *,
    pid: int,
    pgid: int,
    boot_id: str | None,
    start_time: str | None,
) -> Path:
    record_path, _control = store.paths(invocation_id)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation_id,
                "pid": pid,
                "process_group_id": pgid,
                "session_id": os.getsid(pid),
                "boot_id": boot_id,
                "process_start_time": start_time,
            }
        )
    )
    return record_path


def _grew(marker: Path, seconds: float = 0.4) -> bool:
    before = marker.stat().st_size
    time.sleep(seconds)
    return marker.stat().st_size > before


# ---------------------------------------------------------------- T-A


def test_the_process_start_token_does_not_separate_same_second_processes() -> None:
    """The OS identity fence's resolution is the platform ps/procfs granularity."""

    first = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(3)"])
    second = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(3)"])
    try:
        first_token = _process_start_time(first.pid)
        second_token = _process_start_time(second.pid)
    finally:
        for item in (first, second):
            item.kill()
            item.wait(timeout=5)
    if Path("/proc/1/stat").exists():
        pytest.skip("procfs jiffy resolution; the ps 1-second path is the macOS fallback")
    assert first_token == second_token, (first_token, second_token)


def test_a_reused_pid_whose_recorded_identity_matches_is_sigkilled(short_root: Path) -> None:
    """No proof of daemon parentage: any live pid+pgid whose published identity
    reproduces is SIGKILLed with its whole group and descendant tree."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    victim, marker = _spawn_marker(short_root, "victim")
    try:
        _write_record(
            store,
            "sha256:" + "a" * 64,
            pid=victim.pid,
            pgid=victim.pid,
            boot_id=_current_boot_id(),
            start_time=_process_start_time(victim.pid),
        )
        result = store.recover_all()
        assert result.recovered == ("sha256:" + "a" * 64,), result
        assert not _grew(marker)
        with pytest.raises(ProcessLookupError):
            os.kill(victim.pid, 0)
    finally:
        _reap(victim)


# ---------------------------------------------------------------- T-C


def test_a_prefix_record_with_a_live_pid_is_never_signalled(short_root: Path) -> None:
    """CONFIRM: boot_id/process_start_time = None (pre-fix record) sends no signal."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    bystander, marker = _spawn_marker(short_root, "bystander")
    try:
        _write_record(
            store,
            "sha256:" + "b" * 64,
            pid=bystander.pid,
            pgid=bystander.pid,
            boot_id=None,
            start_time=None,
        )
        result = store.recover_all()
        assert result.recovered == ()
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert result.removed[0].invocation_id == "sha256:" + "b" * 64
        assert result.completion_invocation_ids == ("sha256:" + "b" * 64,)
        assert _grew(marker), "the pre-fix record must not authorize a signal"
    finally:
        _reap(bystander)


def test_a_stale_record_naming_a_reused_pid_sends_no_signal(short_root: Path) -> None:
    """CONFIRM: same boot, live pid, but a start token from the dead predecessor."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    bystander, marker = _spawn_marker(short_root, "reused")
    try:
        _write_record(
            store,
            "sha256:" + "c" * 64,
            pid=bystander.pid,
            pgid=bystander.pid,
            boot_id=_current_boot_id(),
            start_time="Thu Jan  1 00:00:00 1970",
        )
        result = store.recover_all()
        assert result.recovered == ()
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert _grew(marker)
    finally:
        _reap(bystander)


# ---------------------------------------------------------------- T-D


def test_ps_absence_is_a_typed_publish_refusal(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_current_boot_id`/`_process_start_time` claim to raise a typed refusal when
    the OS identity is unavailable, but an absent `ps`/`sysctl` raises
    FileNotFoundError straight out of `publish()`."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")

    def missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory: 'ps'")

    monkeypatch.setattr(lease_module.subprocess, "run", missing)
    monkeypatch.setattr(lease_module.Path, "read_text", lambda *_a, **_k: "")
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.publish("sha256:" + "d" * 64, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert caught.value.code == "provider_process_lease_invalid"


def test_ps_garbage_is_typed(short_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIRM: a `ps` that answers with garbage is converted to a typed refusal."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")

    class _Garbage:
        returncode = 0
        stdout = "   "

    monkeypatch.setattr(lease_module.subprocess, "run", lambda *_a, **_k: _Garbage())
    monkeypatch.setattr(lease_module.Path, "read_text", lambda *_a, **_k: "")
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.publish("sha256:" + "e" * 64, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert caught.value.code == "provider_process_lease_invalid"


def test_an_oserror_inside_recovery_retains_the_invocation_id_and_record(
    short_root: Path,
) -> None:
    """A record that parses but fails later is removed with invocation_id=None, so
    its durable `provider_invocation_started` is never completed."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation_id = "sha256:" + "f" * 64
    record_path = _write_record(
        store,
        invocation_id,
        pid=os.getpid(),
        pgid=os.getpgid(0),
        boot_id=_current_boot_id(),
        start_time=_process_start_time(os.getpid()),
    )
    original = lease_module.descendant_processes

    def explode(pid: int) -> object:
        raise OSError(5, "Input/output error")

    lease_module.descendant_processes = explode  # type: ignore[assignment]
    try:
        result = store.recover_all()
    finally:
        lease_module.descendant_processes = original  # type: ignore[assignment]
    assert result.recovered == ()
    assert result.removed == ()
    assert result.could_not_clean[0].invocation_id == invocation_id
    assert result.completion_invocation_ids == ()
    assert record_path.exists()


# ---------------------------------------------------------------- T-E


def test_the_echo_alone_never_authorizes_a_killpg(
    short_root: Path,
) -> None:
    """Clause (a) of the kill law attests only that SOMETHING at the socket knows
    the invocation id - never that `lease.pid` is that process."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation_id = "sha256:" + "1" * 64
    _record, control_path = store.paths(invocation_id)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(control_path))
    server.listen(2)

    def echo() -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            with connection:
                received = connection.recv(4096).decode("utf-8")
                connection.sendall(received.encode("utf-8"))

    threading.Thread(target=echo, daemon=True).start()
    victim, marker = _spawn_marker(short_root, "echo-victim")
    try:
        _write_record(
            store,
            invocation_id,
            pid=victim.pid,
            pgid=victim.pid,
            boot_id=None,
            start_time=None,
        )
        assert (
            store._live_identity_matches(  # noqa: SLF001
                lease_module.ProviderProcessLeaseV1(
                    invocation_id=invocation_id,
                    pid=victim.pid,
                    process_group_id=victim.pid,
                    session_id=os.getsid(victim.pid),
                    boot_id=None,
                    process_start_time=None,
                    control_path=control_path,
                    record_path=_record,
                )
            )
            is False
        )
        result = store.recover_all()
        assert result.recovered == (), result
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert _grew(marker)
    finally:
        server.close()
        _reap(victim)


# ---------------------------------------------------------------- T-F


def test_an_unkillable_group_degrades_the_lane_and_strands_the_durable_start(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """could_not_clean keeps the record, never authors a completion, and (through
    the operator) latches the Provider lane unavailable for the process lifetime."""

    from cruxible_core.runtime.provider_runtime import (
        ProviderRecoveryFoldDisposition,
        ProviderRuntimeOperator,
    )

    operator = ProviderRuntimeOperator(short_root)
    folded: list[ProviderProcessRecoveryResultV1] = []

    def fold_recovery(
        result: ProviderProcessRecoveryResultV1,
    ) -> dict[str, ProviderRecoveryFoldDisposition]:
        operator.acknowledge_recovery(result.completion_invocation_ids)
        folded.append(result)
        return {invocation_id: "handled" for invocation_id in result.completion_invocation_ids}

    operator.bind_recovery_fold(fold_recovery)
    assert operator.process_leases is not None
    store = operator.process_leases
    store.recovery_timeout_seconds = 0.2
    victim, marker = _spawn_marker(short_root, "unkillable")
    invocation_id = "sha256:" + "2" * 64
    record_path = _write_record(
        store,
        invocation_id,
        pid=victim.pid,
        pgid=victim.pid,
        boot_id=_current_boot_id(),
        start_time=_process_start_time(victim.pid),
    )
    real_killpg = os.killpg

    def refuse(pgid: int, sig: int) -> None:
        if pgid == victim.pid and sig == signal.SIGKILL:
            raise PermissionError(1, "Operation not permitted")
        real_killpg(pgid, sig)

    monkeypatch.setattr(lease_module.os, "killpg", refuse)
    try:
        result = operator.recover_all()
        assert result.recovered == ()
        assert result.removed == ()
        assert len(result.could_not_clean) == 1
        assert result.could_not_clean[0].code == "provider_process_group_survived_recovery"
        assert result.completion_invocation_ids == ()
        assert record_path.exists()
        assert operator.unavailable_reason is not None
        assert "provider_process_group_survived_recovery" in operator.unavailable_reason
        assert _grew(marker)
        monkeypatch.setattr(lease_module.os, "killpg", real_killpg)
        operator.invoker_for(
            SimpleNamespace(tree_at=lambda _oid: {}),  # type: ignore[arg-type]
            accepted_oid="a" * 40,
        )
        assert operator.unavailable_reason is None
        assert not record_path.exists()
        assert len(folded) == 1
    finally:
        monkeypatch.undo()
        _reap(victim)


# ---------------------------------------------------------------- T-G


def test_two_records_naming_the_same_pid_do_not_double_signal(short_root: Path) -> None:
    """CONFIRM: ordering is safe - the second record sees a dead pid and is removed."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    victim, marker = _spawn_marker(short_root, "twice")
    try:
        for suffix in ("3", "4"):
            _write_record(
                store,
                "sha256:" + suffix * 64,
                pid=victim.pid,
                pgid=victim.pid,
                boot_id=_current_boot_id(),
                start_time=_process_start_time(victim.pid),
            )
        result = store.recover_all()
        assert len(result.recovered) == 1
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert not _grew(marker)
    finally:
        _reap(victim)
