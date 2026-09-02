"""Round-5: attack U-4 -- the manager-owned transactional re-arm."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_core.playbill.provider_process_leases import (
    ProviderLocalRuntimeRefused,
    ProviderProcessRecoveryResultV1,
)
from cruxible_core.runtime.playbill_manager import PlaybillInstanceManager
from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

DEAD_PID = 99_999_991


def _plant_record(operator: ProviderRuntimeOperator, invocation: str) -> Path:
    store = operator.process_leases
    assert store is not None
    record_path, _control = store.paths(invocation)
    record_path.write_bytes(
        canonical_bytes(
            {
                "invocation_id": invocation,
                "pid": DEAD_PID,
                "process_group_id": DEAD_PID,
                "session_id": None,
                "boot_id": None,
                "process_start_time": None,
            }
        )
    )
    return record_path


class _Fold:
    """A stub with the real service's contract: already-completed ids are handled."""

    def __init__(self, *, instances: tuple[str, ...], failing: str | None = None) -> None:
        self.instances = instances
        self.failing = failing
        self.appends: list[tuple[str, str]] = []
        self.completed: dict[str, set[str]] = {name: set() for name in instances}
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.owner: dict[str, str] = {}

    def own(self, invocation_id: str, instance_id: str) -> None:
        self.owner[invocation_id] = instance_id

    def __call__(self, instance: object, **kwargs: object) -> tuple[str, ...]:
        instance_id = str(instance)
        wanted = tuple(str(item) for item in kwargs["invocation_ids"])  # type: ignore[arg-type]
        self.calls.append((instance_id, wanted))
        if instance_id == self.failing:
            raise RuntimeError("planted journal failure")
        handled: set[str] = set()
        for invocation_id in wanted:
            if self.owner.get(invocation_id, instance_id) != instance_id:
                continue
            if invocation_id in self.completed[instance_id]:
                handled.add(invocation_id)  # already journaled; no second append
                continue
            self.completed[instance_id].add(invocation_id)
            self.appends.append((instance_id, invocation_id))
            handled.add(invocation_id)
        return tuple(sorted(handled))


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    operator: ProviderRuntimeOperator,
    fold: _Fold,
) -> PlaybillInstanceManager:
    manager = PlaybillInstanceManager()
    records = tuple(
        SimpleNamespace(backend="governed_daemon", instance_id=name) for name in fold.instances
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


# --------------------------------------------- crash between fold and acknowledge


def test_a_crash_between_fold_and_acknowledgement_retains_the_record_and_refolds_once(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation = "sha256:" + "c" * 64
    record_path = _plant_record(operator, invocation)
    fold = _Fold(instances=("inst_one",))
    _wire(monkeypatch, operator, fold)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)

    # Crash simulation: the acknowledgement never reaches the store.
    def crash(_ids: tuple[str, ...]) -> None:
        raise RuntimeError("daemon died before acknowledging")

    monkeypatch.setattr(operator, "acknowledge_recovery", crash)
    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()
    assert fold.appends == [("inst_one", invocation)]
    assert record_path.exists(), "the record must survive a crash before acknowledgement"
    assert operator.lane_status()[0] == "unavailable"

    # Restart: a brand-new operator on the same state root re-folds the same id.
    restart = ProviderRuntimeOperator(short_root)
    _wire(monkeypatch, restart, fold)
    restart.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)
    restart._begin_invocation()
    restart._end_invocation()
    assert fold.appends == [("inst_one", invocation)], "completion duplicated on restart"
    assert fold.calls[-1] == ("inst_one", (invocation,))
    assert not record_path.exists()
    assert restart.lane_status() == ("available", None, None)


# ------------------------------------------------- fold failure on instance 2 of 3


def test_a_fold_failure_on_instance_two_retains_only_its_unhandled_record(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    ids = tuple("sha256:" + letter * 64 for letter in "def")
    records = {invocation: _plant_record(operator, invocation) for invocation in ids}
    fold = _Fold(instances=("inst_one", "inst_two", "inst_three"), failing="inst_two")
    for invocation, instance in zip(ids, ("inst_one", "inst_two", "inst_three"), strict=True):
        fold.own(invocation, instance)
    _wire(monkeypatch, operator, fold)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)

    with pytest.raises(ProviderLocalRuntimeRefused):
        operator._begin_invocation()

    assert sorted(fold.appends) == [("inst_one", ids[0]), ("inst_three", ids[2])]
    assert not records[ids[0]].exists()
    assert records[ids[1]].exists()
    assert not records[ids[2]].exists()
    assert operator.lane_status()[0] == "unavailable"

    # The owner heals: only its retained identity is re-folded and acknowledged.
    fold.failing = None
    operator._next_rearm_after = 0.0
    operator._begin_invocation()
    operator._end_invocation()
    assert sorted(fold.appends) == [
        ("inst_one", ids[0]),
        ("inst_three", ids[2]),
        ("inst_two", ids[1]),
    ]
    assert not any(path.exists() for path in records.values())
    assert operator.lane_status() == ("available", None, None)


def test_an_acknowledgement_for_an_id_the_fold_never_saw_is_a_no_op(short_root: Path) -> None:
    operator = ProviderRuntimeOperator(short_root)
    store = operator.process_leases
    assert store is not None
    kept = _plant_record(operator, "sha256:" + "e" * 64)
    operator.acknowledge_recovery(("sha256:" + "f" * 64,))
    assert kept.exists()
    assert store._pending_releases == {}


# ------------------------------------------------------- concurrency with startup


def test_rearm_cannot_run_while_an_invocation_is_in_flight(short_root: Path) -> None:
    operator = ProviderRuntimeOperator(short_root)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)
    operator._rearm_required = True
    operator._in_flight = 1
    calls: list[str] = []
    assert operator.process_leases is not None
    operator.process_leases.recover_all = (  # type: ignore[method-assign]
        lambda **_k: (
            calls.append("scan")
            or ProviderProcessRecoveryResultV1(recovered=(), removed=(), could_not_clean=())
        )
    )
    operator._lazy_rearm_locked()
    assert calls == []
    with pytest.raises(ProviderLocalRuntimeRefused) as excinfo:
        operator.recover_all()
    assert excinfo.value.code == "provider_process_lease_invalid"
    operator._in_flight = 0
    operator._lazy_rearm_locked()
    assert calls == ["scan"]


def test_startup_recovery_and_a_concurrent_rearm_serialize_on_the_operator_lock(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator = ProviderRuntimeOperator(short_root)
    invocation = "sha256:" + "1" * 64
    _plant_record(operator, invocation)
    fold = _Fold(instances=("inst_one",))
    manager = _wire(monkeypatch, operator, fold)
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)
    assert operator.process_leases is not None
    real = operator.process_leases.recover_all
    order: list[str] = []

    def slow(**kwargs: object):  # type: ignore[no-untyped-def]
        order.append("scan-start")
        time.sleep(0.15)
        order.append("scan-end")
        return real(**kwargs)  # type: ignore[arg-type]

    operator.process_leases.recover_all = slow  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def rearm() -> None:
        try:
            time.sleep(0.03)
            operator._begin_invocation()
            operator._end_invocation()
        except ProviderLocalRuntimeRefused:
            pass
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=rearm)
    thread.start()
    manager.recover_provider_runtime()
    thread.join()
    assert errors == []
    # No interleaving of the two scans.
    assert order[:2] == ["scan-start", "scan-end"]
    assert fold.appends.count(("inst_one", invocation)) == 1


# ------------------------------------------ a recovered start with no governed home


def test_a_recovered_start_with_no_governed_home_is_terminally_unclaimed(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-1: a fully scanned but unclaimed identity is released without retry."""

    operator = ProviderRuntimeOperator(short_root)
    invocation = "sha256:" + "2" * 64
    record_path = _plant_record(operator, invocation)
    fold = _Fold(instances=("inst_one",))
    fold.own(invocation, "inst_absent")  # the run lives in an instance that is gone
    _wire(monkeypatch, operator, fold)
    operator.mark_unavailable("provider_process_group_survived_recovery", "stuck", retryable=True)

    operator._begin_invocation()
    operator._end_invocation()
    assert not record_path.exists()
    assert operator.lane_status() == ("available", None, None)

    restart = ProviderRuntimeOperator(short_root)
    _wire(monkeypatch, restart, fold)
    restart.mark_unavailable("provider_process_group_survived_recovery", "s", retryable=True)
    restart._begin_invocation()
    restart._end_invocation()
    assert not record_path.exists()
    assert restart.lane_status() == ("available", None, None)
    assert fold.appends == []


def test_one_governed_instance_without_playbill_is_skipped_as_a_non_owner(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-1: an opt-out governed instance cannot poison Provider recovery."""

    from cruxible_client.contracts.errors import PlaybillBootstrapError

    operator = ProviderRuntimeOperator(short_root)
    invocation = "sha256:" + "3" * 64
    record_path = _plant_record(operator, invocation)

    bare = SimpleNamespace(
        instance_id="inst_bare",
        backend="governed_daemon",
        location=str(short_root / "no-playbill-here"),
        workspace_root=None,
    )
    registry = SimpleNamespace(
        list_instances=lambda: (bare,),
        get=lambda instance_id: bare if instance_id == "inst_bare" else None,
        state_root=short_root / "registry",
    )
    monkeypatch.setattr("cruxible_core.runtime.playbill_manager.get_registry", lambda: registry)
    manager = PlaybillInstanceManager()  # the REAL get(), no stub
    with pytest.raises(PlaybillBootstrapError):
        manager.get("inst_bare")
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))

    manager.recover_provider_runtime()
    assert operator.lane_status() == ("available", None, None)
    assert not record_path.exists()

    for _ in range(3):
        operator._begin_invocation()
        operator._end_invocation()
    assert not record_path.exists()
    assert operator.lane_status() == ("available", None, None)

    # A daemon restart does not help either.
    restart = ProviderRuntimeOperator(short_root)
    manager2 = PlaybillInstanceManager()
    monkeypatch.setattr(manager2, "provider_runtime_operator", lambda: restart)
    restart.bind_recovery_fold(lambda result: manager2._fold_provider_recovery(restart, result))
    manager2.recover_provider_runtime()
    assert restart.lane_status() == ("available", None, None)
    assert not record_path.exists()


def test_a_healthy_fold_is_acknowledged_when_a_sibling_instance_lacks_playbill(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V-1: an opt-out sibling is skipped after the healthy owner claims the id."""

    operator = ProviderRuntimeOperator(short_root)
    invocation = "sha256:" + "4" * 64
    record_path = _plant_record(operator, invocation)
    folded: list[str] = []

    def service(instance: object, **kwargs: object) -> tuple[str, ...]:
        if instance == "inst_bare":
            from cruxible_client.contracts.errors import PlaybillBootstrapError

            raise PlaybillBootstrapError("Playbill is not initialized for this instance")
        ids = tuple(str(item) for item in kwargs["invocation_ids"])  # type: ignore[arg-type]
        folded.extend(ids)
        return ids

    manager = PlaybillInstanceManager()
    records = (
        SimpleNamespace(backend="governed_daemon", instance_id="inst_playbill"),
        SimpleNamespace(backend="governed_daemon", instance_id="inst_bare"),
    )
    monkeypatch.setattr(
        "cruxible_core.runtime.playbill_manager.get_registry",
        lambda: SimpleNamespace(list_instances=lambda: records),
    )
    monkeypatch.setattr(manager, "get", lambda instance_id: instance_id)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_procedure_runs.service_recover_provider_invocations",
        service,
    )
    monkeypatch.setattr(manager, "provider_runtime_operator", lambda: operator)
    operator.bind_recovery_fold(lambda result: manager._fold_provider_recovery(operator, result))

    manager.recover_provider_runtime()
    assert folded == [invocation]
    assert not record_path.exists()
    assert operator.lane_status() == ("available", None, None)
    for _ in range(2):
        operator._begin_invocation()
        operator._end_invocation()
    assert not record_path.exists()
    assert folded == [invocation]
