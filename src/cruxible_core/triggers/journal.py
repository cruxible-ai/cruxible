"""A retained trigger log makes internal fires resumable across daemon and worker restarts.

Which internal Triggers exist is governed: every one is a live Trigger artifact
aimed at an internal action, read from the instance's accepted state. The log
records which Trigger fired, which action it fired and, for a schedule that
fires on a Capture, the exact event, so a worker follows an action however many
Triggers schedule it and may use or ignore the event. Timer state is kept per
Trigger and follows the accepted Trigger set: a new Trigger fires on the next
tick, a changed interval counts from its last fire, and a retired Trigger stops.

A Trigger that fires on Captures reads the procedure journal's Capture index
forward from where it first saw it, never back-filling what landed before; a
changed schedule starts reading afresh. Each event fires a Trigger once.

Deadlines are not Triggers. A worker schedules its own (``next.expire``) as an
operational refresh signal; they fire into the same log, under their action and
with no Trigger.

Firing and advancing a timer commit together. Unlike findings projections this
store is never rebuilt or discarded: it is the operational authority for which
fires happened. Readers seek by sequence and may retain their own resume cursor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.procedures.windows import (
    TriggerEventReferenceV1,
    bind_observation_window,
)
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    CadenceScheduleV1,
    CaptureLandingScheduleV1,
    TriggerScheduleV1,
    TriggerV1,
    WindowCloseScheduleV1,
    schedule_capture_selector,
)
from cruxible_core.triggers.cadence import cadence_due

_SCHEMA = """
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL, trigger_id TEXT, due_at TEXT NOT NULL, fired_at TEXT NOT NULL,
 event TEXT, occurrence TEXT
) STRICT;
CREATE INDEX events_by_action ON events(action,sequence);
CREATE UNIQUE INDEX events_once ON events(trigger_id,occurrence) WHERE occurrence IS NOT NULL;
CREATE TABLE cadences (trigger_id TEXT PRIMARY KEY, last_fired_at TEXT NOT NULL) STRICT;
CREATE TABLE scans (trigger_id TEXT PRIMARY KEY, schedule TEXT NOT NULL, position TEXT NOT NULL)
 STRICT;
CREATE TABLE windows (
 trigger_id TEXT NOT NULL, occurrence TEXT NOT NULL, ends_at TEXT NOT NULL, event TEXT NOT NULL,
 PRIMARY KEY(trigger_id,occurrence)
) STRICT;
CREATE TABLE deadlines (name TEXT PRIMARY KEY, due_at TEXT NOT NULL) STRICT;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
PRAGMA user_version=2;
"""
_VERSION = 2


@dataclass(frozen=True)
class InternalTrigger:
    """One live Trigger's schedule toward one internal action."""

    trigger: str
    action: str
    schedule: TriggerScheduleV1

    @classmethod
    def of(cls, trigger: TriggerV1) -> InternalTrigger:
        action = trigger.action
        if action is None:
            raise ValueError("an internal Trigger fires an internal action")
        return cls(trigger.identity.qualified, action, trigger.schedule)


@dataclass(frozen=True)
class TriggerEvent:
    sequence: int
    action: str
    #: The Trigger that fired, or None for a worker's own deadline.
    trigger: str | None
    due_at: datetime
    fired_at: datetime
    #: The Capture a Capture-driven schedule fired on; None for a timer.
    event: TriggerEventReferenceV1 | None = None


@dataclass(frozen=True)
class _Fire:
    action: str
    trigger: str | None
    due: datetime
    event: TriggerEventReferenceV1 | None = None
    occurrence: str | None = None


def journal_path(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / "triggers.sqlite3"


# Per instance root: the accepted generation last read and its live internal Triggers.
_TRIGGERS: dict[str, tuple[str, tuple[InternalTrigger, ...]]] = {}
_TRIGGERS_LOCK = threading.Lock()


def internal_triggers(instance: Any) -> tuple[InternalTrigger, ...]:
    """Every live Trigger aimed at an internal action at the accepted head.

    Read once per accepted generation: the Trigger set only changes when a new
    generation is accepted, so an idle tick never opens the projection.
    """

    coordinate = instance.accepted_coordinate()
    key = str(instance.root)
    with _TRIGGERS_LOCK:
        cached = _TRIGGERS.get(key)
    if cached is not None and cached[0] == coordinate.generation_root:
        return cached[1]
    with instance.bind_accepted_projection(coordinate) as projection:
        identities = projection.typed.connection.execute(
            "SELECT identity FROM triggers WHERE target_kind='action' AND lifecycle='live' "
            "ORDER BY identity"
        ).fetchall()
        triggers = tuple(
            InternalTrigger.of(projection.typed.source(identity)) for (identity,) in identities
        )
    with _TRIGGERS_LOCK:
        _TRIGGERS[key] = (coordinate.generation_root, triggers)
    return triggers


@contextmanager
def _open(instance: Any, *, create: bool = False) -> Iterator[sqlite3.Connection | None]:
    path = journal_path(instance)
    if not create and not path.exists():
        yield None
        return
    if create:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = (
        sqlite3.connect(path, timeout=30)
        if create
        else sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    )
    try:
        if create:
            connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE" if create else "BEGIN")
        (version,) = connection.execute("PRAGMA user_version").fetchone()
        if version == 0 and create:
            if connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                raise ValueError("Unrecognized trigger journal; retain it for operator repair")
            # executescript would commit the writer lock before creating the schema.
            statement = ""
            for line in _SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
        elif version != _VERSION:
            raise ValueError("Unsupported trigger journal; retain it for operator repair")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _instant(value: str) -> datetime:
    instant = parse_datetime(value)
    assert instant is not None
    return instant


def trigger_events(
    instance: Any, *, after: int = 0, action: str | None = None, limit: int = 1024
) -> tuple[TriggerEvent, ...]:
    with _open(instance) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT sequence,action,trigger_id,due_at,fired_at,event FROM events WHERE sequence>? "
            + ("" if action is None else "AND action=? ")
            + "ORDER BY sequence LIMIT ?",
            (after, limit) if action is None else (after, action, limit),
        ).fetchall()
    return tuple(
        TriggerEvent(
            seq,
            fired_action,
            trigger,
            _instant(due),
            _instant(fired),
            None if event is None else TriggerEventReferenceV1.model_validate_json(event),
        )
        for seq, fired_action, trigger, due, fired, event in rows
    )


def latest_sequence(instance: Any, *, action: str) -> int:
    with _open(instance) as connection:
        if connection is None:
            return 0
        return int(
            connection.execute(
                "SELECT coalesce(max(sequence),0) FROM events WHERE action=?", (action,)
            ).fetchone()[0]
        )


def _deadline_name(name: str) -> str:
    if not name or name in INTERNAL_ACTIONS:
        raise ValueError("Deadline needs a nonempty name distinct from Trigger actions")
    return name


def schedule_deadline(instance: Any, name: str, at: datetime) -> None:
    _deadline_name(name)
    with _open(instance, create=True) as connection:
        assert connection is not None
        connection.execute(
            "INSERT INTO deadlines VALUES (?,?) "
            "ON CONFLICT(name) DO UPDATE SET due_at=excluded.due_at",
            (name, format_datetime(at)),
        )


def cancel_deadline(instance: Any, name: str) -> None:
    """Withdraw a replaced queue's bound when its replacement has no deadline."""

    _deadline_name(name)
    with _open(instance) as connection:
        if (
            connection is None
            or connection.execute("SELECT 1 FROM deadlines WHERE name=?", (name,)).fetchone()
            is None
        ):
            return
    with _open(instance, create=True) as connection:
        assert connection is not None
        connection.execute("DELETE FROM deadlines WHERE name=?", (name,))


def _timed(schedule: TriggerScheduleV1) -> bool:
    """Whether a schedule fires on time alone; every kind is named, none assumed."""

    if isinstance(schedule, CadenceScheduleV1):
        return True
    if isinstance(schedule, CaptureLandingScheduleV1 | WindowCloseScheduleV1):
        return False
    raise ValueError(f"unsupported Trigger schedule kind {schedule.kind!r}")


def _timer_due(schedule: TriggerScheduleV1, *, last: datetime | None) -> datetime | None:
    """When a timed schedule is next due after its last fire; None when it never fired."""

    if isinstance(schedule, CadenceScheduleV1):
        return cadence_due(timedelta(seconds=schedule.interval_seconds), last=last)
    raise ValueError(f"unsupported timed Trigger schedule kind {schedule.kind!r}")


def _due_timers(
    connection: sqlite3.Connection | None,
    *,
    now: datetime,
    triggers: Sequence[InternalTrigger],
) -> list[_Fire]:
    """Every timer and deadline due at `now`."""

    fires: list[_Fire] = []
    last = (
        {}
        if connection is None
        else dict(connection.execute("SELECT trigger_id,last_fired_at FROM cadences").fetchall())
    )
    for item in triggers:
        if not _timed(item.schedule):
            continue
        previous = last.get(item.trigger)
        due = _timer_due(item.schedule, last=None if previous is None else _instant(previous))
        if due is None or due <= now:
            fires.append(_Fire(item.action, item.trigger, now if due is None else due))
    if connection is not None:
        fires.extend(
            _Fire(name, None, _instant(due))
            for name, due in connection.execute(
                "SELECT name,due_at FROM deadlines WHERE due_at<=?", (format_datetime(now),)
            ).fetchall()
        )
    return fires


def _schedule_key(schedule: TriggerScheduleV1) -> str:
    return canonical_bytes(schedule.model_dump(mode="json")).decode()


def _capture_positions(instance: Any) -> dict[str, Any]:
    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    journal, _ = _journal(instance)
    return journal.index.positions(_stream(instance), event_kind="produced_capture")


def _watched_due(
    connection: sqlite3.Connection | None,
    instance: Any,
    *,
    now: datetime,
    triggers: Sequence[InternalTrigger],
) -> bool:
    """Whether any Capture-driven or window Trigger has something to read or fire."""

    watched = [item for item in triggers if not _timed(item.schedule)]
    if not watched:
        return False
    if connection is None:
        return True
    positions: dict[str, Any] | None = None
    for item in watched:
        if schedule_capture_selector(item.schedule) is None:
            assert isinstance(item.schedule, WindowCloseScheduleV1)
            window = bind_observation_window(item.schedule.window)
            if window.ends_at <= now and not _fired(connection, item, _window_key(window.ends_at)):
                return True
            continue
        row = connection.execute(
            "SELECT schedule,position FROM scans WHERE trigger_id=?", (item.trigger,)
        ).fetchone()
        if row is None or row[0] != _schedule_key(item.schedule):
            return True
        if positions is None:
            positions = _capture_positions(instance)
        if json.loads(row[1]) != positions:
            return True
        if connection.execute(
            "SELECT 1 FROM windows WHERE trigger_id=? AND ends_at<=?",
            (item.trigger, format_datetime(now)),
        ).fetchone():
            return True
    return False


def _window_key(ends_at: datetime) -> str:
    return f"window:{format_datetime(ends_at)}"


def _fired(connection: sqlite3.Connection, item: InternalTrigger, occurrence: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM events WHERE trigger_id=? AND occurrence=?", (item.trigger, occurrence)
        ).fetchone()
        is not None
    )


def _watched_fires(
    connection: sqlite3.Connection, instance: Any, *, now: datetime, item: InternalTrigger
) -> list[_Fire]:
    """Read one Capture-driven or window Trigger forward and return what it fires now."""

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    schedule = item.schedule
    selector = schedule_capture_selector(schedule)
    if selector is None:
        assert isinstance(schedule, WindowCloseScheduleV1)
        window = bind_observation_window(schedule.window)
        key = _window_key(window.ends_at)
        if window.ends_at <= now and not _fired(connection, item, key):
            return [_Fire(item.action, item.trigger, window.ends_at, occurrence=key)]
        return []
    positions = _capture_positions(instance)
    row = connection.execute(
        "SELECT schedule,position FROM scans WHERE trigger_id=?", (item.trigger,)
    ).fetchone()
    key = _schedule_key(schedule)
    if row is None or row[0] != key:
        # First sight of this schedule: read forward from here, never back-fill.
        connection.execute("DELETE FROM windows WHERE trigger_id=?", (item.trigger,))
        connection.execute(
            "INSERT OR REPLACE INTO scans VALUES (?,?,?)",
            (item.trigger, key, json.dumps(positions, sort_keys=True)),
        )
        return []
    after = json.loads(row[1])
    fires: list[_Fire] = []
    if after != positions:
        journal, _ = _journal(instance)
        cursor = None
        complete = True
        try:
            while True:
                records, cursor, page_complete = journal.index.captures(
                    _stream(instance),
                    bodies=instance.body_store(),
                    contract_digest=selector.capture_contract_digest,
                    since=None,
                    until=now,
                    limit=256,
                    cursor=cursor,
                    after=after,
                    through=positions,
                )
                complete = complete and page_complete
                for stored in records:
                    record, digest = stored.record, stored.record_digest
                    assert digest is not None
                    event = TriggerEventReferenceV1(
                        run_id=record.run_id or "",
                        partition_id=record.partition_id,
                        sequence=record.sequence,
                        record_digest=digest,
                    )
                    occurrence = "capture:" + digest
                    if isinstance(schedule, CaptureLandingScheduleV1):
                        fires.append(
                            _Fire(item.action, item.trigger, record.recorded_at, event, occurrence)
                        )
                    else:
                        assert isinstance(schedule, WindowCloseScheduleV1)
                        window = bind_observation_window(
                            schedule.window, event=event, event_time=record.recorded_at
                        )
                        connection.execute(
                            "INSERT OR IGNORE INTO windows VALUES (?,?,?,?)",
                            (
                                item.trigger,
                                occurrence,
                                format_datetime(window.ends_at),
                                event.model_dump_json(),
                            ),
                        )
                if cursor is None:
                    break
        except PlaybillError:
            # The Capture index was rebuilt under this reader: start afresh.
            complete = True
            fires = []
        if complete:
            connection.execute(
                "UPDATE scans SET position=? WHERE trigger_id=?",
                (json.dumps(positions, sort_keys=True), item.trigger),
            )
        else:
            # The index is still catching up; read this range again next time.
            fires = []
    for occurrence, ends_at, event_json in connection.execute(
        "SELECT occurrence,ends_at,event FROM windows WHERE trigger_id=? AND ends_at<=? "
        "ORDER BY ends_at,occurrence",
        (item.trigger, format_datetime(now)),
    ).fetchall():
        fires.append(
            _Fire(
                item.action,
                item.trigger,
                _instant(ends_at),
                TriggerEventReferenceV1.model_validate_json(event_json),
                occurrence,
            )
        )
    connection.execute(
        "DELETE FROM windows WHERE trigger_id=? AND ends_at<=?",
        (item.trigger, format_datetime(now)),
    )
    return [fire for fire in fires if not _fired(connection, item, fire.occurrence or "")]


def evaluate_triggers(
    instance: Any, *, now: datetime, triggers: Sequence[InternalTrigger] | None = None
) -> tuple[TriggerEvent, ...]:
    """Fire each due timer once, and each Capture-driven Trigger once per event.

    A timer restarts from this tick after downtime. ``triggers`` defaults to
    the live internal Triggers at the instance's accepted head.
    """

    if triggers is None:
        triggers = internal_triggers(instance)
    with _open(instance) as connection:
        if not _due_timers(connection, now=now, triggers=triggers) and not _watched_due(
            connection, instance, now=now, triggers=triggers
        ):
            return ()
    fired: list[TriggerEvent] = []
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        fires = _due_timers(connection, now=now, triggers=triggers)
        for item in triggers:
            if not _timed(item.schedule):
                fires.extend(_watched_fires(connection, instance, now=now, item=item))
        for fire in sorted(
            fires,
            key=lambda item: (item.due, item.action, item.trigger or "", item.occurrence or ""),
        ):
            if fire.trigger is None:
                connection.execute("DELETE FROM deadlines WHERE name=?", (fire.action,))
            elif fire.occurrence is None:
                connection.execute(
                    "INSERT INTO cadences VALUES (?,?) "
                    "ON CONFLICT(trigger_id) DO UPDATE SET last_fired_at=excluded.last_fired_at",
                    (fire.trigger, format_datetime(now)),
                )
            cursor = connection.execute(
                "INSERT INTO events(action,trigger_id,due_at,fired_at,event,occurrence) "
                "VALUES (?,?,?,?,?,?)",
                (
                    fire.action,
                    fire.trigger,
                    format_datetime(fire.due),
                    format_datetime(now),
                    None if fire.event is None else fire.event.model_dump_json(),
                    fire.occurrence,
                ),
            )
            assert cursor.lastrowid is not None
            fired.append(
                TriggerEvent(cursor.lastrowid, fire.action, fire.trigger, fire.due, now, fire.event)
            )
    return tuple(fired)
