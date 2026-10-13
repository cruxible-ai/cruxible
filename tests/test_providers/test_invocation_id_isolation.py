"""Concurrent real-child tests stay isolated when their invocation ids differ.

Termination sweeps every host process whose argv names the invocation id, so
two tests that shared one constant id killed each other's child under
``pytest -n``. Every real-child test takes its id from
``tests.support.invocation_ids.unique_invocation_id``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import threading
import time
from pathlib import Path

import pytest

from cruxible_core.providers.provider_local_runtime import _run_child
from cruxible_core.providers.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.providers.provider_runtime_contract import ProviderRuntimeBudgetsV1
from tests.support.invocation_ids import unique_invocation_id
from tests.support.short_temporary_root import short_temporary_directory

CONTEXT = b'{"run_id":"RUN-isolation","input":{"value":"isolation"}}'

CHILD = r"""#!/usr/bin/env python3
import json, os, socket, sys, threading, time

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
with open("@PIDFILE@", "w") as handle:
    handle.write(str(os.getpid()))
time.sleep(float("@LINGER@"))
json.dump(
    {
        "protocol_version": "1.0",
        "run_id": document["run_id"],
        "status": "ok",
        "output": {"echo": document["input"]["value"]},
        "refusal": None,
        "error": None,
        "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
    },
    sys.stdout,
    sort_keys=True,
    separators=(",", ":"),
)
sys.stdout.flush()
"""


def _child(path: Path, *, pidfile: Path, linger: float) -> Path:
    path.write_text(
        CHILD.replace("@PIDFILE@", str(pidfile)).replace("@LINGER@", str(linger)),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _store(request: pytest.FixtureRequest, root: Path) -> ProviderProcessLeaseStore:
    control = short_temporary_directory("provider-isolation-")
    request.addfinalizer(lambda: shutil.rmtree(control, ignore_errors=True))
    return ProviderProcessLeaseStore(root, control_root=control)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_unique_invocation_ids_are_stable_per_label_and_distinct_across_labels() -> None:
    first = unique_invocation_id()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)
    assert unique_invocation_id() == first
    assert unique_invocation_id("retry") == unique_invocation_id("retry") != first


def test_terminating_one_invocation_leaves_a_concurrent_child_with_another_id_alive(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Invocation A starts, finishes and is terminated while B's child still runs."""

    slow_pid = tmp_path / "slow.pid"
    fast_pid = tmp_path / "fast.pid"
    slow = _child(tmp_path / "slow-python", pidfile=slow_pid, linger=4.0)
    fast = _child(tmp_path / "fast-python", pidfile=fast_pid, linger=0.0)
    slow_store = _store(request, tmp_path / "slow-leases")
    fast_store = _store(request, tmp_path / "fast-leases")
    slow_id, fast_id = unique_invocation_id("slow"), unique_invocation_id("fast")
    assert slow_id != fast_id
    results: dict[str, object] = {}

    def run_slow() -> None:
        try:
            results["slow"] = _run_child(
                slow,
                entrypoint="demo:Provider",
                context=CONTEXT,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=20, output_bytes=65_536),
                secret_fd=None,
                invocation_id=slow_id,
                process_leases=slow_store,
            )
        except BaseException as exc:  # noqa: BLE001
            results["slow"] = exc

    worker = threading.Thread(target=run_slow)
    worker.start()
    slow_child: int | None = None
    try:
        deadline = time.monotonic() + 10.0
        while not slow_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert slow_pid.exists(), "the slow child never started"
        slow_child = int(slow_pid.read_text(encoding="utf-8"))

        fast_outcome = _run_child(
            fast,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=20, output_bytes=65_536),
            secret_fd=None,
            invocation_id=fast_id,
            process_leases=fast_store,
        )
        assert json.loads(fast_outcome.stdout)["status"] == "ok"
        assert fast_pid.exists()
        assert _alive(slow_child), "terminating one invocation killed another's child"

        worker.join(timeout=20.0)
        outcome = results["slow"]
        assert not isinstance(outcome, BaseException), outcome
        assert json.loads(outcome.stdout)["status"] == "ok"  # type: ignore[attr-defined]
    finally:
        worker.join(timeout=20.0)
        if slow_child is not None:
            with contextlib.suppress(OSError):
                os.kill(slow_child, signal.SIGKILL)
