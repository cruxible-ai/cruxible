"""A retained trigger log makes internal fires resumable across daemon and worker restarts.

Which internal Triggers exist is governed: every one is a live Trigger artifact
aimed at an internal action, read from the instance's accepted state. In v1 an
internal action takes a time schedule (cadence or cron) only; the Trigger law
refuses any other. The log records which Trigger fired and which action it
fired, so a worker follows an action however many Triggers schedule it.

No Trigger fires retroactively. A timer fires each of its instants once, all of
them after the acceptance of its Trigger version (a cadence first at acceptance
plus one interval, a successor schedule from its own acceptance), and none
before the daemon began listening: instants that pass while no daemon runs are
skipped, never fired late. A retired Trigger stops.

Deadlines are not Triggers. A worker schedules its own (``next.expire``) as an
operational refresh signal; they fire into the same log, under their action and
with no Trigger.

Firing and advancing a timer commit together. Unlike findings projections this
store is never rebuilt or discarded: it is the operational authority for which
fires happened. Readers seek by sequence and may retain their own resume cursor.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import (
    INTERNAL_ACTIONS,
    AcceptedTriggerV1,
    TriggerScheduleV1,
    TriggerV1,
    schedule_is_timed,
    trigger_digest,
    trigger_path,
)
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.triggers.cadence import timer_instants

_SCHEMA = """
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL, trigger_id TEXT, due_at TEXT NOT NULL, fired_at TEXT NOT NULL,
 occurrence TEXT
) STRICT;
CREATE INDEX events_by_action ON events(action,sequence);
CREATE UNIQUE INDEX events_once ON events(trigger_id,occurrence) WHERE occurrence IS NOT NULL;
CREATE TABLE timers (trigger_id TEXT PRIMARY KEY, covered_until TEXT NOT NULL) STRICT;
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
    """One live Trigger's schedule toward one internal action, from its acceptance."""

    trigger: str
    action: str
    schedule: TriggerScheduleV1
    #: When this Trigger version was accepted: it fires nothing before then.
    accepted_at: datetime

    @classmethod
    def of(cls, trigger: TriggerV1, *, accepted_at: datetime) -> InternalTrigger:
        action = trigger.action
        if action is None:
            raise ValueError("an internal Trigger fires an internal action")
        return cls(trigger.identity.qualified, action, trigger.schedule, accepted_at)


@dataclass(frozen=True)
class TriggerEvent:
    sequence: int
    action: str
    #: The Trigger that fired, or None for a worker's own deadline.
    trigger: str | None
    due_at: datetime
    fired_at: datetime


@dataclass(frozen=True)
class _Fire:
    action: str
    trigger: str | None
    due: datetime
    occurrence: str | None = None


def journal_path(instance: Any) -> Path:
    return Path(instance.root) / str(instance.descriptor.storage.exhaust) / "triggers.sqlite3"


# Per instance root: the accepted generation last read and its live internal Triggers.
_TRIGGERS: OrderedDict[str, tuple[str, tuple[InternalTrigger, ...]]] = OrderedDict()
_TRIGGERS_CAPACITY = 64


def internal_triggers(instance: Any) -> tuple[InternalTrigger, ...]:
    """Every live Trigger aimed at an internal action at the accepted head.

    Read once per accepted generation: the Trigger set only changes when a new
    generation is accepted, so an idle tick never opens the projection.
    """

    coordinate = instance.accepted_coordinate()
    key = str(instance.root)
    cached = memo_get(_TRIGGERS, key)
    if cached is not None and cached[0] == coordinate.generation_root:
        return cached[1]
    from cruxible_core.service.procedures.procedure_runs import trigger_accepted_at

    with instance.bind_accepted_projection(coordinate) as projection:
        identities = projection.typed.connection.execute(
            "SELECT identity FROM triggers WHERE target_kind='action' AND lifecycle='live' "
            "ORDER BY identity"
        ).fetchall()
        accepted = [projection.typed.source(identity) for (identity,) in identities]
    triggers = tuple(
        InternalTrigger.of(
            trigger,
            accepted_at=trigger_accepted_at(
                instance,
                AcceptedTriggerV1(
                    path=trigger_path(trigger.identity.name),
                    trigger=trigger,
                    artifact_digest=trigger_digest(trigger).tagged,
                ),
            ),
        )
        for trigger in accepted
    )
    memo_put(_TRIGGERS, key, (coordinate.generation_root, triggers), capacity=_TRIGGERS_CAPACITY)
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


def _due_timers(
    connection: sqlite3.Connection | None,
    *,
    now: datetime,
    listening_since: datetime,
    triggers: Sequence[InternalTrigger],
) -> list[_Fire]:
    """Every timer instant and deadline due at `now`.

    A timer fires each of its instants once, none before its acceptance and none
    before this evaluator started listening: instants that passed while no
    daemon was running are skipped, never fired late as a catch-up.
    """

    fires: list[_Fire] = []
    covered = (
        {}
        if connection is None
        else dict(connection.execute("SELECT trigger_id,covered_until FROM timers").fetchall())
    )
    for item in triggers:
        if not schedule_is_timed(item.schedule):
            # The Trigger law accepts only time schedules for internal actions.
            raise ValueError(f"{item.trigger} fires an internal action on a non-time schedule")
        after = max(item.accepted_at, listening_since - timedelta(microseconds=1))
        if item.trigger in covered:
            after = max(after, _instant(covered[item.trigger]))
        fires.extend(
            _Fire(item.action, item.trigger, instant, occurrence=f"tick:{format_datetime(instant)}")
            for instant in timer_instants(
                item.schedule, accepted_at=item.accepted_at, after=after, through=now
            )
        )
    if connection is not None:
        fires.extend(
            _Fire(name, None, _instant(due))
            for name, due in connection.execute(
                "SELECT name,due_at FROM deadlines WHERE due_at<=?", (format_datetime(now),)
            ).fetchall()
        )
    return fires


def _fired(connection: sqlite3.Connection, trigger: str, occurrence: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM events WHERE trigger_id=? AND occurrence=?", (trigger, occurrence)
        ).fetchone()
        is not None
    )


def _record(
    connection: sqlite3.Connection, fires: list[_Fire], *, now: datetime
) -> list[TriggerEvent]:
    fired: list[TriggerEvent] = []
    for fire in sorted(
        fires, key=lambda item: (item.due, item.action, item.trigger or "", item.occurrence or "")
    ):
        if fire.trigger is None:
            connection.execute("DELETE FROM deadlines WHERE name=?", (fire.action,))
        elif _fired(connection, fire.trigger, fire.occurrence or ""):
            continue
        cursor = connection.execute(
            "INSERT INTO events(action,trigger_id,due_at,fired_at,occurrence) VALUES (?,?,?,?,?)",
            (
                fire.action,
                fire.trigger,
                format_datetime(fire.due),
                format_datetime(now),
                fire.occurrence,
            ),
        )
        assert cursor.lastrowid is not None
        fired.append(TriggerEvent(cursor.lastrowid, fire.action, fire.trigger, fire.due, now))
    return fired


def evaluate_triggers(
    instance: Any,
    *,
    now: datetime,
    listening_since: datetime,
    triggers: Sequence[InternalTrigger] | None = None,
) -> tuple[TriggerEvent, ...]:
    """Fire each timer instant and due deadline once.

    ``listening_since`` is when this evaluator (the daemon's consumer runner)
    began firing: timer instants before it passed while nothing was listening
    and are skipped. ``triggers`` defaults to the live internal Triggers at the
    instance's accepted head.
    """

    if triggers is None:
        triggers = internal_triggers(instance)
    with _open(instance) as connection:
        if not _due_timers(connection, now=now, listening_since=listening_since, triggers=triggers):
            return ()
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        fires = _due_timers(connection, now=now, listening_since=listening_since, triggers=triggers)
        for item in triggers:
            connection.execute(
                "INSERT INTO timers VALUES (?,?) "
                "ON CONFLICT(trigger_id) DO UPDATE SET covered_until=excluded.covered_until",
                (item.trigger, format_datetime(now)),
            )
        return tuple(_record(connection, fires, now=now))
