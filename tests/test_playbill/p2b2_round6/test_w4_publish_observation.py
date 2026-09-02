"""Round-6 regression for the guarded pre-publish process observation."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def test_pre_publish_process_table_failure_is_diagnostic_only(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.descendant_tracker_poll_interval_seconds = 30.0
    interpreter = _fake_interpreter(short_root / "provider.py")
    real_observe = runtime_module._DescendantTracker.observe
    calls = 0

    def fail_first(tracker: runtime_module._DescendantTracker) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            failure = ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "planted pre-publish process-table failure",
            )
            tracker._failure = failure
            raise failure
        real_observe(tracker)

    monkeypatch.setattr(runtime_module._DescendantTracker, "observe", fail_first)
    invocation_id = "sha256:" + "a" * 64

    outcome = _run_child(
        interpreter,
        entrypoint="demo:Provider",
        context=b'{"run_id":"RUN-r6","input":{"value":"ok"}}',
        budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=5, output_bytes=65_536),
        secret_fd=None,
        invocation_id=invocation_id,
        process_leases=store,
    )

    assert json.loads(outcome.stdout)["status"] == "ok"
    record_path, control_path = store.paths(invocation_id)
    assert not record_path.exists()
    assert not control_path.exists()
    assert store.diagnostics
    assert "pre-publish process-table failure" in store.diagnostics[0][1]
    assert operator.lane_status() == ("available", None, None)


def test_all_forced_observations_share_the_diagnostic_only_helper() -> None:
    run_child = inspect.getsource(runtime_module._run_child)
    collect_output = inspect.getsource(runtime_module._collect_child_output)
    helper = inspect.getsource(runtime_module._observe_descendants_best_effort)

    pre_publish = run_child.split("process_leases.publish", maxsplit=1)[0]
    assert "_observe_descendants_best_effort(" in pre_publish
    assert "_observe_descendants_best_effort(" in collect_output
    assert "except ProviderLocalRuntimeRefused" in helper
    assert "diagnostic_sink(failure)" in helper
