"""A retained trigger log makes internal fires resumable across daemon and worker restarts.

Which internal Triggers exist is governed: every one is a live Trigger artifact
aimed at an internal action, read from the instance's accepted state. In v1 an
internal action takes a time schedule (cadence or cron) or generation acceptance; the Trigger law
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
    AcceptedTrigger,
    GenerationAcceptedSchedule,
    Trigger,
    TriggerSchedule,
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
CREATE TABLE generations (trigger_id TEXT PRIMARY KEY, version TEXT NOT NULL,
 listening_since TEXT NOT NULL, covered INTEGER NOT NULL) STRICT;
PRAGMA user_version=3;
"""
_VERSION = 3


@dataclass(frozen=True)
class InternalTrigger:
    """One live Trigger's schedule toward one internal action, from its acceptance."""

    trigger: str
    action: str
    schedule: TriggerSchedule
    #: When this Trigger version was accepted: it fires nothing before then.
    accepted_at: datetime
    accepted_generation: int | None = None
    version: str = ""

    @classmethod
    def of(
        cls, trigger: Trigger, *, accepted_at: datetime, accepted_generation: int | None = None
    ) -> InternalTrigger:
        action = trigger.action
        if action is None:
            raise ValueError("an internal Trigger fires an internal action")
        return cls(
            trigger.identity.qualified,
            action,
            trigger.schedule,
            accepted_at,
            accepted_generation,
            trigger_digest(trigger).tagged,
        )


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
            accepted_generation=trigger_generation(instance, trigger),
            accepted_at=trigger_accepted_at(
                instance,
                AcceptedTrigger(
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
        elif version == 2:
            if create:
                connection.execute(
                    "CREATE TABLE generations (trigger_id TEXT PRIMARY KEY, "
                    "version TEXT NOT NULL, listening_since TEXT NOT NULL, "
                    "covered INTEGER NOT NULL) STRICT"
                )
                connection.execute("PRAGMA user_version=3")
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
        if isinstance(item.schedule, GenerationAcceptedSchedule):
            continue
        if not schedule_is_timed(item.schedule):
            # Capture schedules remain outside the internal-action journal.
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


def trigger_generation(instance: Any, trigger: Trigger) -> int:
    """The accepted sequence of this exact Trigger version."""

    with instance.accepted_history_reader() as history:
        occurrence = history.artifact(
            trigger_digest(trigger).tagged, identity=trigger.identity.qualified
        )
        if occurrence is None:
            raise ValueError("Trigger version has no accepted occurrence")
        return int(occurrence.occurrence_sequence)


def generation_at(instance: Any, instant: datetime) -> int:
    """Bisect a matching range, scanning backwards when a bracket is out of order.

    Candidate timestamp validation checks canonical spelling, not parent order
    (cruxible_client.contracts.candidates.validate_candidate_timestamp). A
    reversed bracket therefore needs the latest-sequence linear lookup, never
    an assertion on the tick path. Daemon listeners normally supply their exact
    starting sequence without this time lookup.
    """

    with instance.accepted_history_reader() as history:
        lower, upper = 0, int(history.sequence)

        def scan_backwards() -> int:
            for sequence in range(int(history.sequence), -1, -1):
                if (
                    instance.accepted_evaluation_time(history.generation(sequence).git_oid)
                    <= instant
                ):
                    return sequence
            return 0

        lower_time = instance.accepted_evaluation_time(history.generation(lower).git_oid)
        upper_time = instance.accepted_evaluation_time(history.generation(upper).git_oid)
        if lower_time > upper_time:
            return scan_backwards()
        if instant < lower_time:
            return 0
        if instant >= upper_time:
            return upper
        while lower + 1 < upper:
            middle = (lower + upper) // 2
            middle_time = instance.accepted_evaluation_time(history.generation(middle).git_oid)
            if not lower_time <= middle_time <= upper_time:
                return scan_backwards()
            if middle_time <= instant:
                lower, lower_time = middle, middle_time
            else:
                upper, upper_time = middle, middle_time
        return lower


def _generation_work(
    connection: sqlite3.Connection | None,
    instance: Any,
    *,
    triggers: Sequence[InternalTrigger],
    listening_since: datetime,
    listening_generation: int | None,
) -> tuple[list[_Fire], list[tuple[Any, ...]]]:
    items = [item for item in triggers if isinstance(item.schedule, GenerationAcceptedSchedule)]
    if not items:
        return [], []
    with instance.accepted_history_reader() as history:
        head = history.sequence
        head_time = instance.accepted_evaluation_time(history.generation(head).git_oid)
    rows = (
        {}
        if connection is None or connection.execute("PRAGMA user_version").fetchone()[0] == 2
        else {
            name: (version, listener, covered)
            for name, version, listener, covered in connection.execute("SELECT * FROM generations")
        }
    )
    fires, updates = [], []
    listener = format_datetime(listening_since)
    for item in items:
        version = item.version or format_datetime(item.accepted_at)
        previous = rows.get(item.trigger)
        if previous is None or previous[:2] != (version, listener):
            covered = max(
                item.accepted_generation
                if item.accepted_generation is not None
                else generation_at(instance, item.accepted_at),
                listening_generation
                if listening_generation is not None
                else generation_at(instance, listening_since),
            )
        else:
            covered = previous[2]
        if head > covered:
            fires.append(
                _Fire(item.action, item.trigger, head_time, f"generation:{version}:{head}")
            )
        if previous != (version, listener, head):
            updates.append((item.trigger, version, listener, head))
    return fires, updates


def evaluate_triggers(
    instance: Any,
    *,
    now: datetime,
    listening_since: datetime,
    triggers: Sequence[InternalTrigger] | None = None,
    listening_generation: int | None = None,
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
        generation_fires, generation_updates = _generation_work(
            connection,
            instance,
            triggers=triggers,
            listening_since=listening_since,
            listening_generation=listening_generation,
        )
        if not (
            _due_timers(connection, now=now, listening_since=listening_since, triggers=triggers)
            or generation_fires
            or generation_updates
        ):
            return ()
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        fires = _due_timers(connection, now=now, listening_since=listening_since, triggers=triggers)
        generation_fires, generation_updates = _generation_work(
            connection,
            instance,
            triggers=triggers,
            listening_since=listening_since,
            listening_generation=listening_generation,
        )
        fires.extend(generation_fires)
        connection.executemany(
            "INSERT INTO generations VALUES (?,?,?,?) ON CONFLICT(trigger_id) DO UPDATE SET "
            "version=excluded.version,listening_since=excluded.listening_since,covered=excluded.covered",
            generation_updates,
        )
        for item in triggers:
            if isinstance(item.schedule, GenerationAcceptedSchedule):
                continue
            connection.execute(
                "INSERT INTO timers VALUES (?,?) "
                "ON CONFLICT(trigger_id) DO UPDATE SET covered_until=excluded.covered_until",
                (item.trigger, format_datetime(now)),
            )
        return tuple(_record(connection, fires, now=now))
