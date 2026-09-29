"""Internal timer fires survive restarts and can be followed without replay or backfill."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_core.triggers.config import TriggerOperationalConfigV1, load_trigger_config
from cruxible_core.triggers.journal import (
    evaluate_triggers,
    journal_path,
    latest_sequence,
    schedule_deadline,
    trigger_events,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)
CONFIG = TriggerOperationalConfigV1()


def instance(root: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        root=root, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def test_cadences_fire_once_when_due_and_once_after_downtime(tmp_path: Path) -> None:
    world = instance(tmp_path)
    assert trigger_events(world) == () and not journal_path(world).exists()
    first = evaluate_triggers(world, now=NOW, config=CONFIG)
    assert {event.name for event in first} == {"evidence.sweep", "prediction.anchor_retry"}
    assert all(event.due_at == event.fired_at == NOW for event in first)
    assert evaluate_triggers(world, now=NOW, config=CONFIG) == ()
    hourly = evaluate_triggers(world, now=NOW + timedelta(hours=1), config=CONFIG)
    assert [event.name for event in hourly] == ["prediction.anchor_retry"]
    resumed = evaluate_triggers(instance(tmp_path), now=NOW + timedelta(days=10), config=CONFIG)
    assert len(resumed) == 2
    assert {event.due_at for event in resumed} == {
        NOW + timedelta(hours=2),
        NOW + timedelta(days=1),
    }
    assert evaluate_triggers(world, now=NOW + timedelta(days=10, seconds=1), config=CONFIG) == ()


def test_deadline_replacement_and_rearming_are_one_shot(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, config=CONFIG)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=2))
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=1), config=CONFIG) == ()
    (event,) = evaluate_triggers(world, now=NOW + timedelta(minutes=3), config=CONFIG)
    assert (event.name, event.due_at, event.fired_at) == (
        "next.expire",
        NOW + timedelta(minutes=2),
        NOW + timedelta(minutes=3),
    )
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=4), config=CONFIG) == ()
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=4))
    assert len(evaluate_triggers(world, now=NOW + timedelta(minutes=4), config=CONFIG)) == 1


def test_durable_ordered_journal_resumes_after_a_consumer_cursor(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, config=CONFIG)
    (page,) = trigger_events(world, limit=1)
    cursor = page.sequence
    evaluate_triggers(instance(tmp_path), now=NOW + timedelta(days=1), config=CONFIG)
    remaining = trigger_events(instance(tmp_path), after=cursor)
    all_events = trigger_events(world)
    assert (page, *remaining) == all_events
    assert [event.sequence for event in all_events] == [1, 2, 3, 4]
    assert trigger_events(world, after=remaining[-1].sequence) == ()
    sweeps = trigger_events(world, name="evidence.sweep")
    assert len(sweeps) == 2 and latest_sequence(world, name="evidence.sweep") == sweeps[-1].sequence
    with sqlite3.connect(journal_path(world)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE events SET name='other'")


def test_an_unknown_journal_is_retained_instead_of_rebuilt(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, config=CONFIG)
    with sqlite3.connect(journal_path(world)) as connection:
        connection.execute("PRAGMA user_version=42")
    with pytest.raises(ValueError, match="retain"):
        evaluate_triggers(world, now=NOW, config=CONFIG)
    with sqlite3.connect(journal_path(world)) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (2,)


def test_operational_config_is_closed_and_controls_intervals(tmp_path: Path) -> None:
    assert load_trigger_config(tmp_path) == CONFIG
    path = tmp_path / "daemon/triggers.json"
    path.parent.mkdir()
    for bad in (
        '{"unknown":1}',
        '{"evidence_sweep_interval_seconds":0}',
        '{"prediction_anchor_retry_interval_seconds":true}',
        '{"tag":"wrong"}',
        "{",
    ):
        path.write_text(bad)
        with pytest.raises(ValidationError):
            load_trigger_config(tmp_path)
    path.write_text(
        '{"evidence_sweep_interval_seconds":2,"prediction_anchor_retry_interval_seconds":3}'
    )
    config = load_trigger_config(tmp_path)
    world = instance(tmp_path / "instance")
    evaluate_triggers(world, now=NOW, config=config)
    assert [
        event.name
        for event in evaluate_triggers(world, now=NOW + timedelta(seconds=2), config=config)
    ] == ["evidence.sweep"]


def test_runner_fires_before_matching_consumers(tmp_path: Path) -> None:
    from cruxible_core.consumers.runner import ConsumerRunner

    world = instance(tmp_path)
    seen = []
    kind = SimpleNamespace(
        name="probe",
        active=lambda _: True,
        match=lambda world, **_: seen.extend(trigger_events(world)),
        due=lambda *_a, **_k: (),
    )
    runner = ConsumerRunner(SimpleNamespace(), kinds=(kind,))
    runner.match_once("instance", world, now=NOW)
    assert len(seen) == 2


def test_an_idle_tick_uses_only_a_read_connection_without_a_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, config=CONFIG)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    statements = []
    connections = []
    connect = sqlite3.connect

    def observed(*args, **kwargs):  # type: ignore[no-untyped-def]
        connections.append((args, kwargs))
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed)
    # An operator can hold the writer lock while an idle tick reads.
    with connect(journal_path(world), timeout=0.1) as writer:
        writer.execute("BEGIN IMMEDIATE")
        for seconds in (0, 1, 59):
            assert (
                evaluate_triggers(world, now=NOW + timedelta(seconds=seconds), config=CONFIG) == ()
            )
    assert len(connections) == 3
    assert all(args[0].endswith("?mode=ro") and kwargs["uri"] for args, kwargs in connections)
    assert "BEGIN IMMEDIATE" not in statements
    assert not any("synchronous" in statement.lower() for statement in statements)
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)


def test_due_triggers_are_rechecked_after_the_read_before_firing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.triggers import journal

    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, config=CONFIG)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    due = journal._due_triggers
    replaced = []

    def replace_after_read(connection, **kwargs):  # type: ignore[no-untyped-def]
        pending = due(connection, **kwargs)
        if pending and not replaced:
            replaced.append(True)
            # Finish the read before an operator replaces the deadline.
            connection.commit()
            schedule_deadline(world, "next.expire", NOW + timedelta(minutes=2))
        return pending

    monkeypatch.setattr(journal, "_due_triggers", replace_after_read)
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=1), config=CONFIG) == ()
    (event,) = evaluate_triggers(world, now=NOW + timedelta(minutes=2), config=CONFIG)
    assert event.name == "next.expire" and event.due_at == NOW + timedelta(minutes=2)
