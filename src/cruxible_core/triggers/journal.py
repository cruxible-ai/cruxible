"""A retained trigger log makes timer fires resumable across daemon and worker restarts.

Which internal cadences exist is governed: every one is a live Trigger artifact
aimed at an internal action, read from the instance's accepted state. The log
records which Trigger fired and which action it fired, so a worker follows an
action however many Triggers schedule it. Cadence state is kept per Trigger and
follows the accepted Trigger set: a new Trigger fires on the next tick, a
changed interval counts from its last fire, and a retired Trigger stops.

Deadlines are not Triggers. A worker schedules its own (``next.expire``) as an
operational refresh signal; they fire into the same log, under their action and
with no Trigger.

Firing and advancing a timer commit together. Unlike findings projections this
store is never rebuilt or discarded: it is the operational authority for which
fires happened. Readers seek by sequence and may retain their own resume cursor.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import INTERNAL_ACTIONS
from cruxible_core.triggers.cadence import cadence_due

_SCHEMA = """
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL, trigger_id TEXT, due_at TEXT NOT NULL, fired_at TEXT NOT NULL
) STRICT;
CREATE INDEX events_by_action ON events(action,sequence);
CREATE TABLE cadences (trigger_id TEXT PRIMARY KEY, last_fired_at TEXT NOT NULL) STRICT;
CREATE TABLE deadlines (name TEXT PRIMARY KEY, due_at TEXT NOT NULL) STRICT;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
PRAGMA user_version=2;
"""
_VERSION = 2


@dataclass(frozen=True)
class TriggerCadence:
    """One live Trigger's cadence toward one internal action."""

    trigger: str
    action: str
    interval: timedelta


@dataclass(frozen=True)
class TriggerEvent:
    sequence: int
    action: str
    #: The Trigger that fired, or None for a worker's own deadline.
    trigger: str | None
    due_at: datetime
    fired_at: datetime


def journal_path(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / "triggers.sqlite3"


# Per instance root: the accepted generation last read and its live cadences.
_CADENCES: dict[str, tuple[str, tuple[TriggerCadence, ...]]] = {}
_CADENCES_LOCK = threading.Lock()


def internal_trigger_cadences(instance: Any) -> tuple[TriggerCadence, ...]:
    """Every live Trigger aimed at an internal action at the accepted head.

    Read once per accepted generation: the Trigger set only changes when a new
    generation is accepted, so an idle tick never opens the projection.
    """

    coordinate = instance.accepted_coordinate()
    key = str(instance.root)
    with _CADENCES_LOCK:
        cached = _CADENCES.get(key)
    if cached is not None and cached[0] == coordinate.generation_root:
        return cached[1]
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.typed.connection.execute(
            "SELECT identity,target,interval_seconds FROM triggers WHERE target_kind='action' "
            "AND schedule_kind='cadence' AND lifecycle='live' ORDER BY identity"
        ).fetchall()
    cadences = tuple(
        TriggerCadence(trigger=identity, action=action, interval=timedelta(seconds=interval))
        for identity, action, interval in rows
    )
    with _CADENCES_LOCK:
        _CADENCES[key] = (coordinate.generation_root, cadences)
    return cadences


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
            "SELECT sequence,action,trigger_id,due_at,fired_at FROM events WHERE sequence>? "
            + ("" if action is None else "AND action=? ")
            + "ORDER BY sequence LIMIT ?",
            (after, limit) if action is None else (after, action, limit),
        ).fetchall()
    return tuple(
        TriggerEvent(seq, fired_action, trigger, _instant(due), _instant(fired))
        for seq, fired_action, trigger, due, fired in rows
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


def _due_triggers(
    connection: sqlite3.Connection | None,
    *,
    now: datetime,
    cadences: Sequence[TriggerCadence],
) -> list[tuple[str, str | None, datetime]]:
    """Every (action, Trigger or None, due instant) due at `now`, in due order."""

    pending: list[tuple[str, str | None, datetime]] = []
    last = (
        {}
        if connection is None
        else dict(connection.execute("SELECT trigger_id,last_fired_at FROM cadences").fetchall())
    )
    for cadence in cadences:
        previous = last.get(cadence.trigger)
        due = cadence_due(cadence.interval, last=None if previous is None else _instant(previous))
        if due is None or due <= now:
            pending.append((cadence.action, cadence.trigger, now if due is None else due))
    if connection is not None:
        pending.extend(
            (name, None, _instant(due))
            for name, due in connection.execute(
                "SELECT name,due_at FROM deadlines WHERE due_at<=?", (format_datetime(now),)
            ).fetchall()
        )
    return sorted(pending, key=lambda item: (item[2], item[0], item[1] or ""))


def evaluate_triggers(
    instance: Any, *, now: datetime, cadences: Sequence[TriggerCadence] | None = None
) -> tuple[TriggerEvent, ...]:
    """Fire each due timer once, restarting a cadence from this tick after downtime.

    ``cadences`` defaults to the live internal Triggers at the instance's
    accepted head.
    """

    if cadences is None:
        cadences = internal_trigger_cadences(instance)
    with _open(instance) as connection:
        if not _due_triggers(connection, now=now, cadences=cadences):
            return ()
    fired: list[TriggerEvent] = []
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        for action, trigger, due in _due_triggers(connection, now=now, cadences=cadences):
            if trigger is not None:
                connection.execute(
                    "INSERT INTO cadences VALUES (?,?) "
                    "ON CONFLICT(trigger_id) DO UPDATE SET last_fired_at=excluded.last_fired_at",
                    (trigger, format_datetime(now)),
                )
            else:
                connection.execute("DELETE FROM deadlines WHERE name=?", (action,))
            cursor = connection.execute(
                "INSERT INTO events(action,trigger_id,due_at,fired_at) VALUES (?,?,?,?)",
                (action, trigger, format_datetime(due), format_datetime(now)),
            )
            assert cursor.lastrowid is not None
            fired.append(TriggerEvent(cursor.lastrowid, action, trigger, due, now))
    return tuple(fired)
