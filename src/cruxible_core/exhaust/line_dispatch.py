"""Individual retained dispatch transitions and their rebuildable unresolved set.

The journal owns sessions, coverage, and pending/admitted transitions. SQLite
only locates active sessions and unresolved occurrences; its replay offset is
not a work-completion cursor. Only the dispatch writer emits admission completion.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.line_dispatch import LineEnablement, LineTriggerVersion
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.exhaust.records import (
    LINE_DISPATCH_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
)
from cruxible_core.exhaust.writer import ProcedureExhaustWriter
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.preview_fence import is_previewing

_LOCK = threading.RLock()
_FENCE = "line-dispatch-local-v1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS progress (singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
 session_id TEXT PRIMARY KEY, line_id TEXT NOT NULL, epoch INTEGER NOT NULL,
 active INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS active_session ON sessions(line_id,epoch) WHERE active=1;
CREATE INDEX IF NOT EXISTS sessions_active ON sessions(active);
-- Exact versions ever enabled: next clears upgrade findings with a keyed probe,
-- independent of enablement history size. Built once on projection format change.
CREATE INDEX IF NOT EXISTS sessions_version
 ON sessions(line_id,json_extract(payload,'$.line_artifact_digest'));
CREATE TABLE IF NOT EXISTS pending (
 line_id TEXT NOT NULL, epoch INTEGER NOT NULL, occurrence_id TEXT NOT NULL,
 disposition TEXT NOT NULL, eligible_at TEXT NOT NULL, payload TEXT NOT NULL, run_id TEXT,
 session_id TEXT, trigger_id TEXT,
 PRIMARY KEY(line_id,epoch,occurrence_id));
CREATE INDEX IF NOT EXISTS unresolved
 ON pending(line_id,eligible_at,occurrence_id) WHERE disposition='pending';
CREATE INDEX IF NOT EXISTS armed_work
 ON pending(session_id,eligible_at,occurrence_id) WHERE disposition='pending';
CREATE INDEX IF NOT EXISTS pending_by_run ON pending(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS pending_by_trigger ON pending(line_id,epoch,trigger_id,eligible_at);
CREATE TABLE IF NOT EXISTS evaluated (
 line_id TEXT NOT NULL, epoch INTEGER NOT NULL, since TEXT NOT NULL, until TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evaluated_by_line ON evaluated(line_id,epoch);
CREATE TABLE IF NOT EXISTS open_proposals (
 line_id TEXT NOT NULL, node_id TEXT NOT NULL, proposal_id TEXT NOT NULL,
 signature TEXT NOT NULL, superseded TEXT, PRIMARY KEY(line_id,node_id));
-- What the activity feed lists, one row per transition it names (by journal
-- position), newest first through activity_recent; each Line's name and last
-- enablement state, which says whether a transition changed it; and the last
-- transition the rows were derived from, which may trail progress when a
-- binary that keeps no activity rows applied transitions: those are derived
-- on the next open.
CREATE TABLE IF NOT EXISTS activity (
 sequence INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, line TEXT,
 fields TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS activity_recent ON activity(at, sequence);
CREATE TABLE IF NOT EXISTS activity_lines (
 line_id TEXT PRIMARY KEY, line TEXT NOT NULL, state TEXT);
CREATE TABLE IF NOT EXISTS activity_progress (
 singleton INTEGER PRIMARY KEY, sequence INTEGER NOT NULL);
"""
#: Transitions derived per transaction when activity rows trail the projection:
#: the write lock is released between chunks, so a concurrent opener waits for
#: one chunk, never the whole journal.
_ACTIVITY_CHUNK = 512


@dataclass(frozen=True)
class DispatchActivity:
    """One dispatch transition the activity feed lists, at its journal position.

    ``event`` is ``occurrence_due``, ``occurrence_lapsed``,
    ``line_state_changed`` or ``record_unavailable`` (a transition whose
    payload is lost); ``fields`` are that event's own.
    """

    position: int
    recorded_at: str
    event: str
    line: str | None
    fields: dict[str, Any]


def _line_state(conn: sqlite3.Connection, line_id: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT line,state FROM activity_lines WHERE line_id=?", (line_id,)
    ).fetchone()
    return (None, None) if row is None else (row[0], row[1])


def _note_activity(
    conn: sqlite3.Connection, event: dict[str, Any] | None, sequence: int, at: str
) -> None:
    """Record the activity row one transition names, if it names one.

    A pending occurrence is due work, a lapsed tick is listed, and a session,
    stop or rollover is listed only when it changes the Line's enablement
    state, read as the ``lines`` section reads it: an active segment is
    ``running``; a stopped one is ``disabled`` when deliberately disabled or
    stopped with no reason, else ``stopped``. A daemon restart's rollover
    leaves a running Line running. ``None`` is a transition whose payload is
    lost. Every transition moves the activity high-water mark.
    """

    conn.execute("INSERT OR REPLACE INTO activity_progress VALUES(1,?)", (sequence,))

    def note(name: str, line: str | None, fields: dict[str, Any]) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO activity VALUES(?,?,?,?,?)",
            (
                sequence,
                at,
                name,
                line,
                json.dumps({key: value for key, value in fields.items() if value is not None}),
            ),
        )

    if event is None:
        note("record_unavailable", None, {})
        return
    kind, data = event["kind"], event["data"]
    if kind == "pending":
        line_id, line = data["line_identity_digest"], data["line"]
        conn.execute(
            "INSERT INTO activity_lines VALUES(?,?,NULL) ON CONFLICT(line_id) "
            "DO UPDATE SET line=excluded.line",
            (line_id, line),
        )
        occurrence = data["occurrence"]
        note(
            "occurrence_due",
            line,
            {
                "trigger": data.get("trigger"),
                "occurrence": occurrence["occurrence_id"],
                "eligible_at": occurrence["eligible_at"],
            },
        )
    elif kind == "closed" and data["status"] == "lapsed":
        line, _state = _line_state(conn, data["line_id"])
        if line is not None:
            note("occurrence_lapsed", line, {"occurrence": data["occurrence_id"]})
    elif kind in {"session", "stop", "rollover"}:
        segment = data["opened"] if kind == "rollover" else data
        reason = segment.get("stop_reason") if kind == "stop" else None
        state = (
            "running"
            if kind != "stop"
            else "disabled"
            if reason in {None, "disabled"}
            else "stopped"
        )
        line_id, line = segment["line_id"], segment["line"]
        _known, previous = _line_state(conn, line_id)
        conn.execute("INSERT OR REPLACE INTO activity_lines VALUES(?,?,?)", (line_id, line, state))
        if previous != state:
            note(
                "line_state_changed",
                line,
                {
                    "previous": previous,
                    "state": state,
                    "reason": reason,
                    "detail": segment.get("detail") if kind == "stop" else None,
                },
            )


@dataclass(frozen=True)
class LineOpenProposal:
    """The proposal a Line's terminal last submitted, and what its change was.

    ``signature`` is the change signature (``line_change_signature``) a later
    run of the Line compares its own change against; ``superseded`` is the
    proposal this one withdrew in its place, if any. The record stays after
    the proposal settles: a later run reads the proposal's status, not this.
    """

    line_id: str
    node_id: str
    proposal_id: str
    signature: str
    superseded: str | None = None


def dispatch_root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "line-dispatch"


class LineDispatchStore:
    """The Line dispatch journal and its disposable SQLite projection.

    Inside a preview (``previewing()``, rule R12) the store writes nothing: the
    projection is an in-memory copy of the one on disk, caught up from the
    journal the same way, and a transition the commit would record is applied
    to that copy only, never appended to the journal.
    """

    def __init__(self, instance: PlaybillInstance):
        self.instance = instance
        self.root = dispatch_root(instance)
        if not is_previewing():
            self.root.mkdir(mode=0o700, exist_ok=True)
        self.journal = LocalJournalBackend(self.root) if self.root.is_dir() else None
        self.stream = JournalStreamIdentityV1(
            instance_id=instance.descriptor.instance_id,
            journal_family=LINE_DISPATCH_JOURNAL_FAMILY,
            stream_id="lines",
        )
        self.path = self.root / "dispatch.sqlite3"
        self._preview_sequence = 0

    def _open_projection(self) -> sqlite3.Connection:
        if not is_previewing():
            return sqlite3.connect(self.path)
        conn = sqlite3.connect(":memory:")
        if self.path.is_file():
            source = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                source.backup(conn)
            finally:
                source.close()
        return conn

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            if self.path.is_symlink():
                raise ValueError("dispatch projection must be a regular file")
            conn = self._open_projection()
            conn.row_factory = sqlite3.Row
            try:
                # This is a disposable projection, never a migration of authority.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(pending)")}
                if columns and not {"disposition", "session_id", "trigger_id"} <= columns:
                    conn.executescript(
                        "BEGIN IMMEDIATE; DROP TABLE pending; DROP TABLE sessions; "
                        "DROP TABLE progress; DROP TABLE IF EXISTS activity; "
                        "DROP TABLE IF EXISTS activity_lines; "
                        "DROP TABLE IF EXISTS activity_progress;" + _SCHEMA + "COMMIT;"
                    )
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                if not is_previewing():
                    # Activity rows trailing what the projection applied (a
                    # projection from before them, or transitions an older
                    # binary applied): derive the gap from the journal. A
                    # preview's in-memory copy is discarded, so it never derives.
                    self._derive_activity(conn, through=row[0] if row else 0)
                start = row[0] + 1 if row else 1
                self._preview_sequence = start - 1
                for stored in (
                    ()
                    if self.journal is None
                    else self.journal.select_records(
                        self.stream, partition_id="dispatch", first_sequence=start
                    )
                ):
                    payload = read_payload(
                        self.instance.body_store(),
                        stored.record.payload_digest,
                        access=BodyAccessContext(
                            principal_id="line-dispatch-projection", can_read_body=True
                        ),
                    )
                    at = str(format_datetime(stored.record.recorded_at))
                    if isinstance(payload, UnavailablePayload):
                        _note_activity(conn, None, stored.record.sequence, at)
                        conn.execute(
                            "INSERT OR REPLACE INTO progress VALUES(1,?)", (stored.record.sequence,)
                        )
                        self._preview_sequence = stored.record.sequence
                        continue
                    if not isinstance(payload, dict):
                        raise ValueError("invalid Line dispatch transition")
                    self._apply(conn, payload, stored.record.sequence, at)
                    self._preview_sequence = stored.record.sequence
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _derive_activity(self, conn: sqlite3.Connection, *, through: int) -> None:
        """Derive the activity rows of transitions after the high-water mark, through ``through``.

        One transaction per chunk of ``_ACTIVITY_CHUNK`` transitions, each
        re-reading the mark under the write lock, so concurrent openers share
        the work and none waits for more than one chunk.
        """

        while True:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT sequence FROM activity_progress WHERE singleton=1"
                ).fetchone()
                mark = 0 if row is None else int(row[0])
                if mark > through:
                    mark = 0  # rows past what the projection holds: derive again
                if mark == 0:
                    conn.execute("DELETE FROM activity")
                    conn.execute("DELETE FROM activity_lines")
                    conn.execute("DELETE FROM activity_progress")
                if mark >= through or self.journal is None:
                    conn.commit()
                    return
                last = min(through, mark + _ACTIVITY_CHUNK)
                for stored in self.journal.select_records(
                    self.stream,
                    partition_id="dispatch",
                    first_sequence=mark + 1,
                    last_sequence=last,
                ):
                    payload = read_payload(
                        self.instance.body_store(),
                        stored.record.payload_digest,
                        access=BodyAccessContext(
                            principal_id="line-dispatch-projection", can_read_body=True
                        ),
                    )
                    _note_activity(
                        conn,
                        payload if isinstance(payload, dict) else None,
                        stored.record.sequence,
                        str(format_datetime(stored.record.recorded_at)),
                    )
                # The chunk is covered whole, so the mark always advances.
                conn.execute("INSERT OR REPLACE INTO activity_progress VALUES(1,?)", (last,))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _apply(
        self, conn: sqlite3.Connection, event: dict[str, Any], sequence: int, at: str
    ) -> None:
        _note_activity(conn, event, sequence, at)
        kind, data = event["kind"], event["data"]
        if kind in {"session", "coverage", "stop", "rollover"}:
            for segment in (data["stopped"], data["opened"]) if kind == "rollover" else (data,):
                conn.execute(
                    "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                    (
                        segment["session_id"],
                        segment["line_id"],
                        segment["occurrence_epoch"],
                        int(segment["stops_at"] is None),
                        json.dumps(segment),
                    ),
                )
        elif kind == "pending":
            # Re-evaluation and retries cannot replace the exact original binding.
            # The session that matched it, if any: only that armed segment may
            # dispatch it without an explicit call.
            conn.execute(
                "INSERT OR IGNORE INTO pending VALUES(?,?,?,?,?,?,NULL,?,?)",
                (
                    data["line_identity_digest"],
                    data["occurrence_epoch"],
                    data["occurrence"]["occurrence_id"],
                    "pending",
                    format_datetime(parse_datetime(data["occurrence"]["eligible_at"])),
                    json.dumps(data),
                    data.get("session_id"),
                    data.get("trigger"),
                ),
            )
        elif kind == "admitted":
            conn.execute(
                "UPDATE pending SET disposition='admitted',run_id=? "
                "WHERE line_id=? AND epoch=? AND occurrence_id=?",
                (data["run_id"], data["line_id"], data["epoch"], data["occurrence_id"]),
            )
        elif kind == "closed":
            if data["status"] not in {"rejected", "superseded", "lapsed"}:
                raise ValueError("invalid closed occurrence disposition")
            conn.execute(
                "UPDATE pending SET disposition=? WHERE line_id=? AND epoch=? "
                "AND occurrence_id=? AND disposition!='admitted'",
                (data["status"], data["line_id"], data["epoch"], data["occurrence_id"]),
            )
        elif kind == "reconciled":
            # A retry keeps the payload's own session and Trigger; a rebind to a
            # current Trigger carries the evaluation that derived it again.
            conn.execute(
                "UPDATE pending SET disposition='pending',payload=?,session_id=?,trigger_id=? "
                "WHERE line_id=? AND epoch=? AND occurrence_id=? AND "
                "disposition!='admitted'",
                (
                    json.dumps(data),
                    data.get("session_id"),
                    data.get("trigger"),
                    data["line_identity_digest"],
                    data["occurrence_epoch"],
                    data["occurrence"]["occurrence_id"],
                ),
            )
        elif kind == "evaluated":
            # A range explicitly evaluated in full: it covers a restart gap.
            conn.execute(
                "INSERT INTO evaluated VALUES(?,?,?,?)",
                (data["line_id"], data["epoch"], data["since"], data["until"]),
            )
        elif kind == "open_proposal":
            # The proposal a Line's terminal submitted replaces the one before it.
            conn.execute(
                "INSERT OR REPLACE INTO open_proposals VALUES(?,?,?,?,?)",
                (
                    data["line_id"],
                    data["node_id"],
                    data["proposal_id"],
                    data["signature"],
                    data.get("superseded"),
                ),
            )
        elif kind != "dispatch_refused":
            raise ValueError("unknown Line dispatch transition")
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?)", (sequence,))

    def advance_coverage(self, conn: sqlite3.Connection, session: dict[str, Any]) -> None:
        """Move an active segment's time-only coverage in the projection, journaling nothing.

        Coverage that matched nothing is not retained: the journal keeps every
        coverage record that moved a position, cursor, detail or a timed
        Trigger's first reach. A rebuilt projection therefore reads an idle
        stretch as uncovered from the last of those, which only widens the
        range a restart asks to be evaluated explicitly.
        """

        conn.execute(
            "UPDATE sessions SET payload=? WHERE session_id=? AND active=1",
            (json.dumps(session), session["session_id"]),
        )

    def append(
        self,
        conn: sqlite3.Connection,
        kind: str,
        data: dict[str, Any],
        *,
        actor: GovernedActorContext,
        now: datetime,
    ) -> None:
        event = {"tag": "playbill-line-dispatch-transition-v1", "kind": kind, "data": data}
        if is_previewing():
            # What the commit would record, applied to this preview's in-memory
            # projection only: no journal record and no body are written.
            self._preview_sequence += 1
            self._apply(conn, event, self._preview_sequence, str(format_datetime(now)))
            return
        journal = self.journal
        assert journal is not None
        from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

        bodies = self.instance.body_store()
        ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
            lambda reservation: journal.select_records(
                self.stream,
                event_kind="line_dispatch_transition",
                payload_digest=reservation.body_digest,
            ),
            bodies=bodies,
            intended_event_kinds=frozenset({"line_dispatch_transition"}),
        )
        head = journal.read_head(self.stream, "dispatch")
        journal.activate_writer(self.stream, "dispatch", fencing_token=_FENCE, expected_head=head)
        stored = ProcedureExhaustWriter(
            journal=journal, bodies=self.instance.body_store(), fencing_token=_FENCE
        ).append(
            stream=self.stream,
            partition_id="dispatch",
            event_kind="line_dispatch_transition",
            accepted_coordinate=AcceptedCoordinate.from_internal(
                self.instance.accepted_coordinate()
            ),
            definition_digest=self.stream.identity_digest,
            actor_context=actor,
            recorded_at=now,
            payload=event,
            expected_head=head,
        )
        self._apply(
            conn, event, stored.record.sequence, str(format_datetime(stored.record.recorded_at))
        )
        conn.commit()

    def activity(
        self, *, limit: int, before: tuple[str, int] | None = None
    ) -> tuple[DispatchActivity, ...]:
        """The newest activity rows, strictly ``before`` an (instant, position) key.

        One range of ``activity_recent``: a page costs its rows, whatever the
        journal holds.
        """

        where, args = ("WHERE (at,sequence)<(?,?) ", list(before)) if before else ("", [])
        with self.locked() as conn:
            rows = conn.execute(
                "SELECT sequence,at,event,line,fields FROM activity INDEXED BY activity_recent "
                f"{where}ORDER BY at DESC,sequence DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return tuple(
            DispatchActivity(
                position=int(row[0]),
                recorded_at=str(row[1]),
                event=str(row[2]),
                line=None if row[3] is None else str(row[3]),
                fields=json.loads(row[4]),
            )
            for row in rows
        )

    def open_proposal(self, line_id: str, node_id: str) -> LineOpenProposal | None:
        """The proposal this Line's terminal last submitted; one keyed row."""

        with self.locked() as conn:
            row = conn.execute(
                "SELECT proposal_id,signature,superseded FROM open_proposals "
                "WHERE line_id=? AND node_id=?",
                (line_id, node_id),
            ).fetchone()
        if row is None:
            return None
        return LineOpenProposal(
            line_id=line_id,
            node_id=node_id,
            proposal_id=row[0],
            signature=row[1],
            superseded=row[2],
        )

    def record_open_proposal(
        self, record: LineOpenProposal, *, actor: GovernedActorContext, now: datetime
    ) -> None:
        """Record the proposal a Line's terminal submitted; a repeat records nothing."""

        with self.locked() as conn:
            row = conn.execute(
                "SELECT proposal_id,signature,superseded FROM open_proposals "
                "WHERE line_id=? AND node_id=?",
                (record.line_id, record.node_id),
            ).fetchone()
            if row is not None and tuple(row) == (
                record.proposal_id,
                record.signature,
                record.superseded,
            ):
                return
            self.append(
                conn,
                "open_proposal",
                {
                    "line_id": record.line_id,
                    "node_id": record.node_id,
                    "proposal_id": record.proposal_id,
                    "signature": record.signature,
                    "superseded": record.superseded,
                },
                actor=actor,
                now=now,
            )

    @staticmethod
    def arm_view(
        data: dict[str, Any], *, pending_automatic: int = 0, pending_explicit: int = 0
    ) -> LineEnablement:
        stopped = data["stops_at"] is not None and data.get("stop_reason") is not None
        return LineEnablement(
            enablement_id=data["arm_id"],
            line=data["line"],
            line_artifact_digest=data["line_artifact_digest"],
            occurrence_epoch=data["occurrence_epoch"],
            triggers=tuple(
                LineTriggerVersion(trigger=trigger, artifact_digest=digest)
                for trigger, digest in sorted(
                    data.get("trigger_pins", {}).items(), key=lambda item: item[0].encode()
                )
            ),
            state="stopped" if stopped else "enabled",
            enabled_at=data["armed_at"],
            enabled_by=data["armed_by"],
            evaluated_until=data["evaluated_until"],
            stopped_at=data["stops_at"] if stopped else None,
            stop_reason=data.get("stop_reason") if stopped else None,
            detail=data.get("detail"),
            pending_automatic=pending_automatic,
            pending_explicit=pending_explicit,
        )

    def occurrence_states(self, line_id: str, occurrence_ids: tuple[str, ...]) -> dict[str, str]:
        # An occurrence identity already names its Line epoch and Trigger.
        with self.locked() as conn:
            return {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT occurrence_id,disposition FROM pending WHERE line_id=? "
                    + "AND occurrence_id IN ("
                    + ",".join("?" for _ in occurrence_ids)
                    + ")",
                    (line_id, *occurrence_ids),
                )
            }
