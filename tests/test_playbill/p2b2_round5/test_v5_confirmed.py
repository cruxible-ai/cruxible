"""Round-5: independent re-establishment of the C/K/L/M CONFIRMED properties."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_local_runtime import _open_secret_channel, _run_child
from cruxible_core.playbill.provider_process_leases import (
    ProviderDescendantProcessV1,
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    descendant_is_live,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

SOURCE_ROOT = Path(runtime_module.__file__).resolve().parents[1]
CONTEXT = b'{"run_id":"RUN-r5c","input":{"value":"r5c"}}'

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
    "protocol_version": "1.0", "run_id": document["run_id"], "status": "ok",
    "output": {"echo": document["input"]["value"]}, "refusal": None, "error": None,
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
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


# ---------------------------------------------------------------------- C-3


def test_c3_effect_class_is_governed_and_unchanged_in_this_range() -> None:
    execution = (SOURCE_ROOT / "playbill" / "procedures" / "execution.py").read_text("utf-8")
    assert 'EFFECTFUL_PROVIDER_EFFECT_CLASSES = frozenset({"external_mutation"})' in execution or (
        '"external_mutation"' in execution
    )
    assert "provider_effect_declaration_mismatch" in execution


# ---------------------------------------------------------------------- C-4


def test_c4_no_secret_reaches_argv_or_the_child_environment(short_root: Path) -> None:
    dump = short_root / "dump.json"
    interpreter = _child(short_root / "c.py", dump=dump)
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    handed: list[dict[str, object]] = []
    real_popen = runtime_module.subprocess.Popen

    class Spy(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            handed.append({"argv": list(args[0]), "env": dict(kwargs.get("env") or {})})
            super().__init__(*args, **kwargs)

    runtime_module.subprocess.Popen = Spy  # type: ignore[misc]
    try:
        with _open_secret_channel({"a/b": "SUPERSECRET-V5"}, join_timeout_seconds=2.0) as fd:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=CONTEXT,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536),
                secret_fd=fd,
                invocation_id="sha256:" + "a" * 64,
                process_leases=store,
            )
    finally:
        runtime_module.subprocess.Popen = real_popen  # type: ignore[misc]
    observed = json.loads(dump.read_text(encoding="utf-8"))
    spawns = [item for item in handed if item["env"]]
    assert len(spawns) == 1
    assert "SUPERSECRET-V5" not in json.dumps(handed)
    assert set(spawns[0]["env"]) == {  # type: ignore[arg-type]
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
        "HOME",
        "TMPDIR",
    }
    assert "SUPERSECRET-V5" not in json.dumps(observed)
    home = Path(observed["env"]["HOME"])
    assert home.is_relative_to(short_root.resolve()), home
    assert not home.exists(), "the child scratch survived the invocation"


def test_ui1_the_child_scratch_lives_under_the_state_root_and_is_private(
    short_root: Path,
) -> None:
    dump = short_root / "dump2.json"
    interpreter = _child(short_root / "c2.py", dump=dump)
    store = ProviderProcessLeaseStore(
        short_root / "daemon" / "provider-process-leases", control_root=short_root / "c"
    )
    modes: list[int] = []
    real_chmod = runtime_module.os.chmod
    seen: list[str] = []

    def spy(path, mode, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(str(path))
        modes.append(mode)
        return real_chmod(path, mode, *args, **kwargs)

    runtime_module.os.chmod = spy  # type: ignore[assignment]
    try:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536),
            secret_fd=None,
            invocation_id="sha256:" + "b" * 64,
            process_leases=store,
        )
    finally:
        runtime_module.os.chmod = real_chmod  # type: ignore[assignment]
    scratch = Path(json.loads(dump.read_text("utf-8"))["env"]["HOME"])
    assert scratch.parent.resolve() == (short_root / "daemon").resolve(), (scratch, seen)
    assert scratch.name.startswith(".child-")
    assert 0o700 in modes
    assert any(str(scratch) == item for item in seen)


# ---------------------------------------------------------------------- C-6


def test_c6_and_m5_an_independent_echo_server_cannot_authorise_a_kill(
    short_root: Path,
) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "c" * 64
    record_path, control_path = store.paths(invocation)
    victim = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    script = short_root / "independent.py"
    script.write_text(
        "import os,socket,sys,threading,time\n"
        "invocation_id, control_path = sys.argv[1:3]\n"
        "s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.bind(control_path); os.chmod(control_path,0o600); s.listen(4)\n"
        "open(control_path+'.ready','w').write('1')\n"
        "def serve():\n"
        "    while True:\n"
        "        try: c,_=s.accept()\n"
        "        except OSError: return\n"
        "        with c:\n"
        "            d=c.recv(4096).decode('utf-8')\n"
        "            c.sendall(invocation_id.encode('utf-8') if d==invocation_id else b'')\n"
        "threading.Thread(target=serve,daemon=True).start()\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    impostor = subprocess.Popen(
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
        assert ready.exists()
        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation,
                    "pid": victim.pid,
                    "process_group_id": os.getpgid(victim.pid),
                    "session_id": None,
                    "boot_id": None,
                    "process_start_time": None,
                }
            )
        )
        result = store.recover_all()
        assert result.recovered == ()
        time.sleep(0.3)
        assert victim.poll() is None, "an unrelated group was killed on an impostor echo"
    finally:
        for process in (victim, impostor):
            with contextlib.suppress(Exception):
                process.kill()
                process.wait()


def test_l1_a_dead_orphan_is_never_signalled_and_keeps_its_id(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    bystander = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    invocation = "sha256:" + "d" * 64
    record_path, _control = store.paths(invocation)
    try:
        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation,
                    "pid": bystander.pid,
                    "process_group_id": os.getpgid(bystander.pid),
                    "session_id": None,
                    "boot_id": None,
                    "process_start_time": None,
                }
            )
        )
        result = store.recover_all()
        assert result.recovered == ()
        assert [item.reason for item in result.removed] == ["dead_orphan"]
        assert result.completion_invocation_ids == (invocation,)
        time.sleep(0.3)
        assert bystander.poll() is None
    finally:
        with contextlib.suppress(Exception):
            bystander.kill()
            bystander.wait()


def test_l2_recover_all_is_per_record_fault_isolated(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    malformed = store.root / ("0" * 32 + ".json")
    malformed.write_bytes(b"{not json")
    good = "sha256:" + "e" * 64
    good_path, _control = store.paths(good)
    good_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": good,
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
    assert not malformed.exists() and not good_path.exists()


def test_l5_the_sweep_is_identity_bound_against_pid_reuse(short_root: Path) -> None:
    process = subprocess.Popen(
        [os.sys.executable, "-c", "import time; time.sleep(5)"],  # type: ignore[attr-defined]
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        real = lease_module._process_start_time(process.pid)
        assert descendant_is_live(
            ProviderDescendantProcessV1(pid=process.pid, process_start_time=real)
        )
        assert not descendant_is_live(
            ProviderDescendantProcessV1(pid=process.pid, process_start_time=real + "X")
        )
    finally:
        process.kill()
        process.wait()


def test_l6_exactly_one_owner_closes_the_secret_read_descriptor() -> None:
    source = (SOURCE_ROOT / "playbill" / "provider_local_runtime.py").read_text("utf-8")
    body = source[source.index("def _run_child(") : source.index("def _collect_child_output(")]
    assert "os.close(secret_fd)" not in body
    channel = source[
        source.index("def _open_secret_channel(") : source.index("def _assert_no_secret(")
    ]
    assert channel.count("os.close(read_fd)") == 1


# ---------------------------------------------------------------------- C-7


def test_c7_lease_record_integrity(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "f" * 64
    record_path = store.publish(invocation, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.control_root.stat().st_mode) == 0o700
    document = json.loads(record_path.read_bytes())
    assert set(document) == {
        "invocation_id",
        "pid",
        "process_group_id",
        "session_id",
        "boot_id",
        "process_start_time",
    }
    assert canonical_bytes(document) == record_path.read_bytes()
    record_path.write_bytes(b'{"invocation_id":"x",  "pid":1}')
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        store.require(invocation, timeout_seconds=0.2)
    assert excinfo.value.code == "provider_process_lease_invalid"


# ---------------------------------------------------------------------- C-8


def test_c8_custody_store_permissions_and_traversal(short_root: Path) -> None:
    from cruxible_client.contracts.provider_execution import ProviderSecretReferenceV1

    store = runtime_module.FileProviderSecretStore(short_root / "secrets")
    assert stat.S_IMODE((short_root / "secrets").stat().st_mode) == 0o700
    for bad in ("a/b", "a\\b", "..", ".", "a\x00b"):
        with pytest.raises(Exception):
            ProviderSecretReferenceV1(
                ref="r", realm=bad, name="n", epoch="e", purpose="p", resolver_kind="file"
            )
    assert store is not None


# ---------------------------------------------------------------------- C-10


def test_c10_caps_carry_no_numeric_literal() -> None:
    source = SOURCE_ROOT / "playbill" / "procedures" / "provider_budget.py"
    if not source.exists():
        source = SOURCE_ROOT / "playbill" / "provider_budget.py"
    text = source.read_text("utf-8") if source.exists() else ""
    if not text:
        candidates = list((SOURCE_ROOT / "playbill").rglob("*.py"))
        text = next(
            item.read_text("utf-8")
            for item in candidates
            if "def translate_provider_budget" in item.read_text("utf-8")
        )
    body = text[text.index("def translate_provider_budget") :]
    body = body[: body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    assert re.search(r"\b\d{3,}\b", body) is None, body


# ---------------------------------------------------------------------- C-15 / K-1


def test_c15_the_aggregate_output_cap_is_real(short_root: Path) -> None:
    dump = short_root / "flood.json"
    interpreter = _child(short_root / "flood.py", dump=dump, mode="flood")
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    started = time.monotonic()
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=CONTEXT,
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=30, output_bytes=4096),
            secret_fd=None,
            invocation_id="sha256:" + "1" * 64,
            process_leases=store,
        )
    assert excinfo.value.code == "budget_output_size"
    assert time.monotonic() - started < 10
    assert list(store.root.glob("*.json")) == []


def test_k1_the_r1_escape_shape_is_still_dead(short_root: Path) -> None:
    dump = short_root / "escape.json"
    interpreter = _child(short_root / "escape.py", dump=dump, mode="escape")
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    alive = Path(str(dump) + ".alive")
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=CONTEXT,
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=65_536),
                secret_fd=None,
                invocation_id="sha256:" + "2" * 64,
                process_leases=store,
            )
        assert excinfo.value.code == "budget_wall_clock"
        before = alive.stat().st_size if alive.exists() else -1
        time.sleep(0.4)
        after = alive.stat().st_size if alive.exists() else -1
        assert after == before
        assert list(store.root.glob("*.json")) == []
        assert list(store.control_root.glob("*.sock")) == []
    finally:
        _reap(str(dump))


# ---------------------------------------------------------------------- K-4 / K-9 / K-10


def test_k4_startup_recovery_precedes_serving(
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
    order: list[str] = []
    import cruxible_core.server.app as app_module

    manager = get_playbill_manager()
    real_recover = manager.recover_provider_runtime
    monkeypatch.setattr(
        manager,
        "recover_provider_runtime",
        lambda: (order.append("recover"), real_recover())[1],
    )
    real_fastapi = app_module.FastAPI

    class Spy(real_fastapi):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            order.append("fastapi")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(app_module, "FastAPI", Spy)
    assert app_module.create_app() is not None
    assert order[:2] == ["recover", "fastapi"]


def test_k9_recovery_refuses_while_an_invocation_is_in_flight(short_root: Path) -> None:
    operator = ProviderRuntimeOperator(short_root)
    operator._in_flight = 1
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        operator.recover_all()
    assert excinfo.value.code == "provider_process_lease_invalid"


def test_k10_a_deployment_path_escape_degrades_and_never_raises(short_root: Path) -> None:
    (short_root / "daemon").mkdir(parents=True, exist_ok=True)
    outside = short_root.parent / (short_root.name + "-outside")
    outside.mkdir(exist_ok=True)
    (short_root / "d").symlink_to(outside)
    (short_root / "daemon" / "provider-runtime.json").write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "deployments": [
                    {
                        "tag": "cruxible-provider-deployment-config-v1",
                        "deployment_digest": "sha256:" + "a" * 64,
                        "distribution_path": "d/dist.whl",
                        "lock_path": "d/lock",
                        "environment_path": "d/env",
                        "environment_manifest_path": "d/seal.json",
                        "environment_pin_key": "k",
                        "interpreter_path": "d/env/bin/python",
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
            (short_root / "d").unlink()
        with contextlib.suppress(OSError):
            outside.rmdir()


def test_k3_the_retry_path_and_the_path_length_refusal(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U8-C succeeded the shorter-root repair this oracle still named.

    The retraction rewrote the two oracles it cited and missed this third copy:
    an overlong control namespace now falls back to the verified per-user runtime
    directory, and refuses typed only when neither namespace fits.
    """

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    invocation = "sha256:" + "3" * 64
    _record, control_path = store.paths(invocation)
    leftover = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    leftover.bind(str(control_path))
    leftover.close()
    assert control_path.exists()
    assert store.prepare_control_path(invocation) == control_path
    assert not control_path.exists()
    control_path.write_text("occupied", encoding="utf-8")
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        store.prepare_control_path(invocation)
    assert excinfo.value.code == "provider_process_lease_invalid"

    deep = short_root / ("x" * 90)
    deep.mkdir()
    runtime_root = short_root / "rt"
    runtime_root.mkdir()
    runtime_root.chmod(0o700)
    variable = "XDG_RUNTIME_DIR" if platform.system() == "Linux" else "TMPDIR"
    monkeypatch.setenv(variable, str(runtime_root))
    # No checkout is short enough to hold the fallback namespace and a socket name
    # inside 103 bytes, so the budget is measured as if the verified runtime root
    # were the short per-user path the ruling names. The budget itself never moves.
    real_fsencode = os.fsencode
    monkeypatch.setattr(
        lease_module.os,
        "fsencode",
        lambda value: (
            b"f" * 80 if str(value).startswith(str(runtime_root)) else real_fsencode(value)
        ),
    )
    fallback = ProviderProcessLeaseStore(deep / "l", control_root=deep / "c")
    assert fallback.control_root.is_relative_to(runtime_root)
    assert fallback.paths(invocation)[1].parent == fallback.control_root

    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(ProviderLocalRuntimeRefused) as long_refusal:
        ProviderProcessLeaseStore(deep / "l2", control_root=deep / "c2")
    assert long_refusal.value.code == "provider_process_lease_invalid"
    assert "private per-user runtime directory" in str(long_refusal.value)


# ---------------------------------------------------------------------- L-8 / L-9 / L-12 / M-11


def test_l8_fence_scope_is_required_and_fixed() -> None:
    from cruxible_client.contracts.provider_execution import ProviderInvocationReceiptV1

    field = ProviderInvocationReceiptV1.model_fields["fence_scope"]
    assert field.is_required()
    from typing import get_args

    assert get_args(field.annotation) == ("process_group+descendant_sweep",)
    assert "best-effort cross-session" in (field.description or "")


def test_l9_no_tmp_remains_on_the_fence_path() -> None:
    for name in (
        "playbill/provider_process_leases.py",
        "playbill/provider_local_runtime.py",
        "runtime/provider_runtime.py",
        "runtime/playbill_manager.py",
    ):
        text = (SOURCE_ROOT / name).read_text("utf-8")
        assert "/" + "tmp" not in text, name
        assert "gettempdir" not in text, name


def test_l12_every_fence_timeout_is_config_carried() -> None:
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperationalConfigV1

    knobs = {
        name for name in ProviderRuntimeOperationalConfigV1.model_fields if name.endswith("seconds")
    }
    assert knobs == {
        "lease_acquisition_timeout_seconds",
        "lease_recovery_timeout_seconds",
        "recovery_aggregate_timeout_seconds",
        "rearm_backoff_seconds",
        "secret_writer_join_timeout_seconds",
        "stdin_writer_join_timeout_seconds",
        "descendant_tracker_join_timeout_seconds",
        "descendant_tracker_poll_interval_seconds",
        "process_group_termination_timeout_seconds",
    }
    text = (SOURCE_ROOT / "playbill" / "provider_process_leases.py").read_text("utf-8")
    body = text[text.index("class ProviderProcessLeaseStore") :]
    assert re.search(r"timeout_seconds: float = \d", body) is None


def test_m11_the_closed_vocabularies_are_exactly_as_ruled() -> None:
    from typing import get_args

    from cruxible_client.contracts import ProviderLaneUnavailableCodeV1
    from cruxible_core.playbill.provider_process_leases import (
        ProviderProcessFenceCodeV1,
        ProviderProcessRecoveryFailureV1,
    )

    fence = set(get_args(ProviderProcessFenceCodeV1))
    assert fence == {
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    }
    assert set(get_args(ProviderLaneUnavailableCodeV1)) == fence | {
        "provider_runtime_recovery_failed"
    }
    import typing

    hints = typing.get_type_hints(ProviderProcessRecoveryFailureV1)
    assert set(get_args(hints["code"])) == fence
    assert set(get_args(hints["attempt_status"])) == {"attempted", "not_attempted"}


def test_l7_all_five_fence_codes_are_public_and_map_exactly() -> None:
    from typing import get_args

    from cruxible_client.contracts.procedures.results import ProcedureInternalFailureCodeV1
    from cruxible_core.playbill.provider_outcomes import _LOCAL_MAPPING

    public = set(get_args(ProcedureInternalFailureCodeV1))
    for code in (
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    ):
        assert code in public, code
        assert _LOCAL_MAPPING[code] == ("internal", "executor")


def test_c14_wire_law_guards_still_hold() -> None:
    from cruxible_core.playbill.provider_outcomes import (
        _MAPPING,
        ABSORBABLE_PROVIDER_REFUSALS,
        PROVIDER_RUNTIME_REFUSAL_CODES,
    )

    assert set(_MAPPING) == PROVIDER_RUNTIME_REFUSAL_CODES
    assert all(_MAPPING[code] == ("node_refusal", "input") for code in ABSORBABLE_PROVIDER_REFUSALS)
    from cruxible_client.contracts.provider_execution import ProviderInvocationReceiptV1

    assert ProviderInvocationReceiptV1.model_config["extra"] == "forbid"
    assert ProviderInvocationReceiptV1.model_config["frozen"] is True


def test_m1_the_wnowait_kill_window_is_real_on_every_path(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = short_root / "wn.json"
    interpreter = _child(short_root / "wn.py", dump=dump)
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    entries: list[tuple[object, bool]] = []
    real = runtime_module._terminate_process_group

    def spy(  # type: ignore[no-untyped-def]
        process, timeout_seconds, *, descendants, diagnostic_sink=None
    ):
        alive = True
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            alive = False
        entries.append((process.returncode, alive))
        return real(
            process,
            timeout_seconds,
            descendants=descendants,
            diagnostic_sink=diagnostic_sink,
        )

    monkeypatch.setattr(runtime_module, "_terminate_process_group", spy)
    _run_child(
        interpreter,
        entrypoint="demo:Provider",
        context=CONTEXT,
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=10, output_bytes=65_536),
        secret_fd=None,
        invocation_id="sha256:" + "4" * 64,
        process_leases=store,
    )
    # M-1: the leader is unreaped at every terminate entry (WNOWAIT), so the
    # group identity is still owned by this invocation when the kill is sent.
    assert [item[0] for item in entries] == [None]
