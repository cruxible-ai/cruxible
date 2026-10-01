"""Internal timer fires survive restarts and can be followed without replay or backfill."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.triggers import CadenceScheduleV1
from cruxible_core.triggers import journal as trigger_journal
from cruxible_core.triggers.journal import (
    InternalTrigger,
    evaluate_triggers,
    journal_path,
    latest_sequence,
    schedule_deadline,
    trigger_events,
)


def _cadence(trigger: str, action: str, interval: timedelta) -> InternalTrigger:
    return InternalTrigger(
        trigger, action, CadenceScheduleV1(interval_seconds=int(interval.total_seconds()))
    )


NOW = datetime(2026, 9, 29, tzinfo=UTC)
SWEEP = _cadence("Trigger:evidence-sweep", "evidence.sweep", timedelta(days=1))
RETRY = _cadence("Trigger:prediction-anchor-retry", "prediction.anchor_retry", timedelta(hours=1))
# The default Triggers a new instance is seeded with, as the journal reads them.
CONFIG = (SWEEP, RETRY)


def instance(root: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        root=root, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def test_cadences_fire_once_when_due_and_once_after_downtime(tmp_path: Path) -> None:
    world = instance(tmp_path)
    assert trigger_events(world) == () and not journal_path(world).exists()
    first = evaluate_triggers(world, now=NOW, triggers=CONFIG)
    assert {event.action for event in first} == {"evidence.sweep", "prediction.anchor_retry"}
    assert all(event.due_at == event.fired_at == NOW for event in first)
    assert evaluate_triggers(world, now=NOW, triggers=CONFIG) == ()
    hourly = evaluate_triggers(world, now=NOW + timedelta(hours=1), triggers=CONFIG)
    assert [event.action for event in hourly] == ["prediction.anchor_retry"]
    resumed = evaluate_triggers(instance(tmp_path), now=NOW + timedelta(days=10), triggers=CONFIG)
    assert len(resumed) == 2
    assert {event.due_at for event in resumed} == {
        NOW + timedelta(hours=2),
        NOW + timedelta(days=1),
    }
    assert evaluate_triggers(world, now=NOW + timedelta(days=10, seconds=1), triggers=CONFIG) == ()


def test_deadline_replacement_and_rearming_are_one_shot(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, triggers=CONFIG)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=2))
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=1), triggers=CONFIG) == ()
    (event,) = evaluate_triggers(world, now=NOW + timedelta(minutes=3), triggers=CONFIG)
    assert (event.action, event.due_at, event.fired_at) == (
        "next.expire",
        NOW + timedelta(minutes=2),
        NOW + timedelta(minutes=3),
    )
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=4), triggers=CONFIG) == ()
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=4))
    assert len(evaluate_triggers(world, now=NOW + timedelta(minutes=4), triggers=CONFIG)) == 1


def test_durable_ordered_journal_resumes_after_a_consumer_cursor(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, triggers=CONFIG)
    (page,) = trigger_events(world, limit=1)
    cursor = page.sequence
    evaluate_triggers(instance(tmp_path), now=NOW + timedelta(days=1), triggers=CONFIG)
    remaining = trigger_events(instance(tmp_path), after=cursor)
    all_events = trigger_events(world)
    assert (page, *remaining) == all_events
    assert [event.sequence for event in all_events] == [1, 2, 3, 4]
    assert trigger_events(world, after=remaining[-1].sequence) == ()
    sweeps = trigger_events(world, action="evidence.sweep")
    assert (
        len(sweeps) == 2 and latest_sequence(world, action="evidence.sweep") == sweeps[-1].sequence
    )
    with sqlite3.connect(journal_path(world)) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE events SET action='other'")


def test_an_unknown_journal_is_retained_instead_of_rebuilt(tmp_path: Path) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, triggers=CONFIG)
    with sqlite3.connect(journal_path(world)) as connection:
        connection.execute("PRAGMA user_version=42")
    with pytest.raises(ValueError, match="retain"):
        evaluate_triggers(world, now=NOW, triggers=CONFIG)
    with sqlite3.connect(journal_path(world)) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (2,)


def test_cadence_state_follows_the_accepted_trigger_set(tmp_path: Path) -> None:
    world = instance(tmp_path)
    first = evaluate_triggers(world, now=NOW, triggers=(SWEEP,))
    assert [(event.action, event.trigger) for event in first] == [
        ("evidence.sweep", "Trigger:evidence-sweep")
    ]
    # A new Trigger fires once, on the next tick after it is accepted.
    added = _cadence("Trigger:sweep-often", "evidence.sweep", timedelta(minutes=10))
    (fired,) = evaluate_triggers(world, now=NOW + timedelta(minutes=1), triggers=(SWEEP, added))
    assert (fired.action, fired.trigger) == ("evidence.sweep", "Trigger:sweep-often")
    # Two Triggers on one action both fire into it; a worker follows the action.
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=5), triggers=(SWEEP, added)) == ()
    (again,) = evaluate_triggers(world, now=NOW + timedelta(minutes=11), triggers=(SWEEP, added))
    assert again.trigger == "Trigger:sweep-often" and again.due_at == NOW + timedelta(minutes=11)
    # A changed interval takes effect from the Trigger's last fire.
    shorter = _cadence("Trigger:evidence-sweep", "evidence.sweep", timedelta(hours=1))
    (rescheduled,) = evaluate_triggers(
        world, now=NOW + timedelta(hours=1, minutes=5), triggers=(shorter,)
    )
    assert (rescheduled.trigger, rescheduled.due_at) == (
        "Trigger:evidence-sweep",
        NOW + timedelta(hours=1),
    )
    # A removed Trigger stops; the one that stays keeps its own chain.
    assert (
        evaluate_triggers(world, now=NOW + timedelta(hours=2, minutes=5), triggers=(added,))[
            0
        ].trigger
        == "Trigger:sweep-often"
    )
    sweeps = trigger_events(world, action="evidence.sweep")
    assert {event.trigger for event in sweeps} == {"Trigger:evidence-sweep", "Trigger:sweep-often"}


def test_a_deadline_cannot_take_the_name_of_a_trigger_action(tmp_path: Path) -> None:
    world = instance(tmp_path)
    for name in ("", "evidence.sweep", "prediction.anchor_retry"):
        with pytest.raises(ValueError, match="distinct from Trigger actions"):
            schedule_deadline(world, name, NOW)
    schedule_deadline(world, "next.expire", NOW)
    (event,) = evaluate_triggers(world, now=NOW, triggers=())
    assert (event.action, event.trigger) == ("next.expire", None)


def test_runner_fires_before_matching_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    # The runner fires the live internal Triggers at the instance's accepted head.
    monkeypatch.setattr(trigger_journal, "internal_triggers", lambda _instance: CONFIG)
    runner.match_once("instance", world, now=NOW)
    assert len(seen) == 2


def test_an_idle_tick_uses_only_a_read_connection_without_a_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = instance(tmp_path)
    evaluate_triggers(world, now=NOW, triggers=CONFIG)
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
                evaluate_triggers(world, now=NOW + timedelta(seconds=seconds), triggers=CONFIG)
                == ()
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
    evaluate_triggers(world, now=NOW, triggers=CONFIG)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    due = journal._due_timers
    replaced = []

    def replace_after_read(connection, **kwargs):  # type: ignore[no-untyped-def]
        pending = due(connection, **kwargs)
        if pending and not replaced:
            replaced.append(True)
            # Finish the read before an operator replaces the deadline.
            connection.commit()
            schedule_deadline(world, "next.expire", NOW + timedelta(minutes=2))
        return pending

    monkeypatch.setattr(journal, "_due_timers", replace_after_read)
    assert evaluate_triggers(world, now=NOW + timedelta(minutes=1), triggers=CONFIG) == ()
    (event,) = evaluate_triggers(world, now=NOW + timedelta(minutes=2), triggers=CONFIG)
    assert event.action == "next.expire" and event.due_at == NOW + timedelta(minutes=2)


def test_capture_schedules_fire_an_internal_action_once_per_event_carrying_it(
    tmp_path: Path,
) -> None:
    from tests.support.lines import action_trigger, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
    from tests.test_procedures.test_procedure_run_surface import READ_TIME

    from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1
    from cruxible_client.contracts.triggers import (
        CaptureLandingScheduleV1,
        WindowCloseScheduleV1,
    )
    from cruxible_core.triggers.journal import internal_triggers

    world, _line, procedure, owner = line_world(tmp_path, None, with_owner=True, triggers=())
    landing = action_trigger(
        "sweep-on-landing",
        action="evidence.sweep",
        schedule=CaptureLandingScheduleV1(event=SELECTOR),
    )
    windowed = action_trigger(
        "retry-after-window",
        action="prediction.anchor_retry",
        schedule=WindowCloseScheduleV1(
            window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=60)
        ),
    )
    tree = world.tree_at(world.accepted_coordinate().git_oid)
    tree.update(trigger_members(landing, windowed))
    _accept_tree(world, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="watch")
    watched = tuple(
        item
        for item in internal_triggers(world)
        if item.trigger in {"Trigger:sweep-on-landing", "Trigger:retry-after-window"}
    )
    assert [item.schedule.kind for item in watched] == ["window_close", "capture_landing"]

    capture(world, procedure, at=READ_TIME, partition="run:early")
    # First sight reads forward from here: what already landed never fires.
    assert evaluate_triggers(world, now=READ_TIME + timedelta(hours=1), triggers=watched) == ()

    landed_at = READ_TIME + timedelta(hours=2)
    capture(world, procedure, at=landed_at, partition="run:landed")
    (fired,) = evaluate_triggers(world, now=landed_at + timedelta(seconds=1), triggers=watched)
    assert (fired.action, fired.trigger, fired.due_at) == (
        "evidence.sweep",
        "Trigger:sweep-on-landing",
        landed_at,
    )
    assert fired.event is not None and fired.event.partition_id == "run:landed"
    # The window anchored on the same landing closes a minute later, once.
    assert evaluate_triggers(world, now=landed_at + timedelta(seconds=30), triggers=watched) == ()
    (closed,) = evaluate_triggers(world, now=landed_at + timedelta(minutes=2), triggers=watched)
    assert (closed.action, closed.due_at, closed.event) == (
        "prediction.anchor_retry",
        landed_at + timedelta(minutes=1),
        fired.event,
    )
    assert evaluate_triggers(world, now=landed_at + timedelta(hours=1), triggers=watched) == ()
    # The worker following the action receives the event with the fire.
    (followed,) = trigger_events(world, action="evidence.sweep")
    assert followed.event == fired.event
