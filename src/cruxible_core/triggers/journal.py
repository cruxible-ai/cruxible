"""A retained trigger log makes internal fires resumable across daemon and worker restarts.

Which internal Triggers exist is governed: every one is a live Trigger artifact
aimed at an internal action, read from the instance's accepted state. The log
records which Trigger fired, which action it fired and, for a schedule that
fires on a Capture, the exact event, so a worker follows an action however many
Triggers schedule it and may use or ignore the event.

No Trigger fires retroactively. A timer fires each of its instants once, all of
them after the acceptance of its Trigger version (a cadence first at acceptance
plus one interval, a successor schedule from its own acceptance), and none
before the daemon began listening: instants that pass while no daemon runs are
skipped, never fired late. A retired Trigger stops.

A Trigger that fires on Captures reads the procedure journal's Capture index
forward from its version's acceptance, never back-filling what landed before
it. Its checkpoint is the last record it consumed, named by the record's own
``(recorded_at, partition_id, sequence)``, never by the index's ordinals: an
index rebuilt under it is read again from that record, and each event still
fires once, however late it is read. A read that fails or is incomplete never
advances the checkpoint; it is kept for health instead. A backlog is read in
bounded batches, one transaction each, so it never holds the writer lock for
long. A changed schedule starts reading afresh from its own acceptance.

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
    AcceptedTriggerV1,
    CaptureLandingScheduleV1,
    TriggerScheduleV1,
    TriggerV1,
    WindowCloseScheduleV1,
    schedule_capture_selector,
    schedule_is_timed,
    trigger_digest,
    trigger_path,
)
from cruxible_core.triggers.cadence import timer_instants

_SCHEMA = """
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL, trigger_id TEXT, due_at TEXT NOT NULL, fired_at TEXT NOT NULL,
 event TEXT, occurrence TEXT
) STRICT;
CREATE INDEX events_by_action ON events(action,sequence);
CREATE UNIQUE INDEX events_once ON events(trigger_id,occurrence) WHERE occurrence IS NOT NULL;
CREATE TABLE timers (trigger_id TEXT PRIMARY KEY, covered_until TEXT NOT NULL) STRICT;
CREATE TABLE scans (
 trigger_id TEXT PRIMARY KEY, schedule TEXT NOT NULL, since TEXT NOT NULL, checkpoint TEXT,
 seen TEXT, error TEXT
) STRICT;
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
            continue
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


def _schedule_key(schedule: TriggerScheduleV1) -> str:
    return canonical_bytes(schedule.model_dump(mode="json")).decode()


#: Capture records one scan transaction consumes, and transactions one pass runs.
_SCAN_BATCH = 256
_SCAN_BATCHES_PER_PASS = 16


def _capture_hint(instance: Any) -> str:
    """The Capture index's live position: only a hint that something may have landed.

    Never a checkpoint: a rebuilt index changes it, which merely prompts a read
    from the stable checkpoint.
    """

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    journal, _ = _journal(instance)
    position = journal.index.positions(_stream(instance), event_kind="produced_capture")
    return json.dumps(position, sort_keys=True)


def _fixed_window_fire(
    item: InternalTrigger, *, now: datetime, listening_since: datetime
) -> _Fire | None:
    """A fixed window's close, if it is an instant this evaluator fires.

    Like a timer instant: never one before the Trigger's acceptance, and never
    one that passed while nothing was listening.
    """

    assert isinstance(item.schedule, WindowCloseScheduleV1)
    ends_at = bind_observation_window(item.schedule.window).ends_at
    if item.accepted_at < ends_at <= now and ends_at >= listening_since:
        return _Fire(item.action, item.trigger, ends_at, occurrence=_window_key(ends_at))
    return None


def _watched_due(
    connection: sqlite3.Connection | None,
    instance: Any,
    *,
    now: datetime,
    listening_since: datetime,
    triggers: Sequence[InternalTrigger],
) -> bool:
    """Whether any Capture-driven or window Trigger has something to read or fire."""

    watched = [item for item in triggers if not schedule_is_timed(item.schedule)]
    if not watched:
        return False
    if connection is None:
        return True
    hint: str | None = None
    for item in watched:
        if schedule_capture_selector(item.schedule) is None:
            fire = _fixed_window_fire(item, now=now, listening_since=listening_since)
            if fire is not None and not _fired(connection, item.trigger, fire.occurrence or ""):
                return True
            continue
        row = connection.execute(
            "SELECT schedule,seen,error FROM scans WHERE trigger_id=?", (item.trigger,)
        ).fetchone()
        if row is None or row[0] != _schedule_key(item.schedule) or row[2] is not None:
            return True
        if hint is None:
            hint = _capture_hint(instance)
        if row[1] != hint:
            return True
        if connection.execute(
            "SELECT 1 FROM windows WHERE trigger_id=? AND ends_at<=?",
            (item.trigger, format_datetime(now)),
        ).fetchone():
            return True
    return False


def _window_key(ends_at: datetime) -> str:
    return f"window:{format_datetime(ends_at)}"


def _fired(connection: sqlite3.Connection, trigger: str, occurrence: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM events WHERE trigger_id=? AND occurrence=?", (trigger, occurrence)
        ).fetchone()
        is not None
    )


def _scan_batch(
    connection: sqlite3.Connection, instance: Any, *, now: datetime, item: InternalTrigger
) -> tuple[list[_Fire], bool]:
    """Consume one bounded batch of Captures for one Trigger; whether more remain.

    The batch's fires and window anchors and the advanced checkpoint commit
    together. A failed or incomplete read records why and consumes nothing.
    """

    from cruxible_core.service.procedures.procedure_runs import _journal, _stream

    schedule = item.schedule
    selector = schedule_capture_selector(schedule)
    assert selector is not None
    key = _schedule_key(schedule)
    row = connection.execute(
        "SELECT schedule,since,checkpoint FROM scans WHERE trigger_id=?", (item.trigger,)
    ).fetchone()
    if row is None or row[0] != key:
        # A new schedule reads forward from its own acceptance, never before.
        connection.execute("DELETE FROM windows WHERE trigger_id=?", (item.trigger,))
        connection.execute(
            "INSERT OR REPLACE INTO scans VALUES (?,?,?,NULL,NULL,NULL)",
            (item.trigger, key, format_datetime(item.accepted_at)),
        )
        since, checkpoint = item.accepted_at, None
    else:
        since = _instant(row[1])
        checkpoint = None if row[2] is None else tuple(json.loads(row[2]))
    hint = _capture_hint(instance)
    journal, _ = _journal(instance)
    try:
        records, more, complete = journal.index.captures(
            _stream(instance),
            bodies=instance.body_store(),
            contract_digest=selector.capture_contract_digest,
            since=since,
            until=now + timedelta(microseconds=1),
            limit=_SCAN_BATCH,
            cursor=checkpoint,
            ascending=True,
        )
    except (PlaybillError, OSError, ValueError) as exc:
        connection.execute(
            "UPDATE scans SET error=? WHERE trigger_id=?",
            (f"capture read failed: {exc}", item.trigger),
        )
        return [], False
    if not complete:
        connection.execute(
            "UPDATE scans SET error=? WHERE trigger_id=?",
            ("capture index is catching up; this range is read again", item.trigger),
        )
        return [], False
    fires: list[_Fire] = []
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
            fires.append(_Fire(item.action, item.trigger, record.recorded_at, event, occurrence))
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
    last = records[-1].record if records else None
    connection.execute(
        "UPDATE scans SET checkpoint=coalesce(?,checkpoint),seen=?,error=NULL WHERE trigger_id=?",
        (
            None
            if last is None
            else json.dumps([format_datetime(last.recorded_at), last.partition_id, last.sequence]),
            None if more else hint,
            item.trigger,
        ),
    )
    return [
        fire for fire in fires if not _fired(connection, item.trigger, fire.occurrence or "")
    ], (more is not None)


def _closed_windows(
    connection: sqlite3.Connection, *, now: datetime, item: InternalTrigger
) -> list[_Fire]:
    """Capture-anchored windows of one Trigger that have closed, each delivered once."""

    fires = [
        _Fire(
            item.action,
            item.trigger,
            _instant(ends_at),
            TriggerEventReferenceV1.model_validate_json(event_json),
            occurrence,
        )
        for occurrence, ends_at, event_json in connection.execute(
            "SELECT occurrence,ends_at,event FROM windows WHERE trigger_id=? AND ends_at<=? "
            "ORDER BY ends_at,occurrence",
            (item.trigger, format_datetime(now)),
        ).fetchall()
    ]
    connection.execute(
        "DELETE FROM windows WHERE trigger_id=? AND ends_at<=?",
        (item.trigger, format_datetime(now)),
    )
    return [fire for fire in fires if not _fired(connection, item.trigger, fire.occurrence or "")]


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
    return fired


def trigger_read_errors(instance: Any) -> dict[str, str]:
    """Per Trigger, why its last Capture read consumed nothing, while it has not since."""

    with _open(instance) as connection:
        if connection is None:
            return {}
        return dict(
            connection.execute(
                "SELECT trigger_id,error FROM scans WHERE error IS NOT NULL ORDER BY trigger_id"
            ).fetchall()
        )


def evaluate_triggers(
    instance: Any,
    *,
    now: datetime,
    listening_since: datetime,
    triggers: Sequence[InternalTrigger] | None = None,
) -> tuple[TriggerEvent, ...]:
    """Fire each timer instant once, and each Capture-driven Trigger once per event.

    ``listening_since`` is when this evaluator (the daemon's consumer runner)
    began firing: timer instants before it passed while nothing was listening
    and are skipped. ``triggers`` defaults to the live internal Triggers at the
    instance's accepted head.
    """

    if triggers is None:
        triggers = internal_triggers(instance)
    with _open(instance) as connection:
        if not _due_timers(
            connection, now=now, listening_since=listening_since, triggers=triggers
        ) and not _watched_due(
            connection, instance, now=now, listening_since=listening_since, triggers=triggers
        ):
            return ()
    fired: list[TriggerEvent] = []
    # Captures first, each batch its own short transaction, so a large backlog
    # never holds the writer lock and windows it anchors can close this pass.
    for item in triggers:
        if schedule_is_timed(item.schedule) or schedule_capture_selector(item.schedule) is None:
            continue
        for _batch in range(_SCAN_BATCHES_PER_PASS):
            with _open(instance, create=True) as connection:
                assert connection is not None
                fires, more = _scan_batch(connection, instance, now=now, item=item)
                fired.extend(_record(connection, fires, now=now))
            if not more:
                break
    with _open(instance, create=True) as connection:
        assert connection is not None
        # Recheck under the writer lock: another evaluator may have fired, or
        # a deadline may have been replaced since the read-only due check.
        fires = _due_timers(connection, now=now, listening_since=listening_since, triggers=triggers)
        for item in triggers:
            if schedule_is_timed(item.schedule):
                connection.execute(
                    "INSERT INTO timers VALUES (?,?) "
                    "ON CONFLICT(trigger_id) DO UPDATE SET covered_until=excluded.covered_until",
                    (item.trigger, format_datetime(now)),
                )
            elif schedule_capture_selector(item.schedule) is None:
                fire = _fixed_window_fire(item, now=now, listening_since=listening_since)
                fires.extend(() if fire is None else (fire,))
            else:
                fires.extend(_closed_windows(connection, now=now, item=item))
        fired.extend(_record(connection, fires, now=now))
    return tuple(fired)
