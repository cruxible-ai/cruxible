"""Local Provider binding and invocation enforce the ruled B2 boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.provider_process_leases as process_lease_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.models import ProcedureBudgetV3, ProcedureHardCapsV3
from cruxible_client.contracts.provider_execution import (
    ProviderSecretBindingIdentityV1,
    ProviderSecretReferenceV1,
    ProviderSecretResolutionPlanV1,
    VerifiedProviderBindingV1,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderV2,
    provider_digest,
    provider_expected_implementation_records,
)
from cruxible_core.playbill.provider_local_runtime import (
    BoundLocalProviderV1,
    EnvironmentProviderSecretResolver,
    FileProviderSecretStore,
    LocalProviderDeploymentV1,
    LocalProviderExecutionDriver,
    ProviderLocalRuntimeInvoker,
    ProviderLocalRuntimeRefused,
    ProviderSecretResolverRegistry,
    _assert_no_secret,
    _run_child,
    provider_environment_secret_key,
    translate_provider_budget,
)
from cruxible_core.playbill.provider_process_leases import (
    ProviderProcessLeaseStore,
    ProviderProcessLeaseV1,
)
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
)
from tests.test_playbill._p2b1_support import accepted_interface, provider_v2


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _budget(*, capture_bytes: int = 2_000_000) -> ProcedureBudgetV3:
    return ProcedureBudgetV3(
        wall_clock=CanonicalDurationV1(microseconds=5_900_000),
        max_provider_calls=2,
        max_capture_bytes=capture_bytes,
        max_items=20,
    )


def _caps() -> ProcedureHardCapsV3:
    return ProcedureHardCapsV3(
        max_wall_clock=CanonicalDurationV1(microseconds=4_100_000),
        max_provider_calls=5,
        max_capture_bytes=3_000_000,
        max_items=30,
        max_repeat_attempts=2,
    )


def _secret_plan(
    *, epoch: str = "7", resolver: str = "environment"
) -> ProviderSecretResolutionPlanV1:
    reference = ProviderSecretReferenceV1(
        realm="billing",
        name="api",
        epoch=epoch,
        purpose="provider test",
        resolver_kind=resolver,
    )
    digest = provider_secret_binding_identity_digest(
        ProviderSecretBindingIdentityV1(realm=reference.realm, name=reference.name)
    )
    return ProviderSecretResolutionPlanV1(
        references=(reference,), binding_identity_digests=(digest,)
    )


def test_capture_free_budget_comes_from_governed_policy_and_rounds_down() -> None:
    first = translate_provider_budget(
        budget=_budget(),
        hard_caps=_caps(),
        runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=1_048_576),
        remaining_wall_clock_microseconds=3_900_000,
        result_bytes_cap=900_000,
        produces_capture=False,
    )
    second = translate_provider_budget(
        budget=_budget(),
        hard_caps=_caps(),
        runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=700_000),
        remaining_wall_clock_microseconds=3_900_000,
        result_bytes_cap=900_000,
        produces_capture=False,
    )

    assert first.runtime_wall_clock_seconds == 3
    assert first.procedure_output_bytes_cap is None
    assert first.runtime_output_bytes_cap == 1_048_576
    assert second.runtime_output_bytes_cap == 700_000


def test_provider_budget_refuses_before_spawn_when_whole_second_or_call_is_absent() -> None:
    with pytest.raises(ProviderLocalRuntimeRefused) as wall:
        translate_provider_budget(
            budget=_budget(),
            hard_caps=_caps(),
            runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=1_048_576),
            remaining_wall_clock_microseconds=999_999,
            result_bytes_cap=100,
            produces_capture=False,
        )
    assert wall.value.code == "budget_wall_clock"

    with pytest.raises(ProviderLocalRuntimeRefused) as calls:
        translate_provider_budget(
            budget=_budget().model_copy(update={"max_provider_calls": 0}),
            hard_caps=_caps(),
            runtime_policy=ProcedureRuntimePolicyV1(provider_output_bytes_cap=1_048_576),
            remaining_wall_clock_microseconds=2_000_000,
            result_bytes_cap=100,
            produces_capture=False,
        )
    assert calls.value.code == "budget_max_provider_calls_exceeded"


def test_secret_identity_is_epoch_and_resolver_independent_and_file_store_is_private(
    tmp_path: Path,
) -> None:
    first = _secret_plan(epoch="1", resolver="file")
    rotated = _secret_plan(epoch="2", resolver="environment")
    assert first.binding_identity_digests == rotated.binding_identity_digests

    store = FileProviderSecretStore(tmp_path / "custody")
    reference = first.references[0]
    store.put(reference, "not-in-any-receipt")
    assert store.resolve(reference) == "not-in-any-receipt"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / "billing/api/1").stat().st_mode) == 0o600


def _fake_interpreter(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/python3
import json, os, socket, sys, threading
if len(sys.argv) > 1 and sys.argv[1].endswith("provider_child_fence.py"):
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
                answer = invocation_id.encode("utf-8") if received == invocation_id else b""
                connection.sendall(answer)
    threading.Thread(target=echo, daemon=True).start()
document = json.loads(sys.stdin.buffer.read())
json.dump({
    "protocol_version": "1.0",
    "run_id": document["run_id"],
    "status": "ok",
    "output": {"echo": document["input"]["value"]},
    "refusal": None,
    "error": None,
    "trace": {"endpoints_contacted": ["https://example.test"], "events": [], "metrics": {}}
}, sys.stdout, sort_keys=True, separators=(",", ":"))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_local_bind_reproduces_distribution_lock_materialization_and_runtime_membership(
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "provider.whl"
    distribution.write_bytes(b"provider-wheel")
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"exact-lock")
    materialization_digest = _digest("materialization")
    environment_manifest = tmp_path / "environment.json"
    environment_manifest.write_bytes(
        canonical_bytes(
            {
                "installed_distributions": {
                    "cruxible-provider-runtime": "1.0.0",
                    "demo-provider": "1.0.0",
                },
                "lock_sha256": _sha256_file(lock),
                "materialization_digest": materialization_digest,
            }
        )
    )
    interpreter = _fake_interpreter(tmp_path / "fake-python")
    base = provider_v2()
    assert base.runtime_artifact.local_env is not None
    payload = base.runtime_artifact.model_copy(
        update={
            "distribution": base.runtime_artifact.distribution.model_copy(
                update={"sha256": _sha256_file(distribution)}
            ),
            "local_env": base.runtime_artifact.local_env.model_copy(
                update={
                    "lock_sha256": _sha256_file(lock),
                    "materialization_digests": {"linux-cp311+engine": materialization_digest},
                }
            ),
        }
    )
    provider = ProviderV2.model_validate(
        base.model_copy(
            update={
                "runtime_artifact": payload,
                "implementations": provider_expected_implementation_records(payload),
            }
        ).model_dump(mode="python")
    )
    accepted = AcceptedProviderV1(
        path="providers/demo-provider.json",
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    implementation = provider.implementations[0]
    deployment = LocalProviderDeploymentV1(
        deployment_digest=_digest("deployment"),
        distribution_path=distribution,
        lock_path=lock,
        environment_path=tmp_path,
        environment_manifest_path=environment_manifest,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="1.0.0",
    )

    bound = LocalProviderExecutionDriver().bind(
        accepted, accepted_interface(), implementation.implementation_digest, deployment
    )
    assert bound.binding.materialization_digest == materialization_digest
    assert bound.binding.environment_manifest_digest == _sha256_file(environment_manifest)

    verified_environment = tmp_path / "verified-environment"
    verified_environment.mkdir()
    with pytest.raises(ProviderLocalRuntimeRefused) as escaped_environment:
        LocalProviderExecutionDriver().bind(
            accepted,
            accepted_interface(),
            implementation.implementation_digest,
            replace(deployment, environment_path=verified_environment),
        )
    assert escaped_environment.value.code == "environment_divergence"

    environment_manifest.write_bytes(
        canonical_bytes(
            {
                "installed_distributions": {"demo-provider": "1.0.0"},
                "lock_sha256": _sha256_file(lock),
                "materialization_digest": materialization_digest,
            }
        )
    )
    with pytest.raises(ProviderLocalRuntimeRefused) as missing_runtime:
        LocalProviderExecutionDriver().bind(
            accepted, accepted_interface(), implementation.implementation_digest, deployment
        )
    assert missing_runtime.value.code == "provider_runtime_not_in_materialization"


def test_local_driver_runs_in_isolated_directory_with_fd_secret_and_attribution_egress(
    tmp_path: Path,
) -> None:
    interpreter = _fake_interpreter(tmp_path / "fake-python")
    binding = BoundLocalProviderV1(
        binding=VerifiedProviderBindingV1(
            provider_artifact_digest=_digest("provider"),
            interface_artifact_digest=_digest("interface-artifact"),
            interface_id="demo.interface",
            interface_digest=_digest("interface"),
            implementation_digest=_digest("implementation"),
            deployment_digest=_digest("deployment"),
            materialization_digest=_digest("materialization"),
            environment_manifest_digest=_digest("environment"),
            entrypoint="demo.runtime:Provider",
            declared_endpoints=("https://example.test",),
        ),
        interpreter_path=interpreter,
    )
    plan = _secret_plan()
    registry = ProviderSecretResolverRegistry(
        (
            EnvironmentProviderSecretResolver(
                {provider_environment_secret_key(plan.references[0]): "credential-value"}
            ),
        )
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-local",
        interface_id="demo.interface",
        interface_digest=_digest("interface"),
        implementation_digest=_digest("implementation"),
        entrypoint="demo.runtime:Provider",
        coordinates={"accepted_generation": 4},
        input={"value": "hello"},
        input_bucket="size=small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=16_384),
    )

    leases = ProviderProcessLeaseStore(tmp_path / "process-leases")
    outcome = LocalProviderExecutionDriver().invoke(
        binding,
        context,
        secret_plan=plan,
        secret_resolvers=registry,
        invocation_id=_digest("invocation"),
        process_leases=leases,
    )

    assert outcome.envelope.output == {"echo": "hello"}
    assert outcome.egress.observer_grade == "attribution"
    assert outcome.egress.observed_endpoints == ("https://example.test",)
    assert "credential-value" not in repr(outcome)
    assert tuple(leases.root.glob("*.json")) == ()


def test_local_driver_detects_raw_secret_leak_before_parsing(tmp_path: Path) -> None:
    interpreter = _fake_interpreter(tmp_path / "fake-python")
    # Put the secret in the ordinary payload so the pre-spawn byte check is decisive.
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-leak",
        interface_id="demo.interface",
        interface_digest=_digest("interface"),
        implementation_digest=_digest("implementation"),
        entrypoint="demo.runtime:Provider",
        input={"value": "credential-value"},
        input_bucket="size=small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=16_384),
    )
    bound = BoundLocalProviderV1(
        binding=VerifiedProviderBindingV1(
            provider_artifact_digest=_digest("provider"),
            interface_artifact_digest=_digest("interface-artifact"),
            interface_id="demo.interface",
            interface_digest=_digest("interface"),
            implementation_digest=_digest("implementation"),
            deployment_digest=_digest("deployment"),
            materialization_digest=_digest("materialization"),
            environment_manifest_digest=_digest("environment"),
            entrypoint="demo.runtime:Provider",
        ),
        interpreter_path=interpreter,
    )
    registry = ProviderSecretResolverRegistry(
        (
            EnvironmentProviderSecretResolver(
                {
                    provider_environment_secret_key(_secret_plan().references[0]): (
                        "credential-value"
                    )
                }
            ),
        )
    )

    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        LocalProviderExecutionDriver().invoke(
            bound,
            context,
            secret_plan=_secret_plan(),
            secret_resolvers=registry,
            invocation_id=_digest("leak-invocation"),
            process_leases=ProviderProcessLeaseStore(tmp_path / "leak-leases"),
        )
    assert caught.value.code == "secret_leak"


def test_process_recovery_kills_an_echo_verified_invocation_group(tmp_path: Path) -> None:
    interpreter = _fake_interpreter(tmp_path / "fake-python")
    leases = ProviderProcessLeaseStore(tmp_path / "process-leases")
    invocation_id = _digest("orphaned-invocation")
    record_path, control_path = leases.paths(invocation_id)
    wrapper = tmp_path / "provider_child_fence.py"
    wrapper.write_text("# marker", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(interpreter),
            str(wrapper),
            invocation_id,
            str(control_path),
            "demo.runtime:Provider",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    leases.publish(invocation_id, pid=process.pid, process_group_id=process.pid)
    lease = leases.require(invocation_id)
    assert lease.pid == process.pid

    result = leases.recover_all()
    assert result.recovered == (invocation_id,)
    assert result.removed == ()
    assert result.could_not_clean == ()
    process.wait(timeout=1)
    assert tuple(leases.root.glob("*.json")) == ()


def test_process_recovery_waits_through_a_transient_permission_probe(
    tmp_path: Path, monkeypatch
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "process-leases")
    invocation_id = _digest("permission-probe")
    record_path, control_path = leases.paths(invocation_id)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation_id,
                "pid": 101,
                "process_group_id": 202,
                "boot_id": "boot",
                "process_start_time": "start",
            }
        )
    )
    lease = ProviderProcessLeaseV1(
        invocation_id=invocation_id,
        pid=101,
        process_group_id=202,
        boot_id="boot",
        process_start_time="start",
        control_path=control_path,
        record_path=record_path,
    )
    monkeypatch.setattr(leases, "require_echo", lambda value: None)
    monkeypatch.setattr("os.waitpid", lambda pid, flags: (0, 0))
    probes = iter((PermissionError(), ProcessLookupError()))

    def killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == lease.process_group_id
        if sent_signal == 0:
            raise next(probes)
        assert sent_signal == signal.SIGKILL

    monkeypatch.setattr("os.killpg", killpg)

    result = leases.recover_all()
    assert result.recovered == (invocation_id,)
    assert not record_path.exists()


def test_spawned_child_is_killed_when_its_owned_lease_cannot_be_verified(
    tmp_path: Path, monkeypatch
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "process-leases")
    invocation_id = _digest("unverified-child")
    killed: list[tuple[int, int]] = []

    class _Process:
        pid = 707

        @staticmethod
        def wait(*, timeout: float):
            assert timeout > 0
            return -signal.SIGKILL

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(process_lease_module, "_current_boot_id", lambda: "boot")
    monkeypatch.setattr(process_lease_module, "_process_start_time", lambda _pid: "start")
    monkeypatch.setattr(
        leases,
        "require",
        lambda value: (_ for _ in ()).throw(
            ProviderLocalRuntimeRefused("provider_process_lease_invalid", "lease invalid")
        ),
    )

    def killpg(pid: int, sent_signal: int) -> None:
        if sent_signal == 0:
            raise ProcessLookupError
        killed.append((pid, sent_signal))

    monkeypatch.setattr("os.killpg", killpg)

    with pytest.raises(ProviderLocalRuntimeRefused, match="lease invalid"):
        _run_child(
            tmp_path / "python",
            entrypoint="demo.runtime:Provider",
            context=b"{}",
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=1024),
            secret_fd=None,
            invocation_id=invocation_id,
            process_leases=leases,
        )
    assert killed == [(_Process.pid, signal.SIGKILL)]


def test_wall_clock_escape_is_typed_killed_and_unfenced_only_after_death(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "alive.txt"
    interpreter = tmp_path / "escaping-python"
    interpreter.write_text(
        f"""#!/usr/bin/env python3
import os, socket, sys, threading, time
invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
server.listen(2)
def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            data = connection.recv(4096).decode()
            connection.sendall(invocation_id.encode() if data == invocation_id else b"")
threading.Thread(target=echo, daemon=True).start()
sys.stdout.write('{{"protocol_version":"1.0","run_id":"RUN-x","status":"ok",'
                 '"output":{{"a":1}},"trace":{{"endpoints_contacted":[],"events":[],"metrics":{{}}}}}}')
sys.stdout.flush()
os.close(1)
os.close(2)
with open({str(marker)!r}, "a") as handle:
    for _ in range(400):
        time.sleep(0.05)
        handle.write("alive\\n")
        handle.flush()
""",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    leases = ProviderProcessLeaseStore(tmp_path / "escape-leases")
    invocation_id = _digest("escape")

    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        _run_child(
            interpreter,
            entrypoint="demo.runtime:Provider",
            context=b'{"run_id":"RUN-x"}',
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=0.25, output_bytes=65_536),
            secret_fd=None,
            invocation_id=invocation_id,
            process_leases=leases,
        )
    assert caught.value.code == "budget_wall_clock"
    assert tuple(leases.root.glob("*.json")) == ()
    first = marker.read_text().count("alive") if marker.exists() else 0
    time.sleep(0.2)
    second = marker.read_text().count("alive") if marker.exists() else 0
    assert second == first


def test_recovery_removes_dead_records_without_starving_later_records(tmp_path: Path) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "recovery-leases")
    invocation_ids = tuple(_digest(f"dead-{index}") for index in range(4))
    for invocation_id in invocation_ids:
        record_path, control_path = leases.paths(invocation_id)
        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation_id,
                    "pid": 999_999,
                    "process_group_id": 999_999,
                }
            )
        )
        assert not control_path.exists()

    result = leases.recover_all()
    assert {item.invocation_id for item in result.removed} == set(invocation_ids)
    assert {item.reason for item in result.removed} == {"dead_orphan"}
    assert result.recovered == ()
    assert tuple(leases.root.glob("*.json")) == ()


def test_recovery_never_signals_a_reused_pid_without_exact_os_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "reused-pid-leases")
    invocation_id = _digest("reused-pid")
    record_path, _control_path = leases.paths(invocation_id)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation_id,
                "pid": 707,
                "process_group_id": 707,
                "boot_id": "prior-boot",
                "process_start_time": "prior-start",
            }
        )
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(process_lease_module, "_current_boot_id", lambda: "current-boot")
    monkeypatch.setattr(os, "killpg", lambda pgid, sent: signals.append((pgid, sent)))

    result = leases.recover_all()

    assert signals == []
    assert result.recovered == ()
    assert tuple(item.invocation_id for item in result.removed) == (invocation_id,)
    assert tuple(item.reason for item in result.removed) == ("dead_orphan",)
    assert not record_path.exists()


def test_recovery_isolates_a_survivor_and_continues_to_later_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "isolated-recovery-leases")
    invocation_ids = (_digest("blocked"), _digest("later"))
    for ordinal, invocation_id in enumerate(invocation_ids, start=1):
        record_path, _control_path = leases.paths(invocation_id)
        record_path.write_bytes(
            canonical_bytes(
                {
                    "invocation_id": invocation_id,
                    "pid": 800 + ordinal,
                    "process_group_id": 800 + ordinal,
                    "boot_id": "boot",
                    "process_start_time": f"start-{ordinal}",
                }
            )
        )
    monkeypatch.setattr(leases, "require_echo", lambda _lease: None)

    def recover(lease: ProviderProcessLeaseV1) -> None:
        if lease.invocation_id == invocation_ids[0]:
            raise ProviderLocalRuntimeRefused(
                "provider_process_group_survived_recovery",
                "blocked by probe",
            )

    monkeypatch.setattr(leases, "_kill_and_verify", recover)

    result = leases.recover_all()

    assert result.recovered == (invocation_ids[1],)
    assert tuple(item.invocation_id for item in result.could_not_clean) == (
        invocation_ids[0],
    )
    first_record, _ = leases.paths(invocation_ids[0])
    second_record, _ = leases.paths(invocation_ids[1])
    assert first_record.exists()
    assert not second_record.exists()


def test_malformed_recovery_record_is_removed_without_inventing_an_invocation_id(
    tmp_path: Path,
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "malformed-recovery-leases")
    record_path = leases.root / "not-an-invocation.json"
    record_path.write_text("{}", encoding="utf-8")

    result = leases.recover_all()

    assert result.recovered == ()
    assert len(result.removed) == 1
    assert result.removed[0].invocation_id is None
    assert result.removed[0].reason == "malformed"
    assert not record_path.exists()


def test_control_namespace_is_private_and_stale_socket_is_retryable(tmp_path: Path) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "private-leases")
    assert leases.control_root.resolve().is_relative_to(leases.root.resolve())
    invocation_id = _digest("stale-socket")
    _record_path, control_path = leases.paths(invocation_id)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(control_path))
    stale.close()
    assert control_path.exists()

    outcome = _run_child(
        _fake_interpreter(tmp_path / "retry-python"),
        entrypoint="demo.runtime:Provider",
        context=b'{"run_id":"RUN-retry","input":{"value":"ok"}}',
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
        secret_fd=None,
        invocation_id=invocation_id,
        process_leases=leases,
    )
    assert json.loads(outcome.stdout)["output"] == {"echo": "ok"}


@pytest.mark.parametrize("planted_kind", ["file", "symlink"])
def test_process_lease_root_refuses_non_directory_or_symlink(
    tmp_path: Path, planted_kind: str
) -> None:
    root = tmp_path / "planted"
    if planted_kind == "file":
        root.write_text("not a directory", encoding="utf-8")
    else:
        target = tmp_path / "target"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        ProviderProcessLeaseStore(root)
    assert caught.value.code == "provider_process_lease_invalid"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("billing", "api_key", "v1"), ("billing", "api", "key_v1")),
        (("prod_billing", "api", "v1"), ("prod", "billing_api", "v1")),
    ],
)
def test_environment_secret_keys_are_injective_across_separator_collisions(
    first: tuple[str, str, str], second: tuple[str, str, str]
) -> None:
    references = tuple(
        ProviderSecretReferenceV1(
            realm=realm,
            name=name,
            epoch=epoch,
            resolver_kind="environment",
        )
        for realm, name, epoch in (first, second)
    )
    keys = tuple(provider_environment_secret_key(reference) for reference in references)
    assert keys[0] != keys[1]
    resolver = EnvironmentProviderSecretResolver({keys[0]: "first", keys[1]: "second"})
    assert resolver.resolve(references[0]) == "first"
    assert resolver.resolve(references[1]) == "second"


@pytest.mark.parametrize(
    "code",
    [
        "unaccepted_provider",
        "acceptance_divergence",
        "ambiguous_implementation",
        "no_compatible_artifact",
        "artifact_hash_mismatch",
        "unsupported_backend",
        "lock_bytes_mismatch",
        "cache_integrity",
        "environment_divergence",
        "provider_runtime_not_in_materialization",
        "undeclared_interface",
    ],
)
def test_invoker_rebinds_before_spawn_and_surfaces_every_bind_refusal(
    tmp_path: Path, code: str
) -> None:
    binding = VerifiedProviderBindingV1(
        provider_artifact_digest=_digest("provider"),
        interface_artifact_digest=_digest("interface-artifact"),
        interface_id="demo.interface",
        interface_digest=_digest("interface"),
        implementation_digest=_digest("implementation"),
        deployment_digest=_digest("deployment"),
        materialization_digest=_digest("materialization"),
        environment_manifest_digest=_digest("environment"),
        entrypoint="demo.runtime:Provider",
    )
    deployment = LocalProviderDeploymentV1(
        deployment_digest=binding.deployment_digest,
        distribution_path=tmp_path / "distribution",
        lock_path=tmp_path / "lock",
        environment_path=tmp_path / "environment",
        environment_manifest_path=tmp_path / "environment" / "seal.json",
        environment_pin_key="pin",
        interpreter_path=tmp_path / "environment" / "python",
        provider_runtime_version="1.0.0",
    )

    class _RefusingDriver(LocalProviderExecutionDriver):
        def bind(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise ProviderLocalRuntimeRefused(code, "probe")

        def invoke(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("spawn reached after bind refusal")

    invoker = ProviderLocalRuntimeInvoker(
        deployments_by_digest={binding.deployment_digest: deployment},
        accepted_providers_by_digest={binding.provider_artifact_digest: object()},  # type: ignore[dict-item]
        accepted_interfaces_by_digest={binding.interface_artifact_digest: object()},  # type: ignore[dict-item]
        secret_resolvers=ProviderSecretResolverRegistry(()),
        process_leases=ProviderProcessLeaseStore(tmp_path / "bind-leases"),
        driver=_RefusingDriver(),
    )
    occurrence = SimpleNamespace(
        local_execution=binding,
        provider_artifact_digest=binding.provider_artifact_digest,
        interface_artifact_digest=binding.interface_artifact_digest,
        implementation_digest=binding.implementation_digest,
        secret_plan=ProviderSecretResolutionPlanV1(),
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-bind",
        interface_id=binding.interface_id,
        interface_digest=binding.interface_digest,
        implementation_digest=binding.implementation_digest,
        entrypoint=binding.entrypoint,
        input={},
        input_bucket="bucket",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=1024),
    )

    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        invoker.invoke_provider(
            occurrence=occurrence,  # type: ignore[arg-type]
            context=context,
            invocation_id=_digest("bind-invocation"),
            bound=BoundLocalProviderV1(binding=binding, interpreter_path=tmp_path / "unused"),
        )
    assert caught.value.code == code


def test_output_cap_refuses_before_buffering_more_than_one_extra_byte(tmp_path: Path) -> None:
    interpreter = tmp_path / "flooding-python"
    interpreter.write_text(
        """#!/usr/bin/env python3
import os, socket, sys, threading, time
invocation_id, control_path = sys.argv[2:4]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(control_path)
server.listen(2)
def echo():
    while True:
        try:
            connection, _ = server.accept()
        except OSError:
            return
        with connection:
            data = connection.recv(4096).decode()
            connection.sendall(invocation_id.encode() if data == invocation_id else b"")
threading.Thread(target=echo, daemon=True).start()
sys.stdout.buffer.write(b"x" * 65536)
sys.stdout.buffer.flush()
time.sleep(5)
""",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    leases = ProviderProcessLeaseStore(tmp_path / "cap-leases")
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        _run_child(
            interpreter,
            entrypoint="demo.runtime:Provider",
            context=b"{}",
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=5, output_bytes=4096),
            secret_fd=None,
            invocation_id=_digest("cap"),
            process_leases=leases,
        )
    assert caught.value.code == "budget_output_size"
    assert tuple(leases.root.glob("*.json")) == ()


@pytest.mark.parametrize("transform", [lambda value: value[::-1], base64.b64encode])
def test_secret_scan_refuses_cheap_encoded_variants(transform) -> None:  # type: ignore[no-untyped-def]
    secret = "not-visible-material"
    with pytest.raises(ProviderLocalRuntimeRefused, match="secret_leak"):
        _assert_no_secret(
            transform(secret.encode("utf-8")),
            {"private/key": secret},
            where="provider stderr",
        )


def test_unknown_dynamic_endpoint_form_is_typed_before_spawn(tmp_path: Path) -> None:
    interpreter = _fake_interpreter(tmp_path / "never-spawned")
    binding = BoundLocalProviderV1(
        binding=VerifiedProviderBindingV1(
            provider_artifact_digest=_digest("provider"),
            interface_artifact_digest=_digest("interface-artifact"),
            interface_id="demo.interface",
            interface_digest=_digest("interface"),
            implementation_digest=_digest("implementation"),
            deployment_digest=_digest("deployment"),
            materialization_digest=_digest("materialization"),
            environment_manifest_digest=_digest("environment"),
            entrypoint="demo.runtime:Provider",
            declared_endpoints=("dynamic:future-form",),
        ),
        interpreter_path=interpreter,
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-dynamic",
        interface_id="demo.interface",
        interface_digest=_digest("interface"),
        implementation_digest=_digest("implementation"),
        entrypoint="demo.runtime:Provider",
        input={},
        input_bucket="size=small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=1, output_bytes=1024),
    )
    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        LocalProviderExecutionDriver().invoke(
            binding,
            context,
            secret_plan=ProviderSecretResolutionPlanV1(),
            secret_resolvers=ProviderSecretResolverRegistry(()),
            invocation_id=_digest("dynamic"),
            process_leases=ProviderProcessLeaseStore(tmp_path / "dynamic-leases"),
        )
    assert caught.value.code == "undeclared_egress"
