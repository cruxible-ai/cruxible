"""V-2 through V-4: construction state, exact re-init, and lazy backoff."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import cruxible_core.runtime.provider_runtime as provider_runtime_module
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessLeaseStore,
    ProviderProcessRecoveryFailureV1,
    ProviderProcessRecoveryResultV1,
)
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator


def _secret_store_failure(short_root: Path) -> tuple[ProviderRuntimeOperator, Path]:
    occupied = short_root / "daemon" / "provider-secrets"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("occupied", encoding="utf-8")
    operator = ProviderRuntimeOperator(short_root)
    assert operator.secret_store is None
    assert "secret store" in (operator.unavailable_reason or "")
    return operator, occupied


def test_clean_recovery_never_clears_an_unrepaired_construction_failure(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator, _occupied = _secret_store_failure(short_root)
    assert operator.process_leases is not None
    monkeypatch.setattr(
        operator.process_leases,
        "recover_all",
        lambda **_kwargs: ProviderProcessRecoveryResultV1(
            recovered=(), removed=(), could_not_clean=()
        ),
    )
    operator.mark_unavailable(
        "provider_process_group_survived_recovery", "transient", retryable=True
    )

    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()

    assert operator.lane_status()[0] == "unavailable"
    assert operator.secret_store is None
    assert "secret store" in (operator.unavailable_reason or "")
    assert operator._recovery_failures == {}
    assert "secret store" in operator._construction_failures


def test_filesystem_repair_reinitializes_exactly_the_failed_stage(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator, occupied = _secret_store_failure(short_root)
    occupied.unlink()
    config_before = operator.config

    def config_must_not_rerun() -> object:
        raise AssertionError("a successful construction stage was rerun")

    monkeypatch.setattr(operator, "_load_config", config_must_not_rerun)
    operator._begin_invocation()
    operator._end_invocation()

    assert operator.secret_store is not None
    assert operator.config is config_before
    assert "secret store" not in operator._construction_failures
    assert operator.lane_status() == ("available", None, None)


def test_lazy_rearm_refuses_immediately_inside_the_configured_backoff(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = short_root / "daemon" / "provider-runtime.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "tag": "cruxible-provider-runtime-operational-config-v1",
                "rearm_backoff_seconds": 5.0,
            }
        ),
        encoding="utf-8",
    )
    operator = ProviderRuntimeOperator(short_root)
    assert operator.process_leases is not None
    calls: list[float] = []
    now = [100.0]
    monkeypatch.setattr(provider_runtime_module.time, "monotonic", lambda: now[0])
    failure = ProviderProcessRecoveryFailureV1(
        record_name="stuck.json",
        invocation_id=None,
        code="provider_process_group_survived_recovery",
        message="still stuck",
    )

    def fail(**_kwargs: object) -> ProviderProcessRecoveryResultV1:
        calls.append(now[0])
        return ProviderProcessRecoveryResultV1(recovered=(), removed=(), could_not_clean=(failure,))

    monkeypatch.setattr(operator.process_leases, "recover_all", fail)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)

    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    retained_reason = operator.unavailable_reason
    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert calls == [100.0]
    assert operator.unavailable_reason == retained_reason

    now[0] = 105.0
    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert calls == [100.0, 105.0]


def test_process_lease_control_root_is_a_required_keyword() -> None:
    parameter = inspect.signature(ProviderProcessLeaseStore).parameters["control_root"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
