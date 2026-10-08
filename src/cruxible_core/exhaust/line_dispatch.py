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
from datetime import datetime
from pathlib import Path
from typing import Any

from cruxible_client.contracts.line_dispatch import LineEnablement, LineTriggerVersion
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.records import (
    LINE_DISPATCH_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    parse_journal_payload,
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
"""


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
                        "DROP TABLE progress;" + _SCHEMA + "COMMIT;"
                    )
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                start = row[0] + 1 if row else 1
                self._preview_sequence = start - 1
                for stored in (
                    ()
                    if self.journal is None
                    else self.journal.select_records(
                        self.stream, partition_id="dispatch", first_sequence=start
                    )
                ):
                    payload = parse_journal_payload(
                        self.instance.body_store().read(
                            stored.record.payload_digest,
                            access=BodyAccessContext(
                                principal_id="line-dispatch-projection", can_read_body=True
                            ),
                        )
                    )
                    if not isinstance(payload, dict):
                        raise ValueError("invalid Line dispatch transition")
                    self._apply(conn, payload, stored.record.sequence)
                    self._preview_sequence = stored.record.sequence
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _apply(self, conn: sqlite3.Connection, event: dict[str, Any], sequence: int) -> None:
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
        elif kind != "dispatch_refused":
            raise ValueError("unknown Line dispatch transition")
        conn.execute("INSERT OR REPLACE INTO progress VALUES(1,?)", (sequence,))

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
            self._apply(conn, event, self._preview_sequence)
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
        self._apply(conn, event, stored.record.sequence)
        conn.commit()

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
