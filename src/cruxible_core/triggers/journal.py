"""A retained trigger log makes timer fires resumable across daemon and worker restarts.

Firing and advancing a timer commit together. Unlike findings projections this
store is never rebuilt or discarded: it is the operational authority for which
fires happened. Readers seek by sequence and may retain their own resume cursor.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.triggers.cadence import cadence_due
from cruxible_core.triggers.config import TriggerOperationalConfigV1

_SCHEMA = """
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL, due_at TEXT NOT NULL, fired_at TEXT NOT NULL
) STRICT;
CREATE INDEX events_by_name ON events(name,sequence);
CREATE TABLE cadences (name TEXT PRIMARY KEY, last_fired_at TEXT NOT NULL) STRICT;
CREATE TABLE deadlines (name TEXT PRIMARY KEY, due_at TEXT NOT NULL) STRICT;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
 BEGIN SELECT RAISE(ABORT,'trigger events are append-only'); END;
PRAGMA user_version=1;
"""


@dataclass(frozen=True)
class TriggerEvent:
    sequence: int
    name: str
    due_at: datetime
    fired_at: datetime


def journal_path(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / "triggers.sqlite3"


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
        elif version != 1:
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
    instance: Any, *, after: int = 0, name: str | None = None, limit: int = 1024
) -> tuple[TriggerEvent, ...]:
    with _open(instance) as connection:
        if connection is None:
            return ()
        rows = connection.execute(
            "SELECT sequence,name,due_at,fired_at FROM events WHERE sequence>? "
            + ("" if name is None else "AND name=? ")
            + "ORDER BY sequence LIMIT ?",
            (after, limit) if name is None else (after, name, limit),
        ).fetchall()
    return tuple(
        TriggerEvent(seq, key, _instant(due), _instant(fired)) for seq, key, due, fired in rows
    )


def latest_sequence(instance: Any, *, name: str) -> int:
    with _open(instance) as connection:
        if connection is None:
            return 0
        return int(
            connection.execute(
                "SELECT coalesce(max(sequence),0) FROM events WHERE name=?", (name,)
            ).fetchone()[0]
        )


def schedule_deadline(instance: Any, name: str, at: datetime) -> None:
    if not name or name in TriggerOperationalConfigV1().cadences():
        raise ValueError("Deadline needs a nonempty name distinct from built-in cadences")
    with _open(instance, create=True) as connection:
        assert connection is not None
        connection.execute(
            "INSERT INTO deadlines VALUES (?,?) "
            "ON CONFLICT(name) DO UPDATE SET due_at=excluded.due_at",
            (name, format_datetime(at)),
        )


def _due_triggers(
    connection: sqlite3.Connection | None, *, now: datetime, config: TriggerOperationalConfigV1
) -> list[tuple[str, datetime]]:
    pending: list[tuple[str, datetime]] = []
    last = (
        {}
        if connection is None
        else dict(connection.execute("SELECT name,last_fired_at FROM cadences").fetchall())
    )
    for name, interval in config.cadences().items():
        due = cadence_due(interval, last=None if name not in last else _instant(last[name]))
        if due is None or due <= now:
            pending.append((name, now if due is None else due))
    if connection is not None:
        pending.extend(
            (name, _instant(due))
            for name, due in connection.execute(
                "SELECT name,due_at FROM deadlines WHERE due_at<=?", (format_datetime(now),)
            ).fetchall()
        )
    return sorted(pending, key=lambda item: (item[1], item[0]))


def evaluate_triggers(
    instance: Any, *, now: datetime, config: TriggerOperationalConfigV1
) -> tuple[TriggerEvent, ...]:
    """Fire each due timer once, restarting a cadence from this tick after downtime."""

    with _open(instance) as connection:
        if not _due_triggers(connection, now=now, config=config):
            return ()
    fired: list[TriggerEvent] = []
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        for name, due in _due_triggers(connection, now=now, config=config):
            if name in config.cadences():
                connection.execute(
                    "INSERT INTO cadences VALUES (?,?) "
                    "ON CONFLICT(name) DO UPDATE SET last_fired_at=excluded.last_fired_at",
                    (name, format_datetime(now)),
                )
            else:
                connection.execute("DELETE FROM deadlines WHERE name=?", (name,))
            cursor = connection.execute(
                "INSERT INTO events(name,due_at,fired_at) VALUES (?,?,?)",
                (name, format_datetime(due), format_datetime(now)),
            )
            assert cursor.lastrowid is not None
            fired.append(TriggerEvent(cursor.lastrowid, name, due, now))
    return tuple(fired)
