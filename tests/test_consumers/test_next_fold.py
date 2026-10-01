"""The one next kind keeps independent findings parts moving and reports one health."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cruxible_client.contracts.triggers import CadenceScheduleV1
from cruxible_core.consumers import next as folded
from cruxible_core.consumers.next import NEXT_QUEUE, evidence, predictions, queue
from cruxible_core.consumers.protocol import ConsumerWork
from cruxible_core.consumers.runner import (
    ConsumerRunner,
    consumer_health,
    consumer_kinds,
    consumer_statuses,
)
from cruxible_core.server.config import get_disabled_consumers
from cruxible_core.service.discovery.next import _consumer_stalled_items
from cruxible_core.triggers.journal import InternalTrigger, evaluate_triggers
from tests.core_support._knowledge_loop_support import seed_claims
from tests.test_consumers.test_prediction_settlement import drain, fixed_world
from tests.test_integration.test_next_closed_loop import EVALUATION_TIME


def _cadence(trigger: str, action: str, interval: timedelta) -> InternalTrigger:
    return InternalTrigger(
        trigger, action, CadenceScheduleV1(interval_seconds=int(interval.total_seconds()))
    )


def test_one_kind_one_health_and_independent_part_cursors(tmp_path: Path) -> None:
    instance, _owner, _capture, _contract = fixed_world(tmp_path)
    drain(instance, now=EVALUATION_TIME)
    assert [kind.name for kind in consumer_kinds()] == ["line", "next"]
    (health,) = consumer_health(instance, now=EVALUATION_TIME)
    assert (health.kind, health.consumer_id, health.state) == ("next", "consumer:next", "running")
    assert set(health.detail) == {"queue", "evidence", "prediction"}
    assert health.detail["prediction"]["contracts"] == 1
    before = health.detail
    # Reopening the kind uses each part's durable cursor, even with a later clock.
    restarted = folded.NextConsumers()
    restarted.match(instance, now=EVALUATION_TIME + timedelta(days=100), daemon_id="restart")
    assert tuple(restarted.due(instance, now=EVALUATION_TIME)) == ()
    assert restarted.health(instance, now=EVALUATION_TIME)[0].detail == before
    (status,) = consumer_statuses(SimpleNamespace(open_instances=lambda: (("inst", instance),)))
    assert status.kind == "next" and status.consumer_id == "consumer:next"
    exhaust = instance.root / instance.descriptor.storage.exhaust
    assert not (exhaust / "evidence-availability").exists()
    assert not (exhaust / "prediction-settlement").exists()


@pytest.mark.parametrize("name", ("evidence", "prediction", "unknown", "line"))
def test_removed_or_unknown_disable_names_refuse(name: str) -> None:
    with pytest.raises(ValueError, match="Unknown CRUXIBLE_DISABLED_CONSUMERS"):
        get_disabled_consumers({"CRUXIBLE_DISABLED_CONSUMERS": name})
    assert get_disabled_consumers({"CRUXIBLE_DISABLED_CONSUMERS": " next, next, "}) == {"next"}
    assert get_disabled_consumers({}) == frozenset()


def test_one_error_remains_stalled_until_its_own_work_recovers(tmp_path: Path) -> None:
    instance, _owner, _capture, _contract = fixed_world(tmp_path)
    drain(instance, now=EVALUATION_TIME)
    evaluate_triggers(instance, now=EVALUATION_TIME)
    NEXT_QUEUE.match(instance, now=EVALUATION_TIME, daemon_id="daemon")
    work = next(
        work
        for work in NEXT_QUEUE.due(instance, now=EVALUATION_TIME)
        if work.key == "evidence:sweep"
    )
    manager = SimpleNamespace(get=lambda _id: instance)
    with patch.object(evidence._PART, "_check", side_effect=OSError("store unreadable")):
        with pytest.raises(OSError):
            NEXT_QUEUE.run(manager, "instance", work, now=EVALUATION_TIME)
    (health,) = consumer_health(instance, now=EVALUATION_TIME)
    assert health.state == "stalled" and health.detail["evidence"]["state"] == "stalled"
    assert health.detail["prediction"]["state"] == "running" and health.repair is not None
    rows = _consumer_stalled_items((health,))
    with sqlite3.connect(evidence._STATE.path(instance)) as connection:
        connection.execute("UPDATE progress SET last_error_at='later',sweep_completed_at='later'")
    assert _consumer_stalled_items(consumer_health(instance, now=EVALUATION_TIME)) == rows
    NEXT_QUEUE.run(manager, "instance", work, now=EVALUATION_TIME)
    assert consumer_health(instance, now=EVALUATION_TIME)[0].state == "running"


def test_a_part_that_cannot_match_does_not_block_the_other_parts(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    with patch.object(predictions, "_journals", side_effect=OSError("journal unreadable")):
        NEXT_QUEUE.match(instance, now=EVALUATION_TIME, daemon_id="daemon")
        work = tuple(NEXT_QUEUE.due(instance, now=EVALUATION_TIME))
        assert any(unit.key.startswith("queue:") for unit in work)
        assert all(not unit.key.startswith("prediction:") for unit in work)
        for unit in work:
            NEXT_QUEUE.run(
                SimpleNamespace(get=lambda _id: instance), "instance", unit, now=EVALUATION_TIME
            )
        (health,) = NEXT_QUEUE.health(instance, now=EVALUATION_TIME)
        assert (
            health.state == "stalled"
            and "journal unreadable" in health.detail["prediction"]["errors"]["match"]
        )
        assert health.detail["queue"]["state"] == "running"
    drain(instance, now=EVALUATION_TIME)
    assert NEXT_QUEUE.health(instance, now=EVALUATION_TIME)[0].state == "running"


def test_one_slow_part_does_not_block_other_keys_and_each_key_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = SimpleNamespace(
        root=tmp_path, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )
    entered, release, progressed = Event(), Event(), Event()
    calls: list[str] = []

    class Part:
        def __init__(self, name: str):
            self.name = name

        def due(self, _instance, *, now):  # type: ignore[no-untyped-def]
            return (ConsumerWork(key="unit", item=None),)

        def run(self, _manager, _id, _work, *, now):  # type: ignore[no-untyped-def]
            calls.append(self.name)
            if self.name == "queue":
                entered.set()
                assert release.wait(5)
            else:
                progressed.set()

    monkeypatch.setattr(folded, "_PARTS", {name: Part(name) for name in ("queue", "evidence")})
    runner = ConsumerRunner(SimpleNamespace(get=lambda _id: instance), kinds=(NEXT_QUEUE,))
    with ThreadPoolExecutor(max_workers=NEXT_QUEUE.workers) as pool:
        runner._executors = {"next": pool}
        units = tuple(NEXT_QUEUE.due(instance, now=EVALUATION_TIME))
        try:
            runner._schedule("inst", NEXT_QUEUE, units[0])
            assert entered.wait(5)
            runner._schedule("inst", NEXT_QUEUE, units[0])
            runner._schedule("inst", NEXT_QUEUE, units[1])
            assert progressed.wait(5)
            assert calls.count("queue") == 1
        finally:
            release.set()


@pytest.mark.parametrize("part", ("queue", "evidence", "prediction"))
def test_lag_in_any_part_lags_the_one_health_entry(tmp_path: Path, part: str) -> None:
    instance, _owner = seed_claims(tmp_path)
    config = (
        _cadence(
            "Trigger:evidence-sweep",
            "evidence.sweep",
            timedelta(seconds=100 if part == "evidence" else 1000),
        ),
        _cadence(
            "Trigger:prediction-anchor-retry",
            "prediction.anchor_retry",
            timedelta(seconds=100 if part == "prediction" else 1000),
        ),
    )
    evaluate_triggers(instance, now=EVALUATION_TIME, triggers=config)
    drain(instance, now=EVALUATION_TIME)
    assert NEXT_QUEUE.health(instance, now=EVALUATION_TIME)[0].state == "running"
    if part == "queue":
        instance.body_store().store(b"new CAS input")
    else:
        for seconds in (100, 200):
            evaluate_triggers(
                instance, now=EVALUATION_TIME + timedelta(seconds=seconds), triggers=config
            )
    (health,) = NEXT_QUEUE.health(instance, now=EVALUATION_TIME)
    assert health.state == "lagging" and health.detail[part]["state"] == "lagging"
    assert all(
        detail["state"] == "running" for name, detail in health.detail.items() if name != part
    )


def test_unknown_disable_setting_refuses_loop_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_DISABLED_CONSUMERS", "evidence")
    runner = ConsumerRunner(SimpleNamespace())
    with pytest.raises(ValueError, match="Unknown CRUXIBLE_DISABLED_CONSUMERS"):
        runner.start()
    assert runner.thread is None and runner._executors == {}


@pytest.mark.parametrize("part", ("queue", "evidence", "prediction"))
def test_lost_part_state_lags_until_that_part_rebuilds(tmp_path: Path, part: str) -> None:
    instance, _owner = seed_claims(tmp_path)
    drain(instance, now=EVALUATION_TIME)
    assert NEXT_QUEUE.health(instance, now=EVALUATION_TIME)[0].state == "running"
    modules = {"queue": queue, "evidence": evidence, "prediction": predictions}
    modules[part]._STATE.path(instance).unlink()
    (health,) = NEXT_QUEUE.health(instance, now=EVALUATION_TIME)
    assert health.state == "lagging"
    assert health.detail[part] == {"state": "lagging", "initialized": False}
    drain(instance, now=EVALUATION_TIME)
    assert NEXT_QUEUE.health(instance, now=EVALUATION_TIME)[0].state == "running"
