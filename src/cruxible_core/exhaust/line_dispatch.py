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

from cruxible_client.contracts.line_dispatch import LineArmV1
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
 session_id TEXT,
 PRIMARY KEY(line_id,epoch,occurrence_id));
CREATE INDEX IF NOT EXISTS unresolved
 ON pending(line_id,eligible_at,occurrence_id) WHERE disposition='pending';
CREATE INDEX IF NOT EXISTS armed_work
 ON pending(session_id,eligible_at,occurrence_id) WHERE disposition='pending';
"""


def dispatch_root(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "line-dispatch"


class LineDispatchStore:
    def __init__(self, instance: PlaybillInstance):
        self.instance = instance
        self.root = dispatch_root(instance)
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.journal = LocalJournalBackend(self.root)
        self.stream = JournalStreamIdentityV1(
            instance_id=instance.descriptor.instance_id,
            journal_family=LINE_DISPATCH_JOURNAL_FAMILY,
            stream_id="lines",
        )
        self.path = self.root / "dispatch.sqlite3"

    @contextmanager
    def locked(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            if self.path.is_symlink():
                raise ValueError("dispatch projection must be a regular file")
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                # This is a disposable projection, never a migration of authority.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(pending)")}
                if columns and not {"disposition", "session_id"} <= columns:
                    conn.executescript(
                        "BEGIN IMMEDIATE; DROP TABLE pending; DROP TABLE sessions; "
                        "DROP TABLE progress;" + _SCHEMA + "COMMIT;"
                    )
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT sequence FROM progress WHERE singleton=1").fetchone()
                start = row[0] + 1 if row else 1
                for stored in self.journal.select_records(
                    self.stream, partition_id="dispatch", first_sequence=start
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
                conn.commit()
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _apply(self, conn: sqlite3.Connection, event: dict[str, Any], sequence: int) -> None:
        kind, data = event["kind"], event["data"]
        if kind in {"session", "coverage", "stop"}:
            conn.execute(
                "INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?)",
                (
                    data["session_id"],
                    data["line_id"],
                    data["occurrence_epoch"],
                    int(data["stops_at"] is None),
                    json.dumps(data),
                ),
            )
        elif kind == "pending":
            # Re-evaluation and retries cannot replace the exact original binding.
            # The session that matched it, if any: only that armed segment may
            # dispatch it without an explicit call.
            conn.execute(
                "INSERT OR IGNORE INTO pending VALUES(?,?,?,?,?,?,NULL,?)",
                (
                    data["line_identity_digest"],
                    data["occurrence_epoch"],
                    data["occurrence"]["occurrence_id"],
                    "pending",
                    format_datetime(parse_datetime(data["occurrence"]["eligible_at"])),
                    json.dumps(data),
                    data.get("session_id"),
                ),
            )
        elif kind == "admitted":
            conn.execute(
                "UPDATE pending SET disposition='admitted',run_id=? "
                "WHERE line_id=? AND epoch=? AND occurrence_id=?",
                (data["run_id"], data["line_id"], data["epoch"], data["occurrence_id"]),
            )
        elif kind == "closed":
            if data["status"] not in {"rejected", "superseded"}:
                raise ValueError("invalid closed occurrence disposition")
            conn.execute(
                "UPDATE pending SET disposition=? WHERE line_id=? AND epoch=? "
                "AND occurrence_id=? AND disposition!='admitted'",
                (data["status"], data["line_id"], data["epoch"], data["occurrence_id"]),
            )
        elif kind == "reconciled":
            conn.execute(
                "UPDATE pending SET disposition='pending',payload=? WHERE "
                "line_id=? AND epoch=? AND occurrence_id=? AND "
                "disposition!='admitted'",
                (
                    json.dumps(data),
                    data["line_identity_digest"],
                    data["occurrence_epoch"],
                    data["occurrence"]["occurrence_id"],
                ),
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
        from cruxible_core.storage.material_reservations import ProcedureMaterialReservationStore

        bodies = self.instance.body_store()
        ProcedureMaterialReservationStore(bodies.reservation_root).recover_run_material(
            lambda reservation: self.journal.select_records(
                self.stream,
                event_kind="line_dispatch_transition",
                payload_digest=reservation.body_digest,
            ),
            bodies=bodies,
            intended_event_kinds=frozenset({"line_dispatch_transition"}),
        )
        head = self.journal.read_head(self.stream, "dispatch")
        self.journal.activate_writer(
            self.stream, "dispatch", fencing_token=_FENCE, expected_head=head
        )
        event = {"tag": "playbill-line-dispatch-transition-v1", "kind": kind, "data": data}
        stored = ProcedureExhaustWriter(
            journal=self.journal, bodies=self.instance.body_store(), fencing_token=_FENCE
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
    ) -> LineArmV1:
        stopped = data["stops_at"] is not None and data.get("stop_reason") is not None
        return LineArmV1(
            arm_id=data["arm_id"],
            line=data["line"],
            line_artifact_digest=data["line_artifact_digest"],
            occurrence_epoch=data["occurrence_epoch"],
            state="stopped" if stopped else "armed",
            armed_at=data["armed_at"],
            armed_by=data["armed_by"],
            evaluated_until=data["evaluated_until"],
            stopped_at=data["stops_at"] if stopped else None,
            stop_reason=data.get("stop_reason") if stopped else None,
            detail=data.get("detail"),
            pending_automatic=pending_automatic,
            pending_explicit=pending_explicit,
        )

    def occurrence_states(
        self, line_id: str, epoch: int, occurrence_ids: tuple[str, ...]
    ) -> dict[str, str]:
        with self.locked() as conn:
            return {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT occurrence_id,disposition FROM pending WHERE line_id=? AND epoch=? "
                    + "AND occurrence_id IN ("
                    + ",".join("?" for _ in occurrence_ids)
                    + ")",
                    (line_id, epoch, *occurrence_ids),
                )
            }
