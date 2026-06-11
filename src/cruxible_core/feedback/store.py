"""SQLite persistence for feedback and outcome records."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.instance_protocol import FeedbackStoreProtocol
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_json TEXT NOT NULL,
    target_relationship TEXT NOT NULL DEFAULT '',
    target_from_type TEXT NOT NULL DEFAULT '',
    target_from_id TEXT NOT NULL DEFAULT '',
    target_to_type TEXT NOT NULL DEFAULT '',
    target_to_id TEXT NOT NULL DEFAULT '',
    target_edge_key INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    reason_code TEXT,
    reason_remediation_hint TEXT,
    scope_hints TEXT NOT NULL DEFAULT '{}',
    feedback_profile_key TEXT,
    feedback_profile_version INTEGER,
    decision_context TEXT NOT NULL DEFAULT '{}',
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    decision_surface_type TEXT,
    decision_surface_name TEXT,
    source TEXT NOT NULL DEFAULT 'human',
    model_id TEXT,
    corrections TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_receipt ON feedback(receipt_id);

CREATE TABLE IF NOT EXISTS feedback_entities (
    feedback_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (feedback_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_entities_entity_id ON feedback_entities(entity_id);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL,
    anchor_type TEXT NOT NULL DEFAULT 'receipt',
    anchor_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    outcome_code TEXT,
    outcome_remediation_hint TEXT,
    scope_hints TEXT NOT NULL DEFAULT '{}',
    outcome_profile_key TEXT,
    outcome_profile_version INTEGER,
    decision_context TEXT NOT NULL DEFAULT '{}',
    lineage_snapshot TEXT NOT NULL DEFAULT '{}',
    relationship_type TEXT,
    decision_surface_type TEXT,
    decision_surface_name TEXT,
    source TEXT NOT NULL DEFAULT 'human',
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_receipt ON outcomes(receipt_id);
"""


class FeedbackStore(FeedbackStoreProtocol):
    """Stores and retrieves feedback and outcome records."""

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
            self._conn.executescript(_SCHEMA)

    # -----------------------------------------------------------------
    # Feedback
    # -----------------------------------------------------------------

    def save_feedback(self, record: FeedbackRecord) -> str:
        """Persist a feedback record. Returns the feedback_id."""
        self._save_feedback(record)
        return record.feedback_id

    def save_feedback_batch(self, records: list[FeedbackRecord]) -> list[str]:
        """Persist multiple feedback records. Does not commit."""
        for record in records:
            self._save_feedback(record)
        return [record.feedback_id for record in records]

    def _save_feedback(self, record: FeedbackRecord) -> None:
        """Persist a feedback record without committing."""
        decision_surface_type = record.decision_context.get("surface_type")
        decision_surface_name = record.decision_context.get("surface_name")
        self._conn.execute(
            "INSERT OR REPLACE INTO feedback "
            "(feedback_id, receipt_id, action, target_json, target_relationship, "
            "target_from_type, target_from_id, target_to_type, target_to_id, target_edge_key, "
            "reason, reason_code, reason_remediation_hint, scope_hints, "
            "feedback_profile_key, feedback_profile_version, "
            "decision_context, context_snapshot, decision_surface_type, "
            "decision_surface_name, source, model_id, corrections, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.feedback_id,
                record.receipt_id,
                record.action,
                record.target.model_dump_json(),
                record.target.relationship_type,
                record.target.from_type,
                record.target.from_id,
                record.target.to_type,
                record.target.to_id,
                record.target.edge_key,
                record.reason,
                record.reason_code,
                record.reason_remediation_hint,
                json.dumps(record.scope_hints),
                record.feedback_profile_key,
                record.feedback_profile_version,
                json.dumps(record.decision_context),
                json.dumps(record.context_snapshot),
                decision_surface_type,
                decision_surface_name,
                record.source,
                record.model_id,
                json.dumps(record.corrections),
                format_datetime(record.created_at),
            ),
        )
        self._index_feedback_entities(record)

    def get_feedback(self, feedback_id: str) -> FeedbackRecord | None:
        """Load a feedback record by ID."""
        row = self._conn.execute(
            "SELECT * FROM feedback WHERE feedback_id = ?",
            (feedback_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_feedback(row)

    def list_feedback(
        self,
        *,
        receipt_id: str | None = None,
        relationship_type: str | None = None,
        action: str | None = None,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FeedbackRecord]:
        """List feedback records with optional filter."""
        clauses: list[str] = []
        params: list[object] = []

        if receipt_id is not None:
            clauses.append("receipt_id = ?")
            params.append(receipt_id)
        if relationship_type is not None:
            clauses.append("target_relationship = ?")
            params.append(relationship_type)
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if decision_surface_type is not None:
            clauses.append("decision_surface_type = ?")
            params.append(decision_surface_type)
        if decision_surface_name is not None:
            clauses.append("decision_surface_name = ?")
            params.append(decision_surface_name)

        sql = "SELECT * FROM feedback"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, feedback_id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(sql, tuple(params)).fetchall()

        return [self._row_to_feedback(r) for r in rows]

    def list_feedback_by_entity_ids(
        self,
        entity_ids: list[str],
        limit: int = 100,
    ) -> list[FeedbackRecord]:
        """List feedback records that mention any provided entity IDs."""
        if not entity_ids:
            return []

        placeholders = ",".join("?" for _ in entity_ids)
        rows = self._conn.execute(
            "SELECT DISTINCT f.* FROM feedback f "
            "JOIN feedback_entities fe ON fe.feedback_id = f.feedback_id "
            f"WHERE fe.entity_id IN ({placeholders}) "
            "ORDER BY f.created_at DESC LIMIT ?",
            (*entity_ids, limit),
        ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def count_feedback(self, *, receipt_id: str | None = None) -> int:
        """Count feedback records with optional receipt filter."""
        if receipt_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM feedback").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM feedback WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _row_to_feedback(row: sqlite3.Row) -> FeedbackRecord:
        return FeedbackRecord(
            feedback_id=row["feedback_id"],
            receipt_id=row["receipt_id"],
            action=row["action"],
            target=RelationshipInstance.model_validate_json(row["target_json"]),
            reason=row["reason"],
            reason_code=row["reason_code"],
            reason_remediation_hint=row["reason_remediation_hint"],
            scope_hints=json.loads(row["scope_hints"] or "{}"),
            feedback_profile_key=row["feedback_profile_key"],
            feedback_profile_version=row["feedback_profile_version"],
            decision_context=json.loads(row["decision_context"] or "{}"),
            context_snapshot=json.loads(row["context_snapshot"] or "{}"),
            source=row["source"],
            model_id=row["model_id"],
            corrections=json.loads(row["corrections"]),
            created_at=row["created_at"],
        )

    # -----------------------------------------------------------------
    # Outcomes
    # -----------------------------------------------------------------

    def save_outcome(self, record: OutcomeRecord) -> str:
        """Persist an outcome record. Returns the outcome_id."""
        decision_surface_type = record.decision_context.get("surface_type")
        decision_surface_name = record.decision_context.get("surface_name")
        self._conn.execute(
            "INSERT OR REPLACE INTO outcomes "
            "(outcome_id, receipt_id, anchor_type, anchor_id, outcome, outcome_code, "
            "outcome_remediation_hint, scope_hints, outcome_profile_key, "
            "outcome_profile_version, decision_context, lineage_snapshot, "
            "relationship_type, decision_surface_type, decision_surface_name, source, "
            "detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.outcome_id,
                record.receipt_id,
                record.anchor_type,
                record.anchor_id,
                record.outcome,
                record.outcome_code,
                record.outcome_remediation_hint,
                json.dumps(record.scope_hints),
                record.outcome_profile_key,
                record.outcome_profile_version,
                json.dumps(record.decision_context),
                json.dumps(record.lineage_snapshot),
                record.relationship_type,
                decision_surface_type,
                decision_surface_name,
                record.source,
                json.dumps(record.detail),
                format_datetime(record.created_at),
            ),
        )
        return record.outcome_id

    def get_outcome(self, outcome_id: str) -> OutcomeRecord | None:
        """Load an outcome record by ID."""
        row = self._conn.execute(
            "SELECT * FROM outcomes WHERE outcome_id = ?",
            (outcome_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_outcome(row)

    def list_outcomes(
        self,
        *,
        receipt_id: str | None = None,
        anchor_type: str | None = None,
        anchor_id: str | None = None,
        relationship_type: str | None = None,
        decision_surface_type: str | None = None,
        decision_surface_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[OutcomeRecord]:
        """List outcome records with optional filter."""
        clauses: list[str] = []
        params: list[object] = []
        if receipt_id is not None:
            clauses.append("receipt_id = ?")
            params.append(receipt_id)
        if anchor_type is not None:
            clauses.append("anchor_type = ?")
            params.append(anchor_type)
        if anchor_id is not None:
            clauses.append("anchor_id = ?")
            params.append(anchor_id)
        if relationship_type is not None:
            clauses.append("relationship_type = ?")
            params.append(relationship_type)
        if decision_surface_type is not None:
            clauses.append("decision_surface_type = ?")
            params.append(decision_surface_type)
        if decision_surface_name is not None:
            clauses.append("decision_surface_name = ?")
            params.append(decision_surface_name)

        sql = "SELECT * FROM outcomes"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, outcome_id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_outcome(r) for r in rows]

    def count_outcomes(self, *, receipt_id: str | None = None) -> int:
        """Count outcome records with optional receipt filter."""
        if receipt_id is None:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM outcomes").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM outcomes WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
        return int(row["count"]) if row else 0

    @staticmethod
    def _row_to_outcome(row: sqlite3.Row) -> OutcomeRecord:
        return OutcomeRecord(
            outcome_id=row["outcome_id"],
            receipt_id=row["receipt_id"],
            anchor_type=row["anchor_type"],
            anchor_id=row["anchor_id"],
            outcome=row["outcome"],
            outcome_code=row["outcome_code"],
            outcome_remediation_hint=row["outcome_remediation_hint"],
            scope_hints=json.loads(row["scope_hints"] or "{}"),
            outcome_profile_key=row["outcome_profile_key"],
            outcome_profile_version=row["outcome_profile_version"],
            decision_context=json.loads(row["decision_context"] or "{}"),
            lineage_snapshot=json.loads(row["lineage_snapshot"] or "{}"),
            relationship_type=row["relationship_type"],
            source=row["source"],
            detail=json.loads(row["detail"]),
            created_at=row["created_at"],
        )

    def _index_feedback_entities(self, record: FeedbackRecord) -> None:
        """Index entity identifiers touched by a feedback record."""
        self._conn.execute(
            "DELETE FROM feedback_entities WHERE feedback_id = ?",
            (record.feedback_id,),
        )
        t = record.target
        entity_ids = {
            f"{t.from_type}:{t.from_id}",
            f"{t.to_type}:{t.to_id}",
        }
        for entity_id in entity_ids:
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback_entities (feedback_id, entity_id) VALUES (?, ?)",
                (record.feedback_id, entity_id),
            )

    def close(self) -> None:
        """Close the database connection."""
        if self._owns_connection:
            self._conn.close()
