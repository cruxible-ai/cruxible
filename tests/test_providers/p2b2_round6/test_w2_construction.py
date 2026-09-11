"""Round-6 regression for construction-stage dependency reinitialization."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.runtime import provider_runtime as runtime_module
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

DEPLOYMENT_DIGEST = "sha256:" + "a" * 64
DEPLOYMENT = (
    '{"tag":"cruxible-provider-deployment-config-v1",'
    f'"deployment_digest":"{DEPLOYMENT_DIGEST}",'
    '"distribution_path":"d/dist.whl","lock_path":"d/lock",'
    '"environment_path":"d/env","environment_manifest_path":"d/seal.json",'
    '"environment_pin_key":"k","interpreter_path":"d/env/bin/python",'
    '"provider_runtime_version":"1.0.0"}'
)


def test_config_repair_reinitializes_every_declared_dependent_stage(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = short_root / "s"
    root.mkdir()
    (root / "daemon").mkdir()
    config_path = root / "daemon" / "provider-runtime.json"
    config_path.write_text("{ not json", encoding="utf-8")
    operator = ProviderRuntimeOperator(root)
    assert set(operator._construction_failures) == {"operational config"}
    assert operator.process_leases is not None
    assert operator.process_leases.acquisition_timeout_seconds == 5.0
    assert operator.deployments == {}

    real_initialize = ProviderRuntimeOperator._initialize_filesystem_components
    selected_stages: list[set[str]] = []

    def record_stages(
        self: ProviderRuntimeOperator,
        stages: set[runtime_module._ConstructionStage] | None = None,
    ) -> None:
        selected_stages.append(set() if stages is None else set(stages))
        real_initialize(self, stages)

    monkeypatch.setattr(
        ProviderRuntimeOperator,
        "_initialize_filesystem_components",
        record_stages,
    )
    config_path.write_text(
        '{"tag":"cruxible-provider-runtime-operational-config-v1",'
        '"lease_acquisition_timeout_seconds":1.25,'
        f'"deployments":[{DEPLOYMENT}]}}',
        encoding="utf-8",
    )

    operator._next_rearm_after = 0.0
    operator._begin_invocation()
    operator._end_invocation()

    assert selected_stages == [
        {"operational config", "process lease store", "secret store", "deployment"}
    ]
    assert runtime_module._CONSTRUCTION_STAGE_DEPENDENTS["operational config"] == (
        "process lease store",
        "secret store",
        "deployment",
    )
    assert operator.lane_status() == ("available", None, None)
    assert operator.config.lease_acquisition_timeout_seconds == 1.25
    assert operator.process_leases is not None
    assert operator.process_leases.acquisition_timeout_seconds == 1.25
    assert set(operator.deployments) == {DEPLOYMENT_DIGEST}
