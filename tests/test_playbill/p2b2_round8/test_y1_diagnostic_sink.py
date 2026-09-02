"""Landing-polish regression for the fifth diagnostic-sink call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def test_a_raising_terminate_sink_cannot_replace_success_or_strand_the_lease(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None

    def snapshot_failure(
        _tracker: runtime_module._DescendantTracker,
    ) -> tuple[object, ...]:
        raise ProviderLocalRuntimeRefused(
            "provider_process_lease_invalid",
            "planted terminal snapshot failure",
        )

    def raising_sink(_code: object, _message: object) -> None:
        raise RuntimeError("planted operator sink failure")

    monkeypatch.setattr(runtime_module._DescendantTracker, "snapshot", snapshot_failure)
    store._diagnostic_sink = raising_sink
    invocation_id = "sha256:" + "8" * 64

    outcome = _run_child(
        _fake_interpreter(short_root / "provider.py"),
        entrypoint="demo:Provider",
        context=b'{"run_id":"RUN-r8","input":{"value":"ok"}}',
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=2, output_bytes=65_536),
        secret_fd=None,
        invocation_id=invocation_id,
        process_leases=store,
    )

    assert json.loads(outcome.stdout)["status"] == "ok"
    record_path, control_path = store.paths(invocation_id)
    assert not record_path.exists()
    assert not control_path.exists()
