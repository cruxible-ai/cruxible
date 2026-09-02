"""V-5: transient descendant observation cannot fail a successful run."""

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


def test_transient_process_table_failure_is_diagnostic_only(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.descendant_tracker_poll_interval_seconds = 10.0
    interpreter = _fake_interpreter(short_root / "provider.py")
    real_observe = runtime_module._DescendantTracker.observe
    calls: dict[int, int] = {}

    def fail_after_setup(tracker: runtime_module._DescendantTracker) -> None:
        count = calls.get(id(tracker), 0) + 1
        calls[id(tracker)] = count
        if count > 1:
            failure = ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid", "transient process-table failure"
            )
            tracker._failure = failure
            raise failure
        real_observe(tracker)

    monkeypatch.setattr(runtime_module._DescendantTracker, "observe", fail_after_setup)
    invocation_id = "sha256:" + "5" * 64

    outcome = _run_child(
        interpreter,
        entrypoint="demo:Provider",
        context=b'{"run_id":"RUN-v5","input":{"value":"ok"}}',
        budgets=ProviderRuntimeBudgetsV1(
            wall_clock_seconds=5,
            output_bytes=65_536,
        ),
        secret_fd=None,
        invocation_id=invocation_id,
        process_leases=store,
    )

    assert json.loads(outcome.stdout)["status"] == "ok"
    assert tuple(store.root.glob("*.json")) == ()
    assert store.diagnostics[-1][0] == "provider_process_lease_invalid"
    assert "transient process-table failure" in store.diagnostics[-1][1]
    assert operator._observation_diagnostic_count >= 1
    assert operator._last_observation_diagnostic is not None
    assert "transient process-table failure" in operator._last_observation_diagnostic[1]
    state, code, detail = operator.lane_status()
    assert (state, code) == ("available", None)
    assert detail is not None and "transient process-table failure" in detail
