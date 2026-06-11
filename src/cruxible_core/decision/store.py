"""SQLite persistence for decision records and events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cruxible_core.decision.types import DecisionEvent, DecisionRecord
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import DecisionStoreProtocol
from cruxible_core.temporal import format_datetime, utc_now

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS decision_records (
    decision_record_id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    subject_type TEXT,
    subject_id TEXT,
    status TEXT NOT NULL,
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    finalized_at TEXT,
    final_decision TEXT,
    decision_class TEXT,
    rationale TEXT NOT NULL DEFAULT '',
    abandoned_reason TEXT NOT NULL DEFAULT '',
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_records_status ON decision_records(status);
CREATE INDEX IF NOT EXISTS idx_decision_records_subject
    ON decision_records(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_decision_records_opened_at ON decision_records(opened_at);
CREATE INDEX IF NOT EXISTS idx_decision_records_finalized_at ON decision_records(finalized_at);
CREATE INDEX IF NOT EXISTS idx_decision_records_decision_class
    ON decision_records(decision_class);

CREATE TABLE IF NOT EXISTS decision_events (
    decision_event_id TEXT PRIMARY KEY,
    decision_record_id TEXT NOT NULL REFERENCES decision_records(decision_record_id),
    sequence INTEGER NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    input_summary TEXT NOT NULL,
    output_digest TEXT,
    output_summary TEXT,
    receipt_id TEXT,
    trace_ids TEXT NOT NULL DEFAULT '[]',
    head_snapshot_id TEXT,
    error_type TEXT,
    error_message TEXT,
    surface TEXT,
    request_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    event_json TEXT NOT NULL,
    UNIQUE(decision_record_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_decision_events_record
    ON decision_events(decision_record_id, sequence);
CREATE INDEX IF NOT EXISTS idx_decision_events_receipt ON decision_events(receipt_id);
CREATE INDEX IF NOT EXISTS idx_decision_events_trace_digest
    ON decision_events(trace_ids);
CREATE INDEX IF NOT EXISTS idx_decision_events_status ON decision_events(status);
"""


class DecisionStore(DecisionStoreProtocol):
    """Stores and retrieves decision records and append-only events."""

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        initialize_schema: bool = True,
    ) -> None:
        self._db_path = str(db_path)
        self._conn = connection if connection is not None else sqlite3.connect(self._db_path)
        self._owns_connection = connection is None
        self._conn.row_factory = sqlite3.Row
        if initialize_schema:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)

    def save_record(self, record: DecisionRecord) -> str:
        """Create or replace a decision record."""
        self._conn.execute(
            "INSERT INTO decision_records "
            "(decision_record_id, question, subject_type, subject_id, status, opened_by, "
            "opened_at, finalized_at, final_decision, decision_class, rationale, "
            "abandoned_reason, record_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(decision_record_id) DO UPDATE SET "
            "question = excluded.question, "
            "subject_type = excluded.subject_type, "
            "subject_id = excluded.subject_id, "
            "status = excluded.status, "
            "opened_by = excluded.opened_by, "
            "opened_at = excluded.opened_at, "
            "finalized_at = excluded.finalized_at, "
            "final_decision = excluded.final_decision, "
            "decision_class = excluded.decision_class, "
            "rationale = excluded.rationale, "
            "abandoned_reason = excluded.abandoned_reason, "
            "record_json = excluded.record_json",
            (
                record.decision_record_id,
                record.question,
                record.subject_type,
                record.subject_id,
                record.status,
                record.opened_by,
                format_datetime(record.opened_at),
                format_datetime(record.finalized_at),
                record.final_decision,
                record.decision_class,
                record.rationale,
                record.abandoned_reason,
                record.model_dump_json(),
            ),
        )
        return record.decision_record_id

    def get_record(self, decision_record_id: str) -> DecisionRecord | None:
        row = self._conn.execute(
            "SELECT record_json FROM decision_records WHERE decision_record_id = ?",
            (decision_record_id,),
        ).fetchone()
        if row is None:
            return None
        return DecisionRecord.model_validate_json(row["record_json"])

    def list_records(
        self,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionRecord]:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if subject_type is not None:
            conditions.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            conditions.append("subject_id = ?")
            params.append(subject_id)
        if decision_class is not None:
            conditions.append("decision_class = ?")
            params.append(decision_class)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"SELECT record_json FROM decision_records{where} "
            "ORDER BY opened_at DESC, decision_record_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [DecisionRecord.model_validate_json(row["record_json"]) for row in rows]

    def count_records(
        self,
        *,
        status: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        decision_class: str | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        if subject_type is not None:
            conditions.append("subject_type = ?")
            params.append(subject_type)
        if subject_id is not None:
            conditions.append("subject_id = ?")
            params.append(subject_id)
        if decision_class is not None:
            conditions.append("decision_class = ?")
            params.append(decision_class)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM decision_records{where}",
            params,
        ).fetchone()
        return int(row["count"]) if row else 0

    def update_record(self, record: DecisionRecord) -> None:
        if self.get_record(record.decision_record_id) is None:
            raise ConfigError(f"Decision record '{record.decision_record_id}' not found")
        self.save_record(record)

    def append_event(self, event: DecisionEvent) -> str:
        record = self.get_record(event.decision_record_id)
        if record is None:
            raise ConfigError(f"Decision record '{event.decision_record_id}' not found")
        if record.status != "open":
            raise ConfigError(f"Decision record '{event.decision_record_id}' is not open")
        sequence = self._next_sequence(event.decision_record_id)
        event = event.model_copy(update={"sequence": sequence})
        self._conn.execute(
            "INSERT INTO decision_events "
            "(decision_event_id, decision_record_id, sequence, command, status, "
            "input_digest, input_summary, output_digest, output_summary, receipt_id, "
            "trace_ids, head_snapshot_id, error_type, error_message, surface, request_id, "
            "started_at, finished_at, event_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.decision_event_id,
                event.decision_record_id,
                event.sequence,
                event.command,
                event.status,
                event.input_digest,
                event.input_summary,
                event.output_digest,
                event.output_summary,
                event.receipt_id,
                json.dumps(event.trace_ids),
                event.head_snapshot_id,
                event.error_type,
                event.error_message,
                event.surface,
                event.request_id,
                format_datetime(event.started_at),
                format_datetime(event.finished_at),
                event.model_dump_json(),
            ),
        )
        return event.decision_event_id

    def list_events(
        self,
        decision_record_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionEvent]:
        rows = self._conn.execute(
            "SELECT event_json FROM decision_events WHERE decision_record_id = ? "
            "ORDER BY sequence ASC LIMIT ? OFFSET ?",
            (decision_record_id, limit, offset),
        ).fetchall()
        return [DecisionEvent.model_validate_json(row["event_json"]) for row in rows]

    def find_events(
        self,
        *,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DecisionEvent]:
        where, params = self._event_filters(
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
        )
        rows = self._conn.execute(
            f"SELECT event_json FROM decision_events{where} "
            "ORDER BY finished_at DESC, decision_event_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [DecisionEvent.model_validate_json(row["event_json"]) for row in rows]

    def count_events(
        self,
        *,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
    ) -> int:
        where, params = self._event_filters(
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
        )
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM decision_events{where}",
            params,
        ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _event_filters(
        *,
        decision_record_id: str | None = None,
        receipt_id: str | None = None,
        trace_id: str | None = None,
        status: str | None = None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if decision_record_id is not None:
            conditions.append("decision_record_id = ?")
            params.append(decision_record_id)
        if receipt_id is not None:
            conditions.append("receipt_id = ?")
            params.append(receipt_id)
        if trace_id is not None:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM json_each(decision_events.trace_ids) "
                "WHERE json_each.value = ?"
                ")"
            )
            params.append(trace_id)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    def finalize_record(
        self,
        decision_record_id: str,
        *,
        final_decision: str,
        decision_class: str,
        rationale: str = "",
    ) -> DecisionRecord:
        record = self.get_record(decision_record_id)
        if record is None:
            raise ConfigError(f"Decision record '{decision_record_id}' not found")
        if record.status != "open":
            raise ConfigError(f"Decision record '{decision_record_id}' is not open")
        updated = record.model_copy(
            update={
                "status": "finalized",
                "final_decision": final_decision,
                "decision_class": decision_class,
                "rationale": rationale,
                "finalized_at": utc_now(),
            }
        )
        self.update_record(updated)
        return updated

    def abandon_record(self, decision_record_id: str, *, reason: str = "") -> DecisionRecord:
        record = self.get_record(decision_record_id)
        if record is None:
            raise ConfigError(f"Decision record '{decision_record_id}' not found")
        if record.status != "open":
            raise ConfigError(f"Decision record '{decision_record_id}' is not open")
        updated = record.model_copy(
            update={
                "status": "abandoned",
                "abandoned_reason": reason,
                "finalized_at": utc_now(),
            }
        )
        self.update_record(updated)
        return updated

    def _next_sequence(self, decision_record_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS max_sequence "
            "FROM decision_events WHERE decision_record_id = ?",
            (decision_record_id,),
        ).fetchone()
        return int(row["max_sequence"]) + 1

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()
