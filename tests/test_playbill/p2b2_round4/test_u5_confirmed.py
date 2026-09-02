"""Round-4: independent re-establishment of the C/K/L CONFIRMED properties whose
implementation moved in the round-3 fix commits."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import (
    ProviderDescendantProcessV1,
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    descendant_is_live,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1

CONTEXT = b'{"run_id":"RUN-r4c","input":{"value":"r4c"}}'

CHILD = r"""#!/usr/bin/env python3
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
open("@DUMP@", "w").write(json.dumps({"argv": sys.argv, "env": dict(os.environ)}))
MODE = "@MODE@"
if MODE == "flood":
    sys.stdout.write("A" * 65536)
    sys.stdout.flush()
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
if MODE == "escape":
    os.close(1)
    os.close(2)
    import time
    while True:
        open("@DUMP@.alive", "a").write("x")
        time.sleep(0.02)
"""


def _child(path: Path, *, dump: Path, mode: str = "ok") -> Path:
    path.write_text(CHILD.replace("@DUMP@", str(dump)).replace("@MODE@", mode), encoding="utf-8")
    path.chmod(0o755)
    return path


def _reap(token: str) -> None:
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and token in fields[1]:
            with contextlib.suppress(OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


# ------------------------------------------------------------------ C-4


def test_c4_no_secret_reaches_argv_or_the_child_environment(short_root: Path) -> None:
    from cruxible_core.playbill.provider_local_runtime import _open_secret_channel

    dump = short_root / "dump.json"
    interpreter = _child(short_root / "c.py", dump=dump)
    store = ProviderProcessLeaseStore(short_root / "l")
    handed: list[dict[str, object]] = []
    real_popen = subprocess.Popen

    class Spy(subprocess.Popen):  # type: ignore[type-arg]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            handed.append({"argv": list(args[0]), "env": dict(kwargs.get("env") or {})})
            super().__init__(*args, **kwargs)

    runtime_module.subprocess.Popen = Spy  # type: ignore[misc]
    with _open_secret_channel({"a/b": "SUPERSECRET-C4"}, join_timeout_seconds=2.0) as fd:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=8, output_bytes=65_536),
            secret_fd=fd,
            invocation_id="sha256:" + "a" * 64,
            process_leases=store,
        )
    runtime_module.subprocess.Popen = real_popen  # type: ignore[misc]
    observed = json.loads(dump.read_text(encoding="utf-8"))
    # nothing the DAEMON hands the child carries the material ...
    spawns = [item for item in handed if item["env"]]
    assert len(spawns) == 1
    assert "SUPERSECRET-C4" not in json.dumps(handed)
    assert set(spawns[0]["env"]) == {  # type: ignore[arg-type]
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "HOME",
        "TMPDIR",
    }
    # ... and nothing the child actually observes does either.
    assert "SUPERSECRET-C4" not in json.dumps(observed)
    assert "SUPERSECRET-C4" not in json.dumps(observed["argv"])
    assert spawns[0]["argv"][0] == str(interpreter)


# ------------------------------------------------------------------ C-7


def test_c7_lease_record_integrity_with_the_new_identity_keys(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l7")
    record = store.publish("sha256:" + "c" * 64, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert oct(record.stat().st_mode)[-3:] == "600"
    assert oct((short_root / "l7").stat().st_mode)[-3:] == "700"
    raw = record.read_bytes()
    assert canonical_bytes(json.loads(raw)) == raw
    assert set(json.loads(raw)) == {
        "invocation_id",
        "pid",
        "process_group_id",
        "session_id",
        "boot_id",
        "process_start_time",
    }
    record.write_bytes(b'{"invocation_id": "x"}  ')
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.require("sha256:" + "c" * 64, timeout_seconds=0.05)
    assert caught.value.code == "provider_process_lease_invalid"


# ------------------------------------------------------------------ C-15 / K-1


def test_c15_the_aggregate_output_cap_is_real(short_root: Path) -> None:
    dump = short_root / "d15.json"
    interpreter = _child(short_root / "c15.py", dump=dump, mode="flood")
    store = ProviderProcessLeaseStore(short_root / "l15")
    started = time.monotonic()
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=30, output_bytes=4096),
            secret_fd=None,
            invocation_id="sha256:" + "5" * 64,
            process_leases=store,
        )
    assert caught.value.code == "budget_output_size"
    assert time.monotonic() - started < 10
    assert tuple(store.root.glob("*.json")) == ()


def test_k1_the_r1_escape_shape_is_still_dead(short_root: Path) -> None:
    dump = short_root / "d1.json"
    interpreter = _child(short_root / "c1.py", dump=dump, mode="escape")
    store = ProviderProcessLeaseStore(short_root / "l1")
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as caught:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=CONTEXT,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=65_536),
                secret_fd=None,
                invocation_id="sha256:" + "6" * 64,
                process_leases=store,
            )
        assert caught.value.code == "budget_wall_clock"
        alive = Path(str(dump) + ".alive")
        before = alive.stat().st_size if alive.exists() else -1
        time.sleep(0.4)
        after = alive.stat().st_size if alive.exists() else -1
        assert after == before
        assert tuple(store.root.glob("*.json")) == ()
        assert tuple(store.control_root.glob("*.sock")) == ()
    finally:
        _reap(str(dump))


# ------------------------------------------------------------------ L-1/L-3/L-5


def test_l1_a_dead_orphan_is_never_signalled(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "lo")
    victim = subprocess.Popen(
        [sys.executable, "-c", "import time\nwhile True: time.sleep(0.05)"],
        start_new_session=True,
    )
    try:
        invocation_id = "sha256:" + "b" * 64
        record_path, _control = store.paths(invocation_id)
        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation_id,
                    "pid": victim.pid,
                    "process_group_id": victim.pid,
                    "session_id": None,
                    "boot_id": None,
                    "process_start_time": None,
                }
            )
        )
        result = store.recover_all()
        assert result.recovered == ()
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert result.removed[0].invocation_id == invocation_id
        assert result.completion_invocation_ids == (invocation_id,)
        assert victim.poll() is None
    finally:
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(victim.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            victim.wait(timeout=2)


def test_l2_recover_all_is_per_record_fault_isolated(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "li")
    bad, _c = store.paths("sha256:" + "d" * 64)
    bad.write_bytes(b"{not canonical")
    good_id = "sha256:" + "e" * 64
    good, _c2 = store.paths(good_id)
    good.write_bytes(
        canonical_bytes(
            {
                "invocation_id": good_id,
                "pid": 99_999_991,
                "process_group_id": 99_999_991,
                "session_id": None,
                "boot_id": None,
                "process_start_time": None,
            }
        )
    )
    result = store.recover_all()
    assert {item.reason for item in result.removed} == {"malformed", "dead_orphan"}
    assert result.could_not_clean == ()
    assert tuple(store.root.glob("*.json")) == ()


def test_l5_the_descendant_sweep_is_identity_bound_against_pid_reuse() -> None:
    stale = ProviderDescendantProcessV1(pid=os.getpid(), process_start_time="not-the-token")
    assert descendant_is_live(stale) is False
    real = ProviderDescendantProcessV1(
        pid=os.getpid(), process_start_time=lease_module._process_start_time(os.getpid())
    )
    assert descendant_is_live(real) is True


def test_l6_exactly_one_owner_closes_the_secret_descriptor() -> None:
    import inspect

    assert "os.close(secret_fd)" not in inspect.getsource(runtime_module._run_child)
    assert "os.close(read_fd)" in inspect.getsource(runtime_module._open_secret_channel)


# ------------------------------------------------------------------ K-4 / K-9


def test_k4_startup_recovery_precedes_serving(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.server.registry import get_registry, reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(short_root))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    get_registry()
    order: list[str] = []
    import cruxible_core.server.app as app_module

    manager = get_playbill_manager()
    original = manager.recover_provider_runtime

    def recorded():  # type: ignore[no-untyped-def]
        order.append("recover")
        return original()

    monkeypatch.setattr(manager, "recover_provider_runtime", recorded)
    real_fastapi = app_module.FastAPI

    def traced(*args, **kwargs):  # type: ignore[no-untyped-def]
        order.append("fastapi")
        return real_fastapi(*args, **kwargs)

    monkeypatch.setattr(app_module, "FastAPI", traced)
    app_module.create_app()
    assert order[:2] == ["recover", "fastapi"]


def test_k10_a_deployment_path_escape_degrades_and_never_raises(short_root: Path) -> None:
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    outside = short_root.parent / (short_root.name + "-outside")
    outside.mkdir(exist_ok=True)
    (short_root / "daemon").mkdir(parents=True, exist_ok=True)
    link = short_root / "dist"
    with contextlib.suppress(FileExistsError):
        link.symlink_to(outside)
    (short_root / "daemon" / "provider-runtime.json").write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "deployments": [
                    {
                        "tag": "cruxible-provider-deployment-config-v1",
                        "deployment_digest": "sha256:" + "0" * 64,
                        "distribution_path": "dist/x.whl",
                        "lock_path": "dist/l.txt",
                        "environment_path": "dist/env",
                        "environment_manifest_path": "dist/m.json",
                        "environment_pin_key": "k",
                        "interpreter_path": "dist/env/bin/python",
                        "provider_runtime_version": "1.0.0",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    try:
        operator = ProviderRuntimeOperator(short_root)
        assert operator.deployments == {}
        assert operator.unavailable_code == "provider_process_lease_invalid"
        assert "escapes state root" in (operator.unavailable_reason or "")
    finally:
        with contextlib.suppress(OSError):
            link.unlink()
        with contextlib.suppress(OSError):
            outside.rmdir()
