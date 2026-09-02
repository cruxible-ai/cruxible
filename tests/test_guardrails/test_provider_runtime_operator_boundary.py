"""The Provider operator boundary contains every governed-instance load failure."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillFormatError, PlaybillGitError
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

DEAD_PID = 99_999_991
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def short_root(request: pytest.FixtureRequest) -> Path:
    root = Path(tempfile.mkdtemp(prefix=".b2-r6-boundary-", dir=REPOSITORY_ROOT))

    def remove() -> None:
        shutil.rmtree(root, ignore_errors=True)
        if os.path.lexists(root):
            time.sleep(0.05)
            shutil.rmtree(root, ignore_errors=True)

    request.addfinalizer(remove)
    return root


def _plant_record(operator: ProviderRuntimeOperator, invocation_id: str) -> Path:
    store = operator.process_leases
    assert store is not None
    record_path, _control_path = store.paths(invocation_id)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation_id,
                "pid": DEAD_PID,
                "process_group_id": DEAD_PID,
                "session_id": None,
                "boot_id": None,
                "process_start_time": None,
            }
        )
    )
    return record_path


def _operator_with_failing_instance_load(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> tuple[PlaybillInstanceManager, ProviderRuntimeOperator, Path]:
    root.mkdir()
    operator = ProviderRuntimeOperator(root)
    invocation_id = "sha256:" + "6" * 64
    record_path = _plant_record(operator, invocation_id)
    manager = PlaybillInstanceManager()
    records = (SimpleNamespace(backend="governed_daemon", instance_id="inst_corrupt"),)
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )

    def fail_get(_instance_id: str) -> None:
        raise failure

    monkeypatch.setattr(manager, "get", fail_get)
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))
    return manager, operator, record_path


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(PlaybillFormatError("malformed trust root"), id="format"),
        pytest.param(PlaybillGitError("damaged ledger"), id="git"),
        pytest.param(OSError("unreadable instance"), id="os"),
        pytest.param(Exception("unexpected instance failure"), id="bare"),
    ],
)
def test_operator_public_entries_contain_every_instance_load_exception(
    short_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    manager, operator, record_path = _operator_with_failing_instance_load(
        short_root / "startup", monkeypatch, failure
    )
    result = manager.recover_provider_runtime()
    assert result.completion_invocation_ids == ("sha256:" + "6" * 64,)
    assert record_path.exists()
    state, code, detail = operator.lane_status()
    assert state == "unavailable"
    assert code == "provider_runtime_recovery_failed"
    assert detail is not None and "inst_corrupt" in detail
    assert operator._next_rearm_after > time.monotonic()

    manager, operator, record_path = _operator_with_failing_instance_load(
        short_root / "recover", monkeypatch, failure
    )
    result = operator.recover_all_with_bound_fold()
    assert result.completion_invocation_ids == ("sha256:" + "6" * 64,)
    assert record_path.exists()
    assert operator.lane_status()[1] == "provider_runtime_recovery_failed"

    manager, operator, record_path = _operator_with_failing_instance_load(
        short_root / "begin", monkeypatch, failure
    )
    operator.mark_unavailable(
        "provider_process_group_survived_recovery",
        "recovery required",
        retryable=True,
    )
    operator._next_rearm_after = 0.0
    with pytest.raises(ProviderLocalRuntimeRefused) as begin_refusal:
        operator._begin_invocation()
    assert begin_refusal.value.code == "provider_unavailable"
    assert record_path.exists()
    assert operator.lane_status()[1] == "provider_runtime_recovery_failed"

    manager, operator, record_path = _operator_with_failing_instance_load(
        short_root / "invoker", monkeypatch, failure
    )
    operator.mark_unavailable(
        "provider_process_group_survived_recovery",
        "recovery required",
        retryable=True,
    )
    operator._next_rearm_after = 0.0
    invoker = operator.invoker_for(SimpleNamespace(), accepted_oid="0" * 40)
    with pytest.raises(ProviderLocalRuntimeRefused) as invoker_refusal:
        invoker.bind_provider(occurrence=SimpleNamespace())
    assert invoker_refusal.value.code == "provider_unavailable"
    assert record_path.exists()
    assert operator.lane_status()[1] == "provider_runtime_recovery_failed"
