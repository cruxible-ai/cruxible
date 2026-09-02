"""Round-5: attack U-5/U-3/U-I4/U-I5 -- the descendant fence and lease publication."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
from cruxible_core.playbill.provider_local_runtime import (
    _run_child,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderDescendantProcessV1,
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    kill_descendants,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1

CONTEXT = b'{"run_id":"RUN-r5","input":{"value":"r5"}}'

CHILD = r"""#!/usr/bin/env python3
import json, os, socket, subprocess, sys, threading, time

MARKER = "@MARKER@"
MODE = "@MODE@"
WHEN = "@WHEN@"
LINGER = float("@LINGER@")

invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)

def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if received == invocation_id else b"")

threading.Thread(target=echo, daemon=True).start()

LOOP = (
    "import sys,time,os\n"
    "path=sys.argv[1]\n"
    "mode=sys.argv[2]\n"
    "start=time.time()\n"
    "while True:\n"
    "    if mode=='late-setsid' and time.time()-start>0.35:\n"
    "        try:\n"
    "            os.setsid()\n"
    "        except OSError:\n"
    "            pass\n"
    "        mode='done'\n"
    "    open(path,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)

def spawn():
    if MODE == "setpgid":
        pre = lambda: os.setpgid(0, 0)
        child_mode = "plain"
    elif MODE == "setsid":
        pre = lambda: os.setsid()
        child_mode = "plain"
    else:  # late-setsid: same session at spawn, leaves it after the parent exits
        pre = lambda: os.setpgid(0, 0)
        child_mode = "late-setsid"
    subprocess.Popen(
        [sys.executable, "-c", LOOP, MARKER, child_mode],
        preexec_fn=pre,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    open(MARKER + ".spawned", "w").write("1")

if WHEN == "before":
    spawn()
document = json.loads(sys.stdin.buffer.read())
envelope = {
    "protocol_version": "1.0",
    "run_id": document["run_id"],
    "status": "ok",
    "output": {"echo": document["input"]["value"]},
    "refusal": None,
    "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
if WHEN == "after":
    spawn()
time.sleep(LINGER)
"""


def _child(path: Path, *, marker: Path, mode: str, when: str, linger: float = 0.0) -> Path:
    path.write_text(
        CHILD.replace("@MARKER@", str(marker))
        .replace("@MODE@", mode)
        .replace("@WHEN@", when)
        .replace("@LINGER@", str(linger)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _grew(marker: Path, seconds: float = 0.5) -> bool:
    before = marker.stat().st_size if marker.exists() else -1
    time.sleep(seconds)
    after = marker.stat().st_size if marker.exists() else -1
    return after > before


def _reap(token: str) -> None:
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and token in fields[1]:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


def _budgets() -> ProviderRuntimeBudgetsV1:
    return ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536)


def _invoke(store: ProviderProcessLeaseStore, interpreter: Path, invocation: str) -> object:
    return _run_child(
        interpreter,
        entrypoint="demo:Provider",
        context=CONTEXT,
        budgets=_budgets(),
        secret_fd=None,
        invocation_id=invocation,
        process_leases=store,
    )


# --------------------------------------------------- U-5: same-session determinism


@pytest.mark.parametrize("poll", [0.1, 1.0])
def test_a_same_session_setpgid_escapee_is_swept_on_a_subsecond_success(
    short_root: Path, poll: float
) -> None:
    """The session sweep must not depend on the poll interval or on the ppid chain."""

    marker = short_root / "m-pg"
    interpreter = _child(short_root / "c.py", marker=marker, mode="setpgid", when="after")
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=poll,
    )
    try:
        outcome = _invoke(store, interpreter, "sha256:" + "a" * 64)
        assert outcome is not None
        assert (marker.parent / (marker.name + ".spawned")).exists()
        assert not _grew(marker), "same-session setpgid escapee survived a fast success"
        assert list(store.root.glob("*.json")) == []
    finally:
        _reap(str(marker))


@pytest.mark.parametrize("when", ["before", "after"])
def test_a_cross_session_setsid_escapee_within_the_poll(short_root: Path, when: str) -> None:
    """Cross-session sweep is declared best-effort within the configured interval."""

    marker = short_root / f"m-sid-{when}"
    interpreter = _child(short_root / f"c-{when}.py", marker=marker, mode="setsid", when=when)
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=0.1,
    )
    try:
        _invoke(store, interpreter, "sha256:" + ("b" if when == "before" else "c") * 64)
        assert (marker.parent / (marker.name + ".spawned")).exists()
        survived = _grew(marker)
        if when == "before":
            assert not survived, "a pre-envelope cross-session escapee must be swept"
    finally:
        _reap(str(marker))


def test_a_cross_session_setsid_escapee_is_swept_when_the_child_outlives_one_poll(
    short_root: Path,
) -> None:
    marker = short_root / "m-sid-long"
    interpreter = _child(
        short_root / "c-long.py", marker=marker, mode="setsid", when="after", linger=0.5
    )
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=0.05,
    )
    try:
        _invoke(store, interpreter, "sha256:" + "d" * 64)
        assert not _grew(marker)
    finally:
        _reap(str(marker))


def test_a_descendant_that_changes_session_after_the_first_observed_exit(
    short_root: Path,
) -> None:
    """Identity is (pid, start token), so a later setsid cannot dodge the sweep."""

    marker = short_root / "m-late"
    interpreter = _child(short_root / "c-late.py", marker=marker, mode="late-setsid", when="after")
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=0.1,
    )
    try:
        _invoke(store, interpreter, "sha256:" + "e" * 64)
        assert not _grew(marker, seconds=0.8)
    finally:
        _reap(str(marker))


def test_a_fork_only_escapee_is_swept_by_its_argv_token(short_root: Path) -> None:
    """The only escapee that can still hold the secret fd keeps the invocation id."""

    marker = short_root / "m-fork"
    fork_child = r"""#!/usr/bin/env python3
import json, os, socket, sys, threading
invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
os.chmod(control_path, 0o600)
server.listen(2)
def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            received = connection.recv(4096).decode("utf-8")
            connection.sendall(invocation_id.encode("utf-8") if received == invocation_id else b"")
threading.Thread(target=echo, daemon=True).start()
document = json.loads(sys.stdin.buffer.read())
envelope = {
    "protocol_version": "1.0", "run_id": document["run_id"], "status": "ok",
    "output": {"echo": document["input"]["value"]}, "refusal": None, "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
pid = os.fork()
if pid == 0:
    os.setsid()
    os.close(0); os.close(1); os.close(2)
    import time
    while True:
        open("@MARKER@", "a").write("x")
        time.sleep(0.02)
open("@MARKER@.spawned", "w").write("1")
"""
    interpreter = short_root / "c-fork.py"
    interpreter.write_text(fork_child.replace("@MARKER@", str(marker)), encoding="utf-8")
    interpreter.chmod(0o755)
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=1.0,
    )
    try:
        _invoke(store, interpreter, "sha256:" + "f" * 64)
        assert not _grew(marker)
    finally:
        _reap(str(marker))


def test_kill_descendants_checks_identity_not_only_the_pid(short_root: Path) -> None:
    """A swept identity whose pid was reused between snapshot and kill is spared."""

    victim = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        stale = ProviderDescendantProcessV1(
            pid=victim.pid, process_start_time="Thu Jan  1 00:00:00 1970"
        )
        kill_descendants((stale,))
        time.sleep(0.2)
        assert victim.poll() is None, "pid-only kill would have reaped an unrelated process"
    finally:
        victim.kill()
        victim.wait()


# ------------------------------------------------------------------ U-3 publish


@pytest.mark.parametrize("where", ["mkstemp", "replace"])
@pytest.mark.parametrize("error", ["ENOSPC", "EROFS"])
def test_a_publish_write_failure_is_typed_and_leaves_no_child_or_artifact(
    short_root: Path, monkeypatch: pytest.MonkeyPatch, where: str, error: str
) -> None:
    marker = short_root / f"m-pub-{where}-{error}"
    interpreter = _child(
        short_root / f"c-pub-{where}-{error}.py",
        marker=marker,
        mode="setpgid",
        when="before",
        linger=6.0,
    )
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "9" * 64
    record_path, control_path = store.paths(invocation)
    errno_value = 28 if error == "ENOSPC" else 30
    import cruxible_core.playbill.provider_process_leases as lease_module

    if where == "mkstemp":
        monkeypatch.setattr(
            lease_module.tempfile,
            "mkstemp",
            lambda **_k: (_ for _ in ()).throw(OSError(errno_value, error)),
        )
    else:
        monkeypatch.setattr(
            lease_module.os,
            "replace",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError(errno_value, error)),
        )
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = runtime_module.subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            if list(args[0])[:1] == [str(interpreter)]:
                spawned.append(self)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", Spy)
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
            _invoke(store, interpreter, invocation)
        assert excinfo.value.code == "provider_process_lease_invalid"
        assert len(spawned) == 1
        deadline = time.monotonic() + 3.0
        while spawned[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert spawned[0].poll() is not None, "the spawned child outlived the failed publish"
        assert not record_path.exists()
        assert not control_path.exists()
        leftovers = [item for item in store.root.iterdir() if item.name != "c"]
        assert leftovers == [], leftovers
        assert not _grew(marker, seconds=0.3)
    finally:
        _reap(str(marker))


# ------------------------------------------------------------ U-I4 / U-I5


def test_a_tracker_start_failure_kills_and_reaps_the_child(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = short_root / "m-track"
    interpreter = _child(
        short_root / "c-track.py", marker=marker, mode="setpgid", when="before", linger=6.0
    )
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = runtime_module.subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().__init__(*args, **kwargs)
            if list(args[0])[:1] == [str(interpreter)]:
                spawned.append(self)

    monkeypatch.setattr(runtime_module.subprocess, "Popen", Spy)
    monkeypatch.setattr(
        runtime_module.threading.Thread,
        "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
            _invoke(store, interpreter, "sha256:" + "8" * 64)
        assert excinfo.value.code == "provider_process_lease_invalid"
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        assert list(store.root.glob("*.json")) == []
    finally:
        _reap(str(marker))


def test_a_snapshot_failure_sweeps_and_reports_without_rethrowing(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.playbill.provider_local_runtime import (
        _DescendantTracker,
        _terminate_process_group,
    )

    victim = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    doomed = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        from cruxible_core.playbill.provider_process_leases import _process_start_time

        tracker = _DescendantTracker.__new__(_DescendantTracker)
        tracker.pid = doomed.pid
        tracker.invocation_id = "sha256:" + "7" * 64
        tracker._observation_lock = runtime_module.threading.Lock()
        tracker._observed = {
            (victim.pid, _process_start_time(victim.pid)): ProviderDescendantProcessV1(
                pid=victim.pid, process_start_time=_process_start_time(victim.pid)
            )
        }
        tracker._successful_observation_count = 1
        tracker._failure = ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid", "planted snapshot failure"
        )
        monkeypatch.setattr(
            runtime_module,
            "descendant_is_live",
            lambda identity: victim.poll() is None if identity.pid == victim.pid else False,
        )
        diagnostics: list[ProviderLocalRuntimeRefused] = []
        _terminate_process_group(
            doomed,
            3.0,
            descendants=tracker,
            diagnostic_sink=diagnostics.append,
        )
        assert len(diagnostics) == 1
        assert diagnostics[0].code == "provider_process_lease_invalid"
        deadline = time.monotonic() + 2.0
        while victim.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert victim.poll() is not None, "retained descendants were not swept before the rethrow"
    finally:
        for process in (victim, doomed):
            with contextlib.suppress(Exception):
                process.kill()
                process.wait()
