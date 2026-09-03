"""Round 3 - re-establish the round-1 (C-*) and round-2 (K-*) CONFIRMED lists."""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
import cruxible_core.playbill.provider_process_leases as lease_module
import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.results import (
    ProcedureInternalFailureCodeV1,
    ProcedureOperationalFailureCodeV1,
)
from cruxible_client.contracts.provider_execution import (
    ProviderInvocationReceiptV1,
    ProviderSecretReferenceV1,
)
from cruxible_core.playbill.procedures.execution import ProcedureExecutor
from cruxible_core.playbill.provider_classifiers import ProviderBucketClassifierRegistry
from cruxible_core.playbill.provider_local_runtime import (
    FileProviderSecretStore,
    ProviderLocalRuntimeRefused,
    _run_child,
    provider_environment_secret_key,
)
from cruxible_core.playbill.provider_outcomes import (
    ABSORBABLE_PROVIDER_REFUSALS,
    map_provider_refusal,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderProcessLeaseStore,
    ProviderProcessRecoveryFailureV1,
)
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from tests.test_playbill._p2b1_support import install_demo_classifier
from tests.test_playbill.test_procedure_execution import _Authority, _Contracts
from tests.test_playbill.test_provider_invocation_journal import (
    _accepted_one_provider,
    _Invoker,
    _prepared_v5,
)

from ._child import write_child

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

ESCAPE_SOURCE = r"""#!/usr/bin/env python3
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
json.dump({"protocol_version":"1.0","run_id":document["run_id"],"status":"ok",
           "output":{"echo":"x"},"refusal":None,"error":None,
           "trace":{"endpoints_contacted":[],"events":[],"metrics":{}}},
          sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.flush()
os.close(1)
os.close(2)
marker = "@MARKER@"
while True:
    open(marker, "a").write("x")
    time.sleep(0.02)
"""

FLOOD_SOURCE = r"""#!/usr/bin/env python3
import os, socket, sys, threading
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
block = b"z" * 65536
while True:
    os.write(1, block)
"""


def _write(path: Path, source: str, **replacements: str) -> Path:
    for key, value in replacements.items():
        source = source.replace(f"@{key}@", value)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def _reap(marker: Path) -> None:
    completed = subprocess.run(
        ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
    )
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and str(marker) in fields[1]:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(int(fields[0]), signal.SIGKILL)


# ------------------------------------------------------------------ K-1 / R-1


def test_k1_the_escape_child_is_typed_killed_and_leaves_no_lease(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    marker = short_root / "escape"
    interpreter = _write(short_root / "escape.py", ESCAPE_SOURCE, MARKER=str(marker))
    try:
        with pytest.raises(ProviderLocalRuntimeRefused) as caught:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=b'{"run_id":"RUN-esc","input":{"value":"x"}}',
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=65_536),
                secret_fd=None,
                invocation_id="sha256:" + "a" * 64,
                process_leases=store,
            )
        assert caught.value.code == "budget_wall_clock"
        assert tuple(store.root.glob("*.json")) == ()
        assert tuple(store.control_root.glob("*.sock")) == ()
        before = marker.stat().st_size
        time.sleep(0.4)
        assert marker.stat().st_size == before, "the escape child survived the fence"
    finally:
        _reap(marker)


# ------------------------------------------------------------------ C-15


def test_c15_the_aggregate_output_cap_is_real(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    interpreter = _write(short_root / "flood.py", FLOOD_SOURCE)
    started = time.monotonic()
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        _run_child(
            interpreter,
            entrypoint="demo:Provider",
            context=b'{"run_id":"RUN-flood","input":{"value":"x"}}',
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=30, output_bytes=4096),
            secret_fd=None,
            invocation_id="sha256:" + "b" * 64,
            process_leases=store,
        )
    assert caught.value.code == "budget_output_size"
    assert time.monotonic() - started < 15
    assert tuple(store.root.glob("*.json")) == ()


# ------------------------------------------------------------------ C-4 / C-7


def test_c7_lease_record_integrity(short_root: Path) -> None:
    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    record = store.publish("sha256:" + "c" * 64, pid=os.getpid(), process_group_id=os.getpgid(0))
    assert oct(record.stat().st_mode)[-3:] == "600"
    assert oct((short_root / "l").stat().st_mode)[-3:] == "700"
    raw = record.read_bytes()
    assert canonical_bytes(json.loads(raw)) == raw
    document = json.loads(raw)
    assert set(document) == {
        "invocation_id",
        "pid",
        "process_group_id",
        "session_id",
        "boot_id",
        "process_start_time",
    }
    # A record naming another invocation is refused.
    other_record, _control = store.paths("sha256:" + "d" * 64)
    other_record.write_bytes(raw)
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.require("sha256:" + "d" * 64, timeout_seconds=0.2)
    assert caught.value.code == "provider_process_lease_invalid"
    # Non-canonical bytes are refused.
    record.write_bytes(b'{"pid": 1,\n "invocation_id": "x"}')
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        store.require("sha256:" + "c" * 64, timeout_seconds=0.2)
    assert caught.value.code == "provider_process_lease_invalid"


def test_c4_no_secret_reaches_argv_env_or_the_child_environment() -> None:
    source = inspect.getsource(runtime_module._run_child)
    environment = re.search(r"environment = \{(.*?)\n    \}", source, re.S)
    assert environment is not None
    assert "CRUXIBLE_PROVIDER_SECRET" not in environment.group(1)
    assert set(re.findall(r'"([A-Z_]+)":', environment.group(1))) == {
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONNOUSERSITE",
        "PYTHONDONTWRITEBYTECODE",
    }
    command = re.search(r"command = \[(.*?)\]", source, re.S)
    assert command is not None
    assert "secret_resolver" not in command.group(1).lower()
    assert "secret_values" not in command.group(1).lower()
    scan = inspect.getsource(runtime_module._assert_no_secret)
    assert "raw[::-1]" in scan and "b64encode" in scan
    invoke = inspect.getsource(runtime_module.LocalProviderExecutionDriver.invoke)
    assert invoke.count("_assert_no_secret") == 3
    assert 'where="provider stdout"' in invoke and 'where="provider stderr"' in invoke
    assert 'where="run context"' in invoke


# ------------------------------------------------------------------ C-8


def test_c8_custody_store_permissions_and_traversal(short_root: Path) -> None:
    store = FileProviderSecretStore(short_root / "secrets")
    assert oct((short_root / "secrets").stat().st_mode)[-3:] == "700"
    for bad in ("a/b", "a\\b", "..", ".", "a\x00b"):
        with pytest.raises(Exception):
            ProviderSecretReferenceV1(
                resolver_kind="environment", realm=bad, name="n", epoch="e", purpose="p"
            )
    key_a = provider_environment_secret_key(
        ProviderSecretReferenceV1(
            resolver_kind="environment", realm="billing", name="api_key", epoch="v1", purpose="p"
        )
    )
    key_b = provider_environment_secret_key(
        ProviderSecretReferenceV1(
            resolver_kind="environment", realm="billing", name="api", epoch="key_v1", purpose="p"
        )
    )
    assert key_a != key_b
    assert store is not None


# ------------------------------------------------------------------ C-10 / C-14


def test_c10_caps_carry_no_numeric_literal() -> None:
    source = inspect.getsource(runtime_module.translate_provider_budget)
    assert not re.search(r"\b\d{3,}\b", source), source


def test_c14_wire_law_guards_still_hold() -> None:
    from cruxible_core.playbill import provider_outcomes
    from cruxible_core.playbill.provider_runtime_contract import (
        ProviderRuntimeResultEnvelopeV1,
        ProviderRuntimeRunContextV1,
    )

    for model in (ProviderRuntimeResultEnvelopeV1, ProviderRuntimeRunContextV1):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
    guard = inspect.getsource(provider_outcomes)
    assert "Provider outcome mapping does not equal the mirrored runtime vocabulary" in guard
    assert all(
        provider_outcomes._MAPPING[code] == ("node_refusal", "input")
        for code in ABSORBABLE_PROVIDER_REFUSALS
    )


# ------------------------------------------------------------------ N-4 / task 7


def test_the_three_fence_codes_are_public_and_project_exactly(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes = (
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    )
    public = set(get_args(ProcedureInternalFailureCodeV1)) | set(
        get_args(ProcedureOperationalFailureCodeV1)
    )
    assert [code for code in codes if code not in public] == []
    for code in codes:
        assert map_provider_refusal(code, message="m", detail={}).code == code

    for code in codes[2:]:
        terminal = _project(short_root, monkeypatch, code)
        assert terminal.code == code, (code, terminal.code)


def _project(root: Path, monkeypatch: pytest.MonkeyPatch, code: str):  # type: ignore[no-untyped-def]
    class _Refusing:
        def bind_provider(self, *, occurrence):  # type: ignore[no-untyped-def]
            return _Invoker().bind_provider(occurrence=occurrence)

        def invoke_provider(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise ProviderLocalRuntimeRefused(code, "fence refusal")

    accepted = _accepted_one_provider()
    workspace = root / code
    workspace.mkdir(parents=True, exist_ok=True)
    prepared, fixture = _prepared_v5(accepted, workspace)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=_Refusing(),
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    records = tuple(
        fixture.journal.all_records(
            prepared.admission.journal_stream, prepared.admission.journal_partition_id
        )
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_a, **_k: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert state.terminal is not None
    return state.terminal


def test_a_degraded_lane_refusal_preserves_its_typed_reason_at_projection(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's typed reason reaches `ProviderLocalRuntimeRefused.details`, but
    `provider_unavailable` maps to a NODE refusal and `_RunRefusal` carries no detail,
    so the projected run never names why the lane is down."""

    from cruxible_core.runtime.provider_runtime import _UnavailableProviderRuntimeInvoker

    invoker = _UnavailableProviderRuntimeInvoker(
        code="provider_process_lease_invalid",
        detail="provider_process_lease_invalid: too long",
    )

    accepted = _accepted_one_provider()
    workspace = short_root / "degraded"
    workspace.mkdir(parents=True)
    prepared, fixture = _prepared_v5(accepted, workspace)
    registry = ProviderBucketClassifierRegistry()
    install_demo_classifier(registry)
    ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        provider_runtime_invoker=invoker,
        provider_classifier_registry=registry,
    ).execute(prepared, accepted)
    records = tuple(
        fixture.journal.all_records(
            prepared.admission.journal_stream, prepared.admission.journal_partition_id
        )
    )
    monkeypatch.setattr(procedure_run_service, "_records_for_run", lambda *_a, **_k: records)

    class _Instance:
        def body_store(self):  # type: ignore[no-untyped-def]
            return fixture.bodies

    state = procedure_run_service._state_from_records(
        _Instance(),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
    )
    assert state.terminal is not None
    assert state.terminal.code == "provider_unavailable"
    rendered = json.dumps(state.terminal.model_dump(mode="json"))
    assert "too long" in rendered, rendered
    assert "provider_process_lease_invalid" in rendered


# ------------------------------------------------------------------ task 7


def test_fence_scope_is_required_and_fixed() -> None:
    field = ProviderInvocationReceiptV1.model_fields["fence_scope"]
    assert field.is_required()
    assert get_args(field.annotation) == ("process_group+descendant_sweep",)


def test_recovery_failure_code_is_the_closed_fence_vocabulary() -> None:
    assert set(get_args(get_type_hints(ProviderProcessRecoveryFailureV1)["code"])) == {
        "provider_process_lease_invalid",
        "provider_process_lease_missing",
        "provider_process_lease_echo_failed",
        "provider_process_lease_echo_mismatch",
        "provider_process_group_survived_recovery",
    }


def test_no_tmp_path_remains_on_the_provider_fence_path() -> None:
    for name in (
        "src/cruxible_core/playbill/provider_process_leases.py",
        "src/cruxible_core/playbill/provider_local_runtime.py",
        "src/cruxible_core/runtime/provider_runtime.py",
        "src/cruxible_core/runtime/playbill_manager.py",
    ):
        text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
        assert "/" + "tmp" not in text, name


def test_the_path_length_selects_the_private_namespace_or_refuses_typed(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Succeeds the retracted P2-B2 refusal oracle (U8, manager 2026-09-02).

    The retracted law said an overlong control path refuses and names the
    shorter-root repair. U8 replaced it with a fallback to a verified per-user
    private runtime namespace, refusing typed only when neither namespace fits.
    This oracle was left asserting the retracted law and was red at the P2-B5
    tip; it now asserts both halves of the replacement.
    """

    deep = short_root / ("d" * 40) / ("e" * 40)
    deep.mkdir(parents=True)
    store_root = deep / "l"
    runtime_root = short_root / "r"
    runtime_root.mkdir(mode=0o700)
    monkeypatch.setattr(lease_module.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_root))
    # The fixture's runtime root sits under the repository, whose absolute path
    # is environment-dependent, so the namespace budget is measured against a
    # fixed-width stand-in for a real per-user runtime directory.
    real_fsencode = os.fsencode
    monkeypatch.setattr(
        lease_module.os,
        "fsencode",
        lambda value: (
            b"f" * 80 if str(value).startswith(str(runtime_root)) else real_fsencode(value)
        ),
    )

    store = ProviderProcessLeaseStore(store_root, control_root=deep / "c")
    assert store.control_root.is_relative_to(runtime_root)
    assert store.paths("sha256:" + "f" * 64)[1].parent == store.control_root

    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        ProviderProcessLeaseStore(store_root, control_root=deep / "c")
    assert caught.value.code == "provider_process_lease_invalid"
    assert "private per-user runtime" in str(caught.value)


# ------------------------------------------------------------------ K-4


def test_k4_startup_recovery_precedes_route_registration(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.server import app as app_module
    from cruxible_core.server.registry import reset_registry

    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(short_root / "s"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()

    order: list[str] = []
    manager = get_playbill_manager()
    real = type(manager).recover_provider_runtime

    def traced(self):  # type: ignore[no-untyped-def]
        order.append("recover")
        return real(self)

    real_fastapi = app_module.FastAPI

    def traced_fastapi(*args, **kwargs):  # type: ignore[no-untyped-def]
        order.append("fastapi")
        return real_fastapi(*args, **kwargs)

    monkeypatch.setattr(type(manager), "recover_provider_runtime", traced)
    monkeypatch.setattr(app_module, "FastAPI", traced_fastapi)
    app_module.create_app()
    assert order[:2] == ["recover", "fastapi"]
    get_playbill_manager().clear()


# ------------------------------------------------------------------ K-9


def test_k9_recovery_still_races_a_live_invocation_on_the_same_store(
    short_root: Path,
) -> None:
    """CONFIRM (deployment constraint, unchanged): a second process running
    `recover_all` on the same state root kills an in-flight child."""

    store = ProviderProcessLeaseStore(short_root / "l", control_root=short_root / "c")
    marker = short_root / "raced"
    interpreter = write_child(short_root / "child.py", mode="escape", marker=marker)
    import threading

    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            _run_child(
                interpreter,
                entrypoint="demo:Provider",
                context=b'{"run_id":"RUN-race","input":{"value":"x"}}',
                budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=6, output_bytes=65_536),
                secret_fd=None,
                invocation_id="sha256:" + "9" * 64,
                process_leases=store,
            )
        except ProviderLocalRuntimeRefused as exc:
            outcome["code"] = exc.code

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    for _ in range(400):
        if tuple(store.root.glob("*.json")):
            break
        time.sleep(0.01)
    result = store.recover_all()
    worker.join(timeout=15)
    try:
        assert result.recovered == ("sha256:" + "9" * 64,), result
    finally:
        _reap(marker)
