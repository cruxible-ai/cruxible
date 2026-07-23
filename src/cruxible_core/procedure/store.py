"""SQLite persistence for procedure definitions and invocation records.

The store participates in the unified ``state.db`` transaction. It never owns
commits when handed a unit-of-work connection, and it exposes no definition
update operation: a changed definition must be inserted as a new proposal.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    load_actor_context,
)
from cruxible_core.instance_protocol import ProcedureStoreProtocol
from cruxible_core.procedure.types import (
    ProcedureBudgetSpent,
    ProcedureRecord,
    ProcedureRun,
    ProcedureRunVerdict,
    ProcedureStatus,
    compute_procedure_definition_digest,
)
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS procedures (
    procedure_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    definition_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    supersedes_procedure_id TEXT REFERENCES procedures(procedure_id),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    proposed_actor_context TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    resolved_actor_context TEXT,
    resolved_at TEXT,
    retired_actor_context TEXT,
    retired_at TEXT,
    reason TEXT,
    promoted_config_digest TEXT,
    promoted_lock_digest TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedures_name ON procedures(name);
CREATE INDEX IF NOT EXISTS idx_procedures_status ON procedures(status);
CREATE INDEX IF NOT EXISTS idx_procedures_supersedes
    ON procedures(supersedes_procedure_id);

CREATE TABLE IF NOT EXISTS procedure_runs (
    run_id TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL REFERENCES procedures(procedure_id),
    definition_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    verdict TEXT,
    budget_spent_json TEXT NOT NULL DEFAULT '{}',
    receipt_id TEXT,
    started_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedure_runs_procedure
    ON procedure_runs(procedure_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_procedure_runs_status ON procedure_runs(status);
"""


class ProcedureStore(ProcedureStoreProtocol):
    """Store immutable procedure definitions, lifecycle fields, and runs."""

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

    def save_procedure(self, procedure: ProcedureRecord) -> str:
        """Insert a new immutable definition. Does not commit."""
        actual_digest = compute_procedure_definition_digest(procedure.definition)
        if procedure.definition_digest != actual_digest:
            raise ValueError(
                "procedure definition digest mismatch: "
                f"stored={procedure.definition_digest}, computed={actual_digest}"
            )
        self._conn.execute(
            "INSERT INTO procedures "
            "(procedure_id, name, definition_json, definition_digest, status, version, "
            "supersedes_procedure_id, evidence_refs_json, proposed_actor_context, "
            "proposed_at, resolved_actor_context, resolved_at, retired_actor_context, "
            "retired_at, reason, promoted_config_digest, promoted_lock_digest) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                procedure.procedure_id,
                procedure.definition.name,
                json.dumps(
                    procedure.definition.model_dump(mode="json", by_alias=True, exclude_none=True),
                    sort_keys=True,
                ),
                procedure.definition_digest,
                procedure.status,
                procedure.version,
                procedure.supersedes_procedure_id,
                json.dumps(
                    [ref.model_dump(mode="json") for ref in procedure.evidence_refs],
                    sort_keys=True,
                ),
                json.dumps(dump_actor_context(procedure.proposed_actor_context)),
                format_datetime(procedure.proposed_at),
                _dump_optional_actor(procedure.resolved_actor_context),
                _format_optional_datetime(procedure.resolved_at),
                _dump_optional_actor(procedure.retired_actor_context),
                _format_optional_datetime(procedure.retired_at),
                procedure.reason,
                procedure.promoted_config_digest,
                procedure.promoted_lock_digest,
            ),
        )
        return procedure.procedure_id

    def get_procedure(self, procedure_id: str) -> ProcedureRecord | None:
        """Load one procedure by ID."""
        row = self._conn.execute(
            "SELECT * FROM procedures WHERE procedure_id = ?",
            (procedure_id,),
        ).fetchone()
        return None if row is None else self._row_to_procedure(row)

    def list_procedures(
        self,
        *,
        name: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureRecord]:
        """List procedure records with deterministic newest-first ordering."""
        clauses: list[str] = []
        params: list[Any] = []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM procedures{where} "
            "ORDER BY proposed_at DESC, procedure_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_procedure(row) for row in rows]

    def count_procedures(
        self,
        *,
        name: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count procedure records matching optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM procedures{where}",
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def transition_procedure(
        self,
        procedure_id: str,
        *,
        from_status: ProcedureStatus,
        to_status: ProcedureStatus,
        expected_version: int,
        resolved_actor_context: GovernedActorContext | None = None,
        resolved_at: str | None = None,
        retired_actor_context: GovernedActorContext | None = None,
        retired_at: str | None = None,
        reason: str | None = None,
        promoted_config_digest: str | None = None,
        promoted_lock_digest: str | None = None,
    ) -> bool:
        """Apply one optimistic lifecycle transition without changing definition data."""
        allowed_transitions = {
            ("pending", "live"),
            ("pending", "rejected"),
            ("live", "retired"),
        }
        if (from_status, to_status) not in allowed_transitions:
            raise ValueError(f"invalid procedure transition '{from_status}' -> '{to_status}'")
        if to_status in {"rejected", "retired"} and (reason is None or not reason.strip()):
            raise ValueError(f"procedure transition to '{to_status}' requires a reason")
        if to_status == "live" and (
            resolved_actor_context is None
            or promoted_config_digest is None
            or promoted_lock_digest is None
        ):
            raise ValueError(
                "procedure promotion requires reviewer attribution plus config and lock digests"
            )
        if to_status == "rejected" and resolved_actor_context is None:
            raise ValueError("procedure rejection requires reviewer attribution")
        if to_status == "retired" and retired_actor_context is None:
            raise ValueError("procedure retirement requires reviewer attribution")

        assignments = ["status = ?", "version = version + 1"]
        params: list[Any] = [to_status]
        if resolved_actor_context is not None:
            assignments.append("resolved_actor_context = ?")
            params.append(json.dumps(dump_actor_context(resolved_actor_context)))
        if resolved_at is not None:
            assignments.append("resolved_at = ?")
            params.append(resolved_at)
        if retired_actor_context is not None:
            assignments.append("retired_actor_context = ?")
            params.append(json.dumps(dump_actor_context(retired_actor_context)))
        if retired_at is not None:
            assignments.append("retired_at = ?")
            params.append(retired_at)
        if reason is not None:
            assignments.append("reason = ?")
            params.append(reason)
        if promoted_config_digest is not None:
            assignments.append("promoted_config_digest = ?")
            params.append(promoted_config_digest)
        if promoted_lock_digest is not None:
            assignments.append("promoted_lock_digest = ?")
            params.append(promoted_lock_digest)
        params.extend((procedure_id, from_status, expected_version))
        cursor = self._conn.execute(
            f"UPDATE procedures SET {', '.join(assignments)} "
            "WHERE procedure_id = ? AND status = ? AND version = ?",
            tuple(params),
        )
        return cursor.rowcount == 1

    def save_run(self, run: ProcedureRun) -> str:
        """Insert a crash-visible procedure run record. Does not commit."""
        self._conn.execute(
            "INSERT INTO procedure_runs "
            "(run_id, procedure_id, definition_digest, status, verdict, "
            "budget_spent_json, receipt_id, started_at, finalized_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.procedure_id,
                run.definition_digest,
                run.status,
                run.verdict,
                json.dumps(run.budget_spent.model_dump(mode="json"), sort_keys=True),
                run.receipt_id,
                format_datetime(run.started_at),
                _format_optional_datetime(run.finalized_at),
            ),
        )
        return run.run_id

    def finalize_run(
        self,
        run_id: str,
        *,
        verdict: ProcedureRunVerdict,
        budget_spent: ProcedureBudgetSpent,
        receipt_id: str,
        finalized_at: str,
    ) -> bool:
        """Finalize one started run exactly once. Does not commit."""
        cursor = self._conn.execute(
            "UPDATE procedure_runs SET status = 'finalized', verdict = ?, "
            "budget_spent_json = ?, receipt_id = ?, finalized_at = ? "
            "WHERE run_id = ? AND status = 'started' AND verdict IS NULL",
            (
                verdict,
                json.dumps(budget_spent.model_dump(mode="json"), sort_keys=True),
                receipt_id,
                finalized_at,
                run_id,
            ),
        )
        return cursor.rowcount == 1

    def get_run(self, run_id: str) -> ProcedureRun | None:
        """Load one procedure run by ID."""
        row = self._conn.execute(
            "SELECT * FROM procedure_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else self._row_to_run(row)

    def list_runs(
        self,
        *,
        procedure_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureRun]:
        """List procedure runs with deterministic newest-first ordering."""
        clauses: list[str] = []
        params: list[Any] = []
        if procedure_id is not None:
            clauses.append("procedure_id = ?")
            params.append(procedure_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM procedure_runs{where} "
            "ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def count_runs(
        self,
        *,
        procedure_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """Count procedure runs matching optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if procedure_id is not None:
            clauses.append("procedure_id = ?")
            params.append(procedure_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM procedure_runs{where}",
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def close(self) -> None:
        """Close an owned connection."""
        if self._owns_connection:
            self._conn.close()

    @staticmethod
    def _row_to_procedure(row: sqlite3.Row) -> ProcedureRecord:
        return ProcedureRecord(
            procedure_id=row["procedure_id"],
            definition=json.loads(row["definition_json"]),
            definition_digest=row["definition_digest"],
            status=row["status"],
            version=int(row["version"]),
            supersedes_procedure_id=row["supersedes_procedure_id"],
            evidence_refs=json.loads(row["evidence_refs_json"]),
            proposed_actor_context=load_actor_context(json.loads(row["proposed_actor_context"])),
            proposed_at=row["proposed_at"],
            resolved_actor_context=_load_optional_actor(row["resolved_actor_context"]),
            resolved_at=row["resolved_at"],
            retired_actor_context=_load_optional_actor(row["retired_actor_context"]),
            retired_at=row["retired_at"],
            reason=row["reason"],
            promoted_config_digest=row["promoted_config_digest"],
            promoted_lock_digest=row["promoted_lock_digest"],
        )

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> ProcedureRun:
        return ProcedureRun(
            run_id=row["run_id"],
            procedure_id=row["procedure_id"],
            definition_digest=row["definition_digest"],
            status=row["status"],
            verdict=row["verdict"],
            budget_spent=json.loads(row["budget_spent_json"]),
            receipt_id=row["receipt_id"],
            started_at=row["started_at"],
            finalized_at=row["finalized_at"],
        )


def _dump_optional_actor(actor: Any | None) -> str | None:
    return None if actor is None else json.dumps(dump_actor_context(actor))


def _load_optional_actor(value: str | None) -> Any | None:
    return None if value is None else load_actor_context(json.loads(value))


def _format_optional_datetime(value: Any | None) -> str | None:
    return None if value is None else format_datetime(value)


__all__ = ["ProcedureStore"]
