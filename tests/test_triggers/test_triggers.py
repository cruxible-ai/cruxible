"""Internal timer fires survive restarts and can be followed without replay or backfill."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.triggers import CadenceSchedule
from cruxible_core.triggers import journal as trigger_journal
from cruxible_core.triggers.journal import (
    InternalTrigger,
    evaluate_triggers,
    journal_path,
    latest_sequence,
    schedule_deadline,
    trigger_events,
)

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _cadence(
    trigger: str, action: str, interval: timedelta, *, accepted_at: datetime = NOW
) -> InternalTrigger:
    return InternalTrigger(
        trigger,
        action,
        CadenceSchedule(interval_seconds=int(interval.total_seconds())),
        accepted_at=accepted_at,
    )


SWEEP = _cadence("Trigger:evidence-sweep", "evidence.sweep", timedelta(days=1))
RETRY = _cadence("Trigger:prediction-anchor-retry", "prediction.anchor_retry", timedelta(hours=1))
# The default Triggers a new instance is seeded with, as the journal reads them,
# both accepted at NOW.
CONFIG = (SWEEP, RETRY)


def instance(root: Path):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        root=root, descriptor=SimpleNamespace(storage=SimpleNamespace(exhaust="exhaust"))
    )


def fire(world, at, *, triggers=CONFIG, since=NOW):  # type: ignore[no-untyped-def]
    """Evaluate at ``at`` as a daemon that began listening at ``since``."""

    return evaluate_triggers(world, now=at, listening_since=since, triggers=triggers)


def test_a_new_timer_first_fires_one_interval_after_its_acceptance(tmp_path: Path) -> None:
    world = instance(tmp_path)
    assert trigger_events(world) == () and not journal_path(world).exists()
    # Never on sight: the first instant is acceptance plus one interval.
    assert fire(world, NOW) == ()
    assert fire(world, NOW + timedelta(minutes=59)) == ()
    (hourly,) = fire(world, NOW + timedelta(hours=1))
    assert (hourly.action, hourly.due_at) == ("prediction.anchor_retry", NOW + timedelta(hours=1))
    assert fire(world, NOW + timedelta(hours=1)) == ()
    # A late evaluation within one listening session delivers each instant once.
    late = fire(world, NOW + timedelta(hours=3, minutes=1))
    assert [event.due_at for event in late] == [
        NOW + timedelta(hours=2),
        NOW + timedelta(hours=3),
    ]


def test_instants_passed_while_no_daemon_listened_are_skipped_not_caught_up(
    tmp_path: Path,
) -> None:
    world = instance(tmp_path)
    fire(world, NOW + timedelta(hours=1))
    # Down for ten days; the restarted daemon listens from its own start.
    restarted = NOW + timedelta(days=10, minutes=30)
    assert fire(instance(tmp_path), restarted, since=restarted) == ()
    (retry,) = fire(world, NOW + timedelta(days=10, hours=1), since=restarted)
    assert retry.due_at == NOW + timedelta(days=10, hours=1)
    # Down again; the cadence keeps its own grid: the daily sweep falls due at
    # acceptance plus eleven days, exactly when the next daemon starts.
    eleven = fire(world, NOW + timedelta(days=11), since=NOW + timedelta(days=11))
    assert {(event.action, event.due_at) for event in eleven} == {
        ("evidence.sweep", NOW + timedelta(days=11)),
        ("prediction.anchor_retry", NOW + timedelta(days=11)),
    }


def test_a_resumed_cadence_ticks_on_its_grid_never_at_the_resume_itself() -> None:
    from cruxible_core.triggers.cadence import timer_due, timer_instants

    two_minutes = CadenceSchedule(interval_seconds=120)

    def due(last):  # type: ignore[no-untyped-def]
        return timer_due(two_minutes, accepted_at=NOW, last=last)

    def resumed_at(resume):  # type: ignore[no-untyped-def]
        """The first instant a reader resumed at `resume` reads: at or after it."""

        return next(
            timer_instants(
                two_minutes,
                accepted_at=NOW,
                after=resume - timedelta(microseconds=1),
                through=resume + timedelta(days=1),
            )
        )

    # The chain's next grid instant after its last fire, wherever in the interval
    # that fire was dispatched.
    assert due(NOW) == NOW + timedelta(minutes=2)
    assert due(NOW + timedelta(minutes=2, seconds=7)) == NOW + timedelta(minutes=4)
    # A resumed reader starts at the first grid instant at or after its resume:
    # a Line resumed at 00:09:30 next ticks at 00:10:00, never at 00:09:30 itself.
    assert resumed_at(NOW + timedelta(minutes=9, seconds=30)) == NOW + timedelta(minutes=10)
    # An instant exactly at the resume is on the grid and is the resumed range's.
    on_grid = NOW + timedelta(minutes=10)
    assert resumed_at(on_grid) == on_grid


def test_deadline_replacement_and_rearming_are_one_shot(tmp_path: Path) -> None:
    world = instance(tmp_path)
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=1))
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=2))
    assert fire(world, NOW + timedelta(minutes=1)) == ()
    (event,) = fire(world, NOW + timedelta(minutes=3))
    assert (event.action, event.due_at, event.fired_at) == (
        "next.expire",
        NOW + timedelta(minutes=2),
        NOW + timedelta(minutes=3),
    )
    assert fire(world, NOW + timedelta(minutes=4)) == ()
    schedule_deadline(world, "next.expire", NOW + timedelta(minutes=4))
    assert len(fire(world, NOW + timedelta(minutes=4))) == 1


def test_durable_ordered_journal_resumes_after_a_consumer_cursor(tmp_path: Path) -> None:
    world = instance(tmp_path)
    day = NOW + timedelta(days=1)
    fire(world, day, since=day)
    (page,) = trigger_events(world, limit=1)
    cursor = page.sequence
    fire(instance(tmp_path), day + timedelta(days=1), since=day + timedelta(days=1))
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
    day = NOW + timedelta(days=1)
    fire(world, day, since=day)
    with sqlite3.connect(journal_path(world)) as connection:
        connection.execute("PRAGMA user_version=42")
    with pytest.raises(ValueError, match="retain"):
        fire(world, day + timedelta(days=1), since=day)
    with sqlite3.connect(journal_path(world)) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (2,)


def test_timer_state_follows_the_accepted_trigger_set(tmp_path: Path) -> None:
    world = instance(tmp_path)
    (first,) = fire(world, NOW + timedelta(days=1), triggers=(SWEEP,))
    assert (first.action, first.trigger) == ("evidence.sweep", "Trigger:evidence-sweep")
    # A Trigger accepted later fires first one interval after its own acceptance.
    added = _cadence(
        "Trigger:sweep-often",
        "evidence.sweep",
        timedelta(minutes=10),
        accepted_at=NOW + timedelta(days=1, minutes=1),
    )
    both = (SWEEP, added)
    assert fire(world, NOW + timedelta(days=1, minutes=5), triggers=both) == ()
    (fired,) = fire(world, NOW + timedelta(days=1, minutes=11), triggers=both)
    assert (fired.trigger, fired.due_at) == (
        "Trigger:sweep-often",
        NOW + timedelta(days=1, minutes=11),
    )
    # A successor schedule starts from its own acceptance, never from the old grid.
    replaced_at = NOW + timedelta(days=1, hours=2, minutes=30)
    hourly = _cadence(
        "Trigger:evidence-sweep", "evidence.sweep", timedelta(hours=1), accepted_at=replaced_at
    )
    assert fire(world, replaced_at + timedelta(minutes=59), triggers=(hourly,)) == ()
    (rescheduled,) = fire(world, replaced_at + timedelta(hours=1), triggers=(hourly,))
    assert (rescheduled.trigger, rescheduled.due_at) == (
        "Trigger:evidence-sweep",
        replaced_at + timedelta(hours=1),
    )
    # A removed Trigger stops; the one that stays keeps its own grid.
    later = fire(world, NOW + timedelta(days=1, hours=5, minutes=1), triggers=(added,))
    assert {event.trigger for event in later} == {"Trigger:sweep-often"}


def test_a_deadline_cannot_take_the_name_of_a_trigger_action(tmp_path: Path) -> None:
    world = instance(tmp_path)
    for name in ("", "evidence.sweep", "prediction.anchor_retry"):
        with pytest.raises(ValueError, match="distinct from Trigger actions"):
            schedule_deadline(world, name, NOW)
    schedule_deadline(world, "next.expire", NOW)
    (event,) = fire(world, NOW, triggers=())
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
    # Both accepted a day before the runner starts: it fires what falls due at its
    # own start, never the instants before it.
    accepted_earlier = tuple(replace(item, accepted_at=NOW - timedelta(days=1)) for item in CONFIG)
    monkeypatch.setattr(trigger_journal, "internal_triggers", lambda _instance: accepted_earlier)
    runner.match_once("instance", world, now=NOW)
    assert len(seen) == 2


def test_an_idle_tick_uses_only_a_read_connection_without_a_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = instance(tmp_path)
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
            assert fire(world, NOW + timedelta(seconds=seconds)) == ()
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
    assert fire(world, NOW + timedelta(minutes=1)) == ()
    (event,) = fire(world, NOW + timedelta(minutes=2))
    assert event.action == "next.expire" and event.due_at == NOW + timedelta(minutes=2)


def test_restarting_the_same_runner_skips_the_ticks_it_missed_while_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.consumers import runner as runner_module
    from cruxible_core.consumers.runner import ConsumerRunner

    world = instance(tmp_path)
    hourly = (replace(RETRY, accepted_at=NOW - timedelta(hours=1)),)
    monkeypatch.setattr(trigger_journal, "internal_triggers", lambda _instance: hourly)
    # The loop thread reads no registry: it must not see a real state root.
    monkeypatch.setattr(
        runner_module, "get_registry", lambda: SimpleNamespace(list_instances=lambda: ())
    )
    runner = ConsumerRunner(SimpleNamespace(), kinds=())
    runner.start()
    runner.match_once("instance", world, now=NOW)
    assert [event.due_at for event in trigger_events(world)] == [NOW]
    runner.close()
    # Stopped for four hours, then started again in place.
    runner.start()
    try:
        runner.match_once("instance", world, now=NOW + timedelta(hours=4, minutes=30))
        assert [event.due_at for event in trigger_events(world)] == [NOW]
        runner.match_once("instance", world, now=NOW + timedelta(hours=5))
    finally:
        runner.close()
    assert [event.due_at for event in trigger_events(world)] == [NOW, NOW + timedelta(hours=5)]


def test_generation_fires_coalesce_and_restart_skips_offline_accepts(tmp_path: Path) -> None:
    from contextlib import contextmanager

    from cruxible_client.contracts.triggers import GenerationAcceptedSchedule

    world = instance(tmp_path)
    head = [2]

    @contextmanager
    def history():
        yield SimpleNamespace(
            sequence=head[0], generation=lambda sequence: SimpleNamespace(git_oid=str(sequence))
        )

    world.accepted_history_reader = history
    world.accepted_evaluation_time = lambda oid: NOW + timedelta(seconds=int(oid))
    trigger = InternalTrigger(
        "Trigger:floor",
        "floor.refresh",
        GenerationAcceptedSchedule(),
        accepted_at=NOW + timedelta(seconds=2),
        accepted_generation=2,
        version="v1",
    )
    assert fire(world, NOW + timedelta(seconds=2), triggers=(trigger,), since=NOW) == ()
    # One record covers a burst, not one record per accepted generation.
    head[0] = 5
    (event,) = fire(world, NOW + timedelta(seconds=6), triggers=(trigger,), since=NOW)
    assert (event.action, event.due_at) == ("floor.refresh", NOW + timedelta(seconds=5))
    assert fire(world, NOW + timedelta(seconds=7), triggers=(trigger,), since=NOW) == ()
    head[0] = 8
    restart = NOW + timedelta(seconds=9)
    assert fire(world, restart, triggers=(trigger,), since=restart) == ()
    head[0] = 9
    assert len(fire(world, restart + timedelta(seconds=1), triggers=(trigger,), since=restart)) == 1
    # A successor starts at its own acceptance, even with retained coverage.
    successor = replace(
        trigger, accepted_generation=10, accepted_at=NOW + timedelta(seconds=10), version="v2"
    )
    head[0] = 10
    assert fire(world, NOW + timedelta(seconds=11), triggers=(successor,), since=restart) == ()


def test_version_two_journal_migrates_in_place_without_losing_fires(tmp_path: Path) -> None:
    world = instance(tmp_path)
    fire(world, NOW + timedelta(hours=1))
    events = trigger_events(world)
    with sqlite3.connect(journal_path(world)) as connection:
        timers = connection.execute("SELECT * FROM timers").fetchall()
        connection.execute("DROP TABLE generations")
        connection.execute("PRAGMA user_version=2")
    assert trigger_events(world) == events
    schedule_deadline(world, "next.expire", NOW + timedelta(hours=2))
    with sqlite3.connect(journal_path(world)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
        assert connection.execute("SELECT * FROM timers").fetchall() == timers
    assert trigger_events(world) == events


@pytest.mark.parametrize("second,expected", [(-1, 0), (0, 1), (12345, 24691), (2**18, 2**18)])
def test_generation_time_lookup_bisects_and_keeps_the_last_equal_instant(
    tmp_path, second, expected
):
    from contextlib import contextmanager

    from cruxible_core.triggers.journal import generation_at

    world = instance(tmp_path)
    calls = []

    @contextmanager
    def history():
        yield SimpleNamespace(
            sequence=2**18, generation=lambda sequence: SimpleNamespace(git_oid=str(sequence))
        )

    def evaluation_time(oid):
        calls.append(oid)
        return NOW + timedelta(seconds=int(oid) // 2)

    world.accepted_history_reader = history
    world.accepted_evaluation_time = evaluation_time
    assert generation_at(world, NOW + timedelta(seconds=second)) == expected
    assert len(calls) <= 20


@pytest.mark.parametrize("offsets,second,expected", [((5, 1, 4, 3), 2, 1), ((0, 1, 7, 2, 4), 3, 3)])
def test_out_of_order_generation_times_fall_back_without_stopping_trigger_ticks(
    tmp_path, offsets, second, expected
):
    from contextlib import contextmanager

    from cruxible_client.contracts.triggers import GenerationAcceptedSchedule
    from cruxible_core.triggers.journal import generation_at

    world = instance(tmp_path)

    @contextmanager
    def history():
        yield SimpleNamespace(
            sequence=len(offsets) - 1,
            generation=lambda sequence: SimpleNamespace(git_oid=str(sequence)),
        )

    world.accepted_history_reader = history
    world.accepted_evaluation_time = lambda oid: NOW + timedelta(seconds=offsets[int(oid)])
    listening_since = NOW + timedelta(seconds=second)
    assert generation_at(world, listening_since) == expected
    trigger = InternalTrigger(
        "Trigger:floor",
        "floor.refresh",
        GenerationAcceptedSchedule(),
        accepted_at=NOW,
        accepted_generation=0,
        version="v1",
    )
    assert (
        len(fire(world, NOW + timedelta(seconds=10), triggers=(trigger,), since=listening_since))
        == 1
    )
    assert (
        fire(world, NOW + timedelta(seconds=11), triggers=(trigger,), since=listening_since) == ()
    )
