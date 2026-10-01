"""Internal timer fires survive restarts and can be followed without replay or backfill."""

import sqlite3
from dataclasses import replace
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

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _cadence(
    trigger: str, action: str, interval: timedelta, *, accepted_at: datetime = NOW
) -> InternalTrigger:
    return InternalTrigger(
        trigger,
        action,
        CadenceScheduleV1(interval_seconds=int(interval.total_seconds())),
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


ACCEPTED = datetime(2026, 8, 28, 15, 2, tzinfo=UTC)


def _watched_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    """An instance whose live internal Triggers fire on a Capture landing and its window."""

    from tests.support.lines import action_trigger, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_line_triggers import SELECTOR, line_world

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
    assert [item.accepted_at for item in watched] == [ACCEPTED, ACCEPTED]
    return world, procedure, watched


def test_capture_schedules_fire_an_internal_action_once_per_event_carrying_it(
    tmp_path: Path,
) -> None:
    from tests.test_procedures.test_line_triggers import capture

    world, procedure, watched = _watched_world(tmp_path)
    assert [item.schedule.kind for item in watched] == ["window_close", "capture_landing"]

    # A Capture that landed before the Triggers were accepted never fires them.
    capture(world, procedure, at=ACCEPTED - timedelta(hours=1), partition="run:early")
    assert fire(world, ACCEPTED + timedelta(hours=1), triggers=watched, since=ACCEPTED) == ()

    landed_at = ACCEPTED + timedelta(hours=2)
    capture(world, procedure, at=landed_at, partition="run:landed")
    (fired,) = fire(world, landed_at + timedelta(seconds=1), triggers=watched, since=ACCEPTED)
    assert (fired.action, fired.trigger, fired.due_at) == (
        "evidence.sweep",
        "Trigger:sweep-on-landing",
        landed_at,
    )
    assert fired.event is not None and fired.event.partition_id == "run:landed"
    # The window anchored on the same landing closes a minute later, once.
    assert fire(world, landed_at + timedelta(seconds=30), triggers=watched, since=ACCEPTED) == ()
    (closed,) = fire(world, landed_at + timedelta(minutes=2), triggers=watched, since=ACCEPTED)
    assert (closed.action, closed.due_at, closed.event) == (
        "prediction.anchor_retry",
        landed_at + timedelta(minutes=1),
        fired.event,
    )
    assert fire(world, landed_at + timedelta(hours=1), triggers=watched, since=ACCEPTED) == ()
    # The worker following the action receives the event with the fire.
    (followed,) = trigger_events(world, action="evidence.sweep")
    assert followed.event == fired.event


@pytest.mark.parametrize("rebuilt_before_the_read", [True, False])
def test_an_index_rebuild_delivers_each_unread_capture_exactly_once(
    tmp_path: Path, rebuilt_before_the_read: bool
) -> None:
    from tests.test_procedures.test_line_triggers import capture

    from cruxible_core.service.procedures.procedure_runs import _journal

    world, procedure, watched = _watched_world(tmp_path)
    assert fire(world, ACCEPTED + timedelta(minutes=1), triggers=watched, since=ACCEPTED) == ()
    read = capture(
        world, procedure, at=ACCEPTED + timedelta(minutes=2), partition="run:read-before"
    )
    # Its landing and the window it anchors (closed at minute 3) are both read.
    assert len(fire(world, ACCEPTED + timedelta(minutes=3), triggers=watched, since=ACCEPTED)) == 2
    unread = capture(world, procedure, at=ACCEPTED + timedelta(minutes=4), partition="run:unread")
    journal, _ = _journal(world)
    if rebuilt_before_the_read:
        journal.index.path.unlink()
    else:
        # Read once from the old index, then rebuilt: the checkpoint is a record,
        # so the rebuilt index resumes after it rather than from its new head.
        fire(world, ACCEPTED + timedelta(minutes=4, seconds=30), triggers=watched, since=ACCEPTED)
        journal.index.path.unlink()
    later = []
    for minute in (5, 6, 7):
        later.extend(
            fire(world, ACCEPTED + timedelta(minutes=minute), triggers=watched, since=ACCEPTED)
        )
    landings = [
        event.event.record_digest
        for event in trigger_events(world, action="evidence.sweep")
        if event.event is not None
    ]
    assert landings == [read.record_digest, unread.record_digest]
    retries = [
        event.event.record_digest
        for event in trigger_events(world, action="prediction.anchor_retry")
        if event.event is not None
    ]
    assert retries == [read.record_digest, unread.record_digest]


def test_a_failed_capture_read_keeps_the_checkpoint_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_procedures.test_line_triggers import capture

    from cruxible_client.contracts.errors import PlaybillError
    from cruxible_core.exhaust.journal_index import JournalIndex
    from cruxible_core.triggers.journal import trigger_read_errors

    world, procedure, watched = _watched_world(tmp_path)
    landing = tuple(item for item in watched if item.schedule.kind == "capture_landing")
    fire(world, ACCEPTED + timedelta(minutes=1), triggers=landing, since=ACCEPTED)
    stored = capture(world, procedure, at=ACCEPTED + timedelta(minutes=2), partition="run:x")
    original = JournalIndex.captures

    def failing(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PlaybillError("index unreadable")

    monkeypatch.setattr(JournalIndex, "captures", failing)
    for minute in (3, 4):
        assert (
            fire(world, ACCEPTED + timedelta(minutes=minute), triggers=landing, since=ACCEPTED)
            == ()
        )
    assert trigger_read_errors(world) == {
        "Trigger:sweep-on-landing": "capture read failed: index unreadable"
    }
    # Recovered, it delivers what the failed reads did not consume, once.
    monkeypatch.setattr(JournalIndex, "captures", original)
    (recovered,) = fire(world, ACCEPTED + timedelta(minutes=5), triggers=landing, since=ACCEPTED)
    assert recovered.event is not None and recovered.event.record_digest == stored.record_digest
    assert trigger_read_errors(world) == {}


def test_a_large_backlog_is_read_in_bounded_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_procedures.test_line_triggers import capture

    from cruxible_core.triggers import journal

    world, procedure, watched = _watched_world(tmp_path)
    landing = tuple(item for item in watched if item.schedule.kind == "capture_landing")
    fire(world, ACCEPTED + timedelta(minutes=1), triggers=landing, since=ACCEPTED)
    for index in range(7):
        capture(
            world,
            procedure,
            at=ACCEPTED + timedelta(minutes=2, seconds=index),
            partition=f"run:backlog-{index}",
        )
    monkeypatch.setattr(journal, "_SCAN_BATCH", 2)
    monkeypatch.setattr(journal, "_SCAN_BATCHES_PER_PASS", 3)
    writers = []
    opened = journal._open

    def counted(instance, *, create=False):  # type: ignore[no-untyped-def]
        if create:
            writers.append(True)
        return opened(instance, create=create)

    monkeypatch.setattr(journal, "_open", counted)
    first = fire(world, ACCEPTED + timedelta(minutes=3), triggers=landing, since=ACCEPTED)
    # Three transactions of two records each, then the timer/window transaction.
    assert len(first) == 6 and len(writers) == 4
    rest = fire(world, ACCEPTED + timedelta(minutes=4), triggers=landing, since=ACCEPTED)
    assert len(rest) == 1
    due = [event.due_at for event in (*first, *rest)]
    assert due == sorted(due) and len(set(due)) == 7
