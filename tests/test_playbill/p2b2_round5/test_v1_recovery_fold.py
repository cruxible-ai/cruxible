"""V-1: recovery acknowledgement is conservative and per invocation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import cruxible_core.playbill.provider_process_leases as lease_module
import cruxible_core.runtime.playbill_manager as manager_module
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_core.playbill.provider_process_leases import ProviderLocalRuntimeRefused
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

DEAD_PID = 99_999_991


def _plant(operator: ProviderRuntimeOperator, invocation_id: str) -> Path:
    store = operator.process_leases
    assert store is not None
    record, _control = store.paths(invocation_id)
    record.write_bytes(
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
    return record


class _Fold:
    def __init__(self) -> None:
        self.owner: dict[str, str] = {}
        self.fail_once: set[str] = set()
        self.completed: set[str] = set()
        self.appends: list[str] = []

    def __call__(self, instance: str, **kwargs: object) -> tuple[str, ...]:
        if instance in self.fail_once:
            self.fail_once.remove(instance)
            raise RuntimeError("transient owner failure")
        handled: list[str] = []
        for invocation_id in kwargs["invocation_ids"]:  # type: ignore[union-attr]
            if self.owner.get(invocation_id) != instance:
                continue
            if invocation_id not in self.completed:
                self.completed.add(invocation_id)
                self.appends.append(invocation_id)
            handled.append(invocation_id)
        return tuple(handled)


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    operator: ProviderRuntimeOperator,
    instances: tuple[str, ...],
    fold: _Fold,
) -> PlaybillInstanceManager:
    monkeypatch.setattr(lease_module, "_current_boot_id", lambda: "test-boot")
    manager = PlaybillInstanceManager()
    records = tuple(
        SimpleNamespace(backend="governed_daemon", instance_id=instance_id)
        for instance_id in instances
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )
    monkeypatch.setattr(manager, "get", lambda instance_id: instance_id)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_procedure_runs.service_recover_provider_invocations",
        fold,
    )
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))
    return manager


def test_owner_failure_retains_only_its_id_then_succeeds(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    ids = tuple("sha256:" + letter * 64 for letter in "def")
    records = {invocation_id: _plant(operator, invocation_id) for invocation_id in ids}
    fold = _Fold()
    for invocation_id, instance_id in zip(ids, ("inst_one", "inst_two", "inst_three"), strict=True):
        fold.owner[invocation_id] = instance_id
    fold.fail_once.add("inst_two")
    _wire(monkeypatch, operator, ("inst_one", "inst_two", "inst_three"), fold)
    operator.mark_unavailable("provider_process_group_survived_recovery", "retry", retryable=True)

    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert not records[ids[0]].exists()
    assert records[ids[1]].exists()
    assert not records[ids[2]].exists()
    assert sorted(fold.appends) == [ids[0], ids[2]]

    operator._next_rearm_after = 0.0
    operator._begin_invocation()
    operator._end_invocation()
    assert not records[ids[1]].exists()
    assert sorted(fold.appends) == sorted(ids)
    assert operator.lane_status() == ("available", None, None)


def test_unclaimed_id_is_terminally_released_when_every_fold_succeeds(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation_id = "sha256:" + "a" * 64
    record = _plant(operator, invocation_id)
    fold = _Fold()
    manager = _wire(monkeypatch, operator, ("inst_one",), fold)
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        manager_module._log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    result = operator.recover_all()

    dispositions = manager._fold_provider_recovery(operator, result)

    assert dispositions == {invocation_id: "unclaimed"}
    assert not record.exists()
    assert warnings == [
        (
            "provider_recovery_unclaimed",
            {"invocation_id": invocation_id, "terminal": True},
        )
    ]

    restart = ProviderRuntimeOperator(short_root)
    assert restart.process_leases is not None
    assert restart.process_leases.recover_all().completion_invocation_ids == ()
    assert restart.lane_status() == ("available", None, None)


def test_uninitialized_instance_is_a_non_owning_skip(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation_id = "sha256:" + "b" * 64
    record = _plant(operator, invocation_id)
    manager = PlaybillInstanceManager()
    records = (
        SimpleNamespace(backend="governed_daemon", instance_id="inst_live"),
        SimpleNamespace(backend="governed_daemon", instance_id="inst_bare"),
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )
    monkeypatch.setattr(lease_module, "_current_boot_id", lambda: "test-boot")

    def get(instance_id: str) -> str:
        if instance_id == "inst_bare":
            raise PlaybillBootstrapError("Playbill is not initialized")
        return instance_id

    monkeypatch.setattr(manager, "get", get)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_procedure_runs.service_recover_provider_invocations",
        lambda _instance, **kwargs: tuple(kwargs["invocation_ids"]),
    )
    result = operator.recover_all()

    dispositions = manager._fold_provider_recovery(operator, result)

    assert dispositions == {invocation_id: "handled"}
    assert not record.exists()


def test_only_uninitialized_instances_make_the_record_terminally_unclaimed(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation_id = "sha256:" + "9" * 64
    record = _plant(operator, invocation_id)
    manager = PlaybillInstanceManager()
    bare = SimpleNamespace(backend="governed_daemon", instance_id="inst_bare")
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: (bare,)),
    )
    monkeypatch.setattr(lease_module, "_current_boot_id", lambda: "test-boot")
    monkeypatch.setattr(
        manager,
        "get",
        lambda _instance_id: (_ for _ in ()).throw(
            PlaybillBootstrapError("Playbill is not initialized")
        ),
    )
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))
    operator.mark_unavailable("provider_process_group_survived_recovery", "startup", retryable=True)

    operator.recover_all_with_bound_fold()

    assert not record.exists()
    assert operator.lane_status() == ("available", None, None)


def test_startup_recovery_holds_the_operator_lock_through_the_fold(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation_id = "sha256:" + "c" * 64
    _plant(operator, invocation_id)
    fold = _Fold()
    fold.owner[invocation_id] = "inst_one"
    _wire(monkeypatch, operator, ("inst_one",), fold)
    observed: list[bool] = []
    bound = operator._recovery_fold
    assert bound is not None

    def assert_locked(result: object):  # type: ignore[no-untyped-def]
        observed.append(operator._lock._is_owned())  # type: ignore[attr-defined]
        return bound(result)  # type: ignore[arg-type]

    operator.bind_recovery_fold(assert_locked)
    operator.recover_all_with_bound_fold()

    assert observed == [True]
