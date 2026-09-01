"""Local Provider binding and invocation enforce the ruled B2 boundary."""

from __future__ import annotations

import hashlib
import signal
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.errors import PlaybillExecutionError
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
    ProviderLocalRuntimeRefused,
    ProviderSecretResolverRegistry,
    _run_child,
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
    invocation_id, record_path, control_path = sys.argv[2:5]
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
    document = {
        "invocation_id": invocation_id,
        "pid": os.getpid(),
        "process_group_id": os.getpgrp(),
    }
    with open(record_path, "wb") as handle:
        handle.write(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
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
                {"CRUXIBLE_PROVIDER_SECRET_billing_api_7": "credential-value"}
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
    assert tuple(leases.root.iterdir()) == ()


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
                {"CRUXIBLE_PROVIDER_SECRET_billing_api_7": "credential-value"}
            ),
        )
    )

    with pytest.raises(ProviderLocalRuntimeRefused) as caught:
        LocalProviderExecutionDriver().invoke(
            bound, context, secret_plan=_secret_plan(), secret_resolvers=registry
        )
    assert caught.value.code == "secret_leak"


def test_process_recovery_kills_only_an_echo_verified_invocation_group(tmp_path: Path) -> None:
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
            str(record_path),
            str(control_path),
            "demo.runtime:Provider",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    lease = leases.require(invocation_id)
    assert lease.pid == process.pid

    assert leases.recover_all() == (invocation_id,)
    process.wait(timeout=1)
    assert tuple(leases.root.iterdir()) == ()


def test_process_recovery_waits_through_a_transient_permission_probe(
    tmp_path: Path, monkeypatch
) -> None:
    leases = ProviderProcessLeaseStore(tmp_path / "process-leases")
    invocation_id = _digest("permission-probe")
    record_path, control_path = leases.paths(invocation_id)
    record_path.write_bytes(canonical_bytes({"invocation_id": invocation_id}))
    lease = ProviderProcessLeaseV1(
        invocation_id=invocation_id,
        pid=101,
        process_group_id=202,
        control_path=control_path,
        record_path=record_path,
    )
    monkeypatch.setattr(leases, "require", lambda value, timeout_seconds: lease)
    monkeypatch.setattr("os.waitpid", lambda pid, flags: (0, 0))
    probes = iter((PermissionError(), ProcessLookupError()))

    def killpg(process_group_id: int, sent_signal: int) -> None:
        assert process_group_id == lease.process_group_id
        if sent_signal == 0:
            raise next(probes)
        assert sent_signal == signal.SIGKILL

    monkeypatch.setattr("os.killpg", killpg)

    assert leases.recover_all() == (invocation_id,)
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
        def wait(*, timeout: int):
            assert timeout == 5
            return -signal.SIGKILL

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: _Process())
    monkeypatch.setattr(
        leases,
        "require",
        lambda value: (_ for _ in ()).throw(PlaybillExecutionError("lease invalid")),
    )
    monkeypatch.setattr("os.killpg", lambda pid, sent_signal: killed.append((pid, sent_signal)))

    with pytest.raises(PlaybillExecutionError, match="lease invalid"):
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
