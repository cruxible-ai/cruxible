"""Daemon construction keeps Provider execution inside the operational state root."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.provider_execution import ProviderSecretResolutionPlanV1
from cruxible_client.contracts.provider_interfaces import render_provider_interface
from cruxible_client.contracts.providers import (
    AcceptedProviderV1,
    ProviderV2,
    provider_digest,
    provider_expected_implementation_records,
    render_provider,
)
from cruxible_core.playbill.provider_local_runtime import LocalProviderDeploymentV1
from cruxible_core.playbill.provider_runtime_contract import (
    ProviderRuntimeBudgetsV1,
    ProviderRuntimeRunContextV1,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill._p2b1_support import accepted_interface, provider_v2
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_daemon_operator_rebinds_and_runs_a_real_local_subprocess(tmp_path: Path) -> None:
    state_root = tmp_path / "daemon-state"
    materialization = state_root / "materializations" / "demo"
    materialization.mkdir(parents=True)
    distribution = materialization / "provider.whl"
    distribution.write_bytes(b"provider-wheel")
    lock = materialization / "uv.lock"
    lock.write_bytes(b"exact-lock")
    interpreter = _fake_interpreter(materialization / "python")
    materialization_digest = _digest("operator-materialization")
    seal = materialization / "environment.json"
    seal.write_bytes(
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
    accepted_provider = AcceptedProviderV1(
        path="providers/demo-provider.json",
        provider=provider,
        artifact_digest=provider_digest(provider).tagged,
    )
    interface = accepted_interface()
    deployment_digest = _digest("operator-deployment")
    config_path = state_root / "daemon" / "provider-runtime.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(
        canonical_bytes(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "lease_acquisition_timeout_seconds": 5,
                "lease_recovery_timeout_seconds": 5,
                "deployments": [
                    {
                        "tag": "cruxible-provider-deployment-config-v1",
                        "deployment_digest": deployment_digest,
                        "distribution_path": str(distribution.relative_to(state_root)),
                        "lock_path": str(lock.relative_to(state_root)),
                        "environment_path": str(materialization.relative_to(state_root)),
                        "environment_manifest_path": str(seal.relative_to(state_root)),
                        "environment_pin_key": "linux-cp311+engine",
                        "interpreter_path": str(interpreter.relative_to(state_root)),
                        "provider_runtime_version": "1.0.0",
                    }
                ],
            }
        )
    )

    class _Instance:
        def tree_at(self, oid: str) -> dict[str, bytes]:
            assert oid == "a" * 40
            return {
                accepted_provider.path: render_provider(accepted_provider.provider),
                interface.path: render_provider_interface(interface.registration),
            }

    implementation = provider.implementations[0]
    deployment = LocalProviderDeploymentV1(
        deployment_digest=deployment_digest,
        distribution_path=distribution,
        lock_path=lock,
        environment_path=materialization,
        environment_manifest_path=seal,
        environment_pin_key="linux-cp311+engine",
        interpreter_path=interpreter,
        provider_runtime_version="1.0.0",
    )
    operator = ProviderRuntimeOperator(state_root)
    assert operator.deployments == {deployment_digest: deployment}
    assert operator.recover_all() == ()
    invoker = operator.invoker_for(_Instance(), accepted_oid="a" * 40)  # type: ignore[arg-type]
    admitted_binding = operator.driver.bind(
        accepted_provider,
        interface,
        implementation.implementation_digest,
        deployment,
    ).binding
    occurrence = SimpleNamespace(
        local_execution=admitted_binding,
        provider_artifact_digest=accepted_provider.artifact_digest,
        interface_artifact_digest=interface.artifact_digest,
        implementation_digest=implementation.implementation_digest,
        secret_plan=ProviderSecretResolutionPlanV1(),
    )
    context = ProviderRuntimeRunContextV1(
        protocol_version="1.0",
        run_id="RUN-daemon-operator",
        interface_id=interface.registration.interface_id,
        interface_digest=interface.registration.interface_digest,
        implementation_digest=implementation.implementation_digest,
        entrypoint=admitted_binding.entrypoint,
        input={"value": "served"},
        input_bucket="size=small",
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
    )

    outcome = invoker.invoke_provider(
        occurrence=occurrence,  # type: ignore[arg-type]
        context=context,
        invocation_id=_digest("daemon-invocation"),
    )
    assert outcome.envelope.output == {"echo": "served"}
    assert outcome.verified_binding == admitted_binding
    assert tuple(operator.process_leases.root.glob("*.json")) == ()
