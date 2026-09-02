"""Round-5: the deterministic half of fence_scope, bounded detail, and accessors."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

import cruxible_core.runtime.playbill_manager as manager_module
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderProcessLeaseStore
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

CONTEXT = b'{"run_id":"RUN-r5x","input":{"value":"x"}}'

SAME_GROUP_CHILD = r"""#!/usr/bin/env python3
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
LOOP = (
    "import sys,time\n"
    "p=sys.argv[1]\n"
    "while True:\n"
    "    open(p,'a').write('x')\n"
    "    time.sleep(0.02)\n"
)
subprocess.Popen(
    [sys.executable, "-c", LOOP, MARKER],
    close_fds=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
envelope = {
    "protocol_version": "1.0", "run_id": document["run_id"], "status": "ok",
    "output": {"echo": document["input"]["value"]}, "refusal": None, "error": None,
    "trace": {"endpoints_contacted": [], "events": [], "metrics": {}},
}
json.dump(envelope, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
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


def test_c5_a_same_group_descendant_dies_with_the_group_on_the_success_path(
    short_root: Path,
) -> None:
    marker = short_root / "m-same"
    interpreter = short_root / "c-same.py"
    interpreter.write_text(SAME_GROUP_CHILD.replace("@MARKER@", str(marker)), encoding="utf-8")
    interpreter.chmod(0o755)
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "5" * 64,
            process_leases=store,
        )
        before = marker.stat().st_size if marker.exists() else -1
        time.sleep(0.5)
        after = marker.stat().st_size if marker.exists() else -1
        assert after == before, "the in-group descendant outlived the group kill"
        assert list(store.root.glob("*.json")) == []
    finally:
        _reap(str(marker))


def test_ui3_the_degraded_detail_is_bounded_by_latest_codes_and_count(
    short_root: Path,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    for index in range(200):
        operator.mark_unavailable(
            "provider_process_group_survived_recovery", f"record {index} is stuck"
        )
    detail = operator.unavailable_reason or ""
    assert detail.startswith("latest=provider_process_group_survived_recovery: record 199")
    assert detail.endswith("codes=[provider_process_group_survived_recovery]; count=200")
    assert len(detail) < 200, len(detail)
    operator.mark_unavailable("provider_process_lease_invalid", "another cause")
    detail = operator.unavailable_reason or ""
    assert "codes=[provider_process_group_survived_recovery,provider_process_lease_invalid]" in (
        detail
    )
    assert len(detail) < 250, len(detail)


def test_the_manager_accessors_never_raise_when_construction_explodes(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.server.registry import get_registry, reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(short_root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    get_registry()

    def explode(self: object, state_root: Path) -> None:
        raise RuntimeError("planted non-OSError construction failure")

    monkeypatch.setattr(manager_module.ProviderRuntimeOperator, "__init__", explode)
    manager = get_playbill_manager()
    first = manager.provider_runtime_operator()
    assert first.lane_status()[0] == "unavailable"
    assert first.lane_status()[1] == "provider_runtime_recovery_failed"
    assert manager.cached_provider_runtime_operator() is first
    # And startup recovery on that object still returns without raising.
    assert manager.recover_provider_runtime() is not None


def test_both_status_surfaces_read_the_same_non_raising_accessor() -> None:
    import cruxible_core.runtime.host_api as host_api_module
    import cruxible_core.runtime.playbill_api as playbill_api_module

    for module in (host_api_module, playbill_api_module):
        text = Path(module.__file__ or "").read_text("utf-8")
        assert "provider_runtime_operator().lane_status()" in text
