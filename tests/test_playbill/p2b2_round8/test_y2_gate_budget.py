"""Landing-polish regression for observation-gate budget attribution."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import cruxible_core.playbill.provider_local_runtime as runtime_module
from cruxible_core.playbill import provider_outcomes
from cruxible_core.playbill.provider_local_runtime import _run_child
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.playbill.provider_runtime_contract import ProviderRuntimeBudgetsV1
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator
from tests.test_playbill.test_provider_local_driver import _fake_interpreter


def test_a_table_stall_past_the_wall_clock_is_a_typed_fence_refusal(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    store.acquisition_timeout_seconds = 5.0
    store.descendant_tracker_poll_interval_seconds = 0.01
    invocation_id = "sha256:" + "9" * 64
    recovers_at = time.monotonic() + 2.0
    real_snapshot = runtime_module.snapshot_provider_descendants

    def stalled_table(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if time.monotonic() < recovers_at:
            raise ProviderLocalRuntimeRefused(
                "provider_process_lease_invalid",
                "the operating-system process table is unavailable",
            )
        return real_snapshot(*args, **kwargs)  # type: ignore[operator]

    def unexpected_collection(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("provider input collection ran after the fence window")

    monkeypatch.setattr(runtime_module, "snapshot_provider_descendants", stalled_table)
    monkeypatch.setattr(runtime_module, "_collect_child_output", unexpected_collection)
    started = time.monotonic()

    with pytest.raises(ProviderLocalRuntimeRefused) as refusal:
        _run_child(
            _fake_interpreter(short_root / "provider.py"),
            entrypoint="demo:Provider",
            context=b'{"run_id":"RUN-r8","input":{"value":"ok"}}',
            budgets=ProviderRuntimeBudgetsV1(wall_clock_seconds=0.75, output_bytes=65_536),
            secret_fd=None,
            invocation_id=invocation_id,
            process_leases=store,
        )

    assert refusal.value.code == "provider_process_lease_invalid"
    assert refusal.value.code != "budget_wall_clock"
    assert time.monotonic() - started < 1.5
    assert {
        **provider_outcomes._MAPPING,
        **provider_outcomes._LOCAL_MAPPING,
    }[refusal.value.code] == ("internal", "executor")
    record_path, _control_path = store.paths(invocation_id)
    assert record_path.exists()
