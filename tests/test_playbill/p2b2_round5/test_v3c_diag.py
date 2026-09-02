"""Round-5 diagnostic: hardest cross-session escape -- setsid + exec + instant _exit."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1

CONTEXT = b'{"run_id":"RUN-r5c","input":{"value":"x"}}'

HARSH = r"""#!/usr/bin/env python3
import json, os, socket, subprocess, sys, threading
MARKER = "@MARKER@"
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
os.close(1)
os.close(2)
LOOP = (
    "import sys,time\n"
    "p=sys.argv[1]\n"
    "while True:\n"
    "    open(p,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)
subprocess.Popen(
    [sys.executable, "-c", LOOP, MARKER],
    preexec_fn=lambda: os.setsid(),
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
os._exit(0)
"""


def _reap(token: str) -> None:
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and token in fields[1]:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


@pytest.mark.parametrize("poll", [0.1, 1.0])
@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_diag_harsh_cross_session(short_root: Path, poll: float, attempt: int) -> None:
    marker = short_root / f"m-{poll}-{attempt}"
    interpreter = short_root / f"c-{poll}-{attempt}.py"
    interpreter.write_text(HARSH.replace("@MARKER@", str(marker)), encoding="utf-8")
    interpreter.chmod(0o755)
    store = ProviderProcessLeaseStore(
        short_root / "l",
        control_root=short_root / "c",
        descendant_tracker_poll_interval_seconds=poll,
    )
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + f"{attempt:064d}",
            process_leases=store,
        )
        before = marker.stat().st_size if marker.exists() else -1
        time.sleep(0.5)
        after = marker.stat().st_size if marker.exists() else -1
        print(
            f"HARSH poll={poll} attempt={attempt} survived={after > before} "
            f"records={[p.name for p in store.root.glob('*.json')]}"
        )
    finally:
        _reap(str(marker))
