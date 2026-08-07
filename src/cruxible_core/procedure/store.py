"""SQLite persistence for procedure definitions and invocation records.

The store participates in the unified ``state.db`` transaction. It never owns
commits when handed a unit-of-work connection, and it exposes no definition
update operation: a changed definition must be inserted as a new proposal.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast, get_args

from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    load_actor_context,
)
from cruxible_core.instance_protocol import ProcedureStoreProtocol
from cruxible_core.procedure.pins import AcceptanceNodePin
from cruxible_core.procedure.types import (
    ProcedureBudgetSpent,
    ProcedureEvidenceArtifact,
    ProcedureRecord,
    ProcedureRefusalReason,
    ProcedureRun,
    ProcedureRunVerdict,
    ProcedureStatus,
    ProcedureTrackRecord,
    compute_procedure_definition_digest,
)
from cruxible_core.sqlite_ddl import execute_schema_script
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
    acceptance_config_digest TEXT,
    acceptance_lock_digest TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedures_name ON procedures(name);
CREATE INDEX IF NOT EXISTS idx_procedures_status ON procedures(status);
CREATE INDEX IF NOT EXISTS idx_procedures_supersedes
    ON procedures(supersedes_procedure_id);

-- ``refusal_reason`` reaches already-populated databases through storage
-- migration 0005, not through this statement: rows finalized before it keep
-- NULL, because the bucket is derived from the branch that refuses and the
-- historical receipt text cannot be reclassified retroactively. Kept OUT of
-- the table body deliberately -- SQLite stores a CREATE TABLE verbatim,
-- comments included, and rebuilds the statement on ALTER TABLE, so a comment
-- next to the last column breaks any later DROP COLUMN.
CREATE TABLE IF NOT EXISTS procedure_runs (
    run_id TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL REFERENCES procedures(procedure_id),
    definition_digest TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'started',
    verdict TEXT,
    budget_spent_json TEXT NOT NULL DEFAULT '{}',
    receipt_id TEXT,
    started_at TEXT NOT NULL,
    finalized_at TEXT,
    refusal_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedure_runs_procedure
    ON procedure_runs(procedure_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_procedure_runs_status ON procedure_runs(status);
-- Covering index for the track-record aggregate: the grouped query reads only
-- these three columns, so a procedure list page never touches the run rows.
CREATE INDEX IF NOT EXISTS idx_procedure_runs_track_record
    ON procedure_runs(procedure_id, verdict, finalized_at);

CREATE TABLE IF NOT EXISTS procedure_evidence_artifacts (
    artifact_id TEXT PRIMARY KEY,
    content_digest TEXT NOT NULL UNIQUE,
    byte_count INTEGER NOT NULL,
    payload_json TEXT,
    truncated_head TEXT,
    oversized INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS procedure_run_evidence (
    run_id TEXT NOT NULL REFERENCES procedure_runs(run_id),
    output_alias TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES procedure_evidence_artifacts(artifact_id),
    receipt_id TEXT NOT NULL,
    PRIMARY KEY (run_id, output_alias)
);
CREATE INDEX IF NOT EXISTS idx_procedure_run_evidence_artifact
    ON procedure_run_evidence(artifact_id);

-- One row per (node, resolved dependency) as ACCEPTED. A pin is a payload plus
-- its digest, never a bare digest: a bare digest can be compared and cannot be
-- read, so a mismatch would say only "something changed" and a receipt carrying
-- one could not reconstruct the accepted world at all.
CREATE TABLE IF NOT EXISTS procedure_acceptance_node_pins (
    procedure_id TEXT NOT NULL REFERENCES procedures(procedure_id),
    node_id TEXT NOT NULL,
    pin_kind TEXT NOT NULL CHECK (pin_kind IN ('provider', 'query', 'parameter', 'artifact')),
    pin_key TEXT NOT NULL,
    pin_payload_json TEXT NOT NULL,
    pin_digest TEXT NOT NULL,
    PRIMARY KEY (procedure_id, node_id, pin_kind, pin_key)
);
CREATE INDEX IF NOT EXISTS idx_procedure_node_pins_key
    ON procedure_acceptance_node_pins(pin_kind, pin_key, pin_digest);
"""

_KNOWN_REFUSAL_REASONS = frozenset(get_args(ProcedureRefusalReason))
"""Refusal buckets this version understands, for reading a foreign database."""

_MAX_ID_PARAMETERS_PER_STATEMENT = 500
"""Ids bound into one ``IN (...)`` statement.

SQLite's compiled-in limit is 32766 host parameters and exceeding it raises
rather than degrading, so an unbounded ``IN`` list makes the caller's page size
a correctness constraint on the store. 500 keeps every statement far below the
cap while staying one round trip for any realistic procedure page.
"""


def _id_chunks(ids: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Split an id tuple into statement-sized chunks, preserving order."""
    size = _MAX_ID_PARAMETERS_PER_STATEMENT
    return [ids[start : start + size] for start in range(0, len(ids), size)]


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
            execute_schema_script(self._conn, _SCHEMA)

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
            "retired_at, reason, acceptance_config_digest, acceptance_lock_digest) "
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
                procedure.acceptance_config_digest,
                procedure.acceptance_lock_digest,
            ),
        )
        return procedure.procedure_id

    def save_acceptance_node_pins(self, pins: Sequence[AcceptanceNodePin]) -> int:
        """Write the accepted world for one procedure. Does not commit."""
        rows = [
            (
                pin.procedure_id,
                pin.node_id,
                pin.pin_kind,
                pin.pin_key,
                json.dumps(pin.pin_payload, sort_keys=True),
                pin.pin_digest,
            )
            for pin in pins
        ]
        self._conn.executemany(
            "INSERT OR REPLACE INTO procedure_acceptance_node_pins "
            "(procedure_id, node_id, pin_kind, pin_key, pin_payload_json, pin_digest) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        return len(rows)

    def list_acceptance_node_pins(self, procedure_id: str) -> list[AcceptanceNodePin]:
        """Return the accepted world for one procedure, in stable order."""
        rows = self._conn.execute(
            "SELECT * FROM procedure_acceptance_node_pins WHERE procedure_id = ? "
            "ORDER BY node_id, pin_kind, pin_key",
            (procedure_id,),
        ).fetchall()
        return [
            AcceptanceNodePin(
                procedure_id=row["procedure_id"],
                node_id=row["node_id"],
                pin_kind=row["pin_kind"],
                pin_key=row["pin_key"],
                pin_payload=json.loads(row["pin_payload_json"]),
                pin_digest=row["pin_digest"],
            )
            for row in rows
        ]

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
        acceptance_config_digest: str | None = None,
        acceptance_lock_digest: str | None = None,
    ) -> bool:
        """Apply one optimistic lifecycle transition without changing definition data."""
        allowed_transitions = {
            ("pending", "live"),
            ("pending", "rejected"),
            ("pending", "withdrawn"),
            ("live", "retired"),
        }
        if (from_status, to_status) not in allowed_transitions:
            raise ValueError(f"invalid procedure transition '{from_status}' -> '{to_status}'")
        if to_status in {"rejected", "retired"} and (reason is None or not reason.strip()):
            raise ValueError(f"procedure transition to '{to_status}' requires a reason")
        if to_status == "live" and (
            resolved_actor_context is None
            or acceptance_config_digest is None
            or acceptance_lock_digest is None
        ):
            raise ValueError(
                "procedure acceptance requires reviewer attribution plus config and lock digests"
            )
        if to_status == "rejected" and resolved_actor_context is None:
            raise ValueError("procedure rejection requires reviewer attribution")
        # A withdrawal carries no required reason -- an author retracting their
        # own proposal owes no verdict -- but it must still say WHO retracted it,
        # because that identity is exactly what authorized the transition.
        if to_status == "withdrawn" and resolved_actor_context is None:
            raise ValueError("procedure withdrawal requires author or reviewer attribution")
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
        if acceptance_config_digest is not None:
            assignments.append("acceptance_config_digest = ?")
            params.append(acceptance_config_digest)
        if acceptance_lock_digest is not None:
            assignments.append("acceptance_lock_digest = ?")
            params.append(acceptance_lock_digest)
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
            "budget_spent_json, receipt_id, started_at, finalized_at, refusal_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                run.refusal_reason,
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
        refusal_reason: ProcedureRefusalReason | None = None,
    ) -> bool:
        """Finalize one started run exactly once. Does not commit."""
        if refusal_reason is not None and verdict != "refused":
            raise ValueError("only a refused procedure run may record a refusal reason")
        cursor = self._conn.execute(
            "UPDATE procedure_runs SET status = 'finalized', verdict = ?, "
            "budget_spent_json = ?, receipt_id = ?, finalized_at = ?, refusal_reason = ? "
            "WHERE run_id = ? AND status = 'started' AND verdict IS NULL",
            (
                verdict,
                json.dumps(budget_spent.model_dump(mode="json"), sort_keys=True),
                receipt_id,
                finalized_at,
                refusal_reason,
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

    def get_run_track_records(
        self,
        procedure_ids: Sequence[str],
    ) -> dict[str, ProcedureTrackRecord]:
        """Aggregate run-ledger summaries for a whole procedure page.

        Two grouped statements per id chunk -- the verdict buckets, then the
        most-frequent refusal bucket -- rather than one statement per procedure.
        The id list is chunked because a caller may hand us a page far larger
        than SQLite's per-statement host-parameter cap, and blowing that cap is
        an outright error, not a slow path.
        """
        unique_ids = tuple(dict.fromkeys(procedure_ids))
        if not unique_ids:
            return {}
        records: dict[str, ProcedureTrackRecord] = {}
        for chunk in _id_chunks(unique_ids):
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT procedure_id, COUNT(*) AS runs, "
                "SUM(CASE WHEN verdict = 'succeeded' THEN 1 ELSE 0 END) AS succeeded, "
                "SUM(CASE WHEN verdict = 'failed' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN verdict = 'refused' THEN 1 ELSE 0 END) AS refused, "
                "SUM(CASE WHEN verdict = 'budget_exceeded' THEN 1 ELSE 0 END) "
                "AS budget_exceeded, "
                "SUM(CASE WHEN verdict IS NULL THEN 1 ELSE 0 END) AS in_flight, "
                "MAX(CASE WHEN verdict = 'succeeded' THEN finalized_at END) "
                "AS last_succeeded_at "
                f"FROM procedure_runs WHERE procedure_id IN ({placeholders}) "
                "GROUP BY procedure_id",
                chunk,
            ).fetchall()
            top_reasons = self._top_refusal_reasons(chunk, placeholders)
            for row in rows:
                procedure_id = str(row["procedure_id"])
                records[procedure_id] = ProcedureTrackRecord(
                    runs=int(row["runs"]),
                    succeeded=int(row["succeeded"]),
                    failed=int(row["failed"]),
                    refused=int(row["refused"]),
                    budget_exceeded=int(row["budget_exceeded"]),
                    in_flight=int(row["in_flight"]),
                    last_succeeded_at=row["last_succeeded_at"],
                    top_refusal_reason=top_reasons.get(procedure_id),
                    linked_outcomes=None,
                )
        return records

    def _top_refusal_reasons(
        self,
        chunk: tuple[str, ...],
        placeholders: str,
    ) -> dict[str, ProcedureRefusalReason]:
        """Most-frequent recorded refusal bucket per procedure in one chunk.

        Ties break on the bucket name so the surface is deterministic rather
        than dependent on scan order. Runs refused before the column existed
        carry no bucket and are excluded outright: counting them as a shared
        "unknown" bucket would let history outvote every reason actually
        observed since.

        Buckets this version does not recognize are skipped for the same
        reason a null is -- a database written by a newer version must degrade
        to a less specific summary, never fail the whole procedure listing over
        an advisory count.
        """
        rows = self._conn.execute(
            "SELECT procedure_id, refusal_reason, COUNT(*) AS reason_count "
            f"FROM procedure_runs WHERE procedure_id IN ({placeholders}) "
            "AND verdict = 'refused' AND refusal_reason IS NOT NULL "
            "GROUP BY procedure_id, refusal_reason "
            "ORDER BY procedure_id ASC, reason_count DESC, refusal_reason ASC",
            chunk,
        ).fetchall()
        top: dict[str, ProcedureRefusalReason] = {}
        for row in rows:
            reason = str(row["refusal_reason"])
            if reason not in _KNOWN_REFUSAL_REASONS:
                continue
            top.setdefault(str(row["procedure_id"]), cast(ProcedureRefusalReason, reason))
        return top

    def save_evidence_artifact(self, artifact: ProcedureEvidenceArtifact) -> str:
        """Persist digest-addressed typed JSON content without committing."""
        self._conn.execute(
            "INSERT OR IGNORE INTO procedure_evidence_artifacts "
            "(artifact_id, content_digest, byte_count, payload_json, truncated_head, "
            "oversized, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.content_digest,
                artifact.byte_count,
                (None if artifact.oversized else json.dumps(artifact.payload, sort_keys=True)),
                artifact.truncated_head,
                int(artifact.oversized),
                format_datetime(artifact.created_at),
            ),
        )
        return artifact.artifact_id

    def link_run_evidence(
        self,
        *,
        run_id: str,
        output_alias: str,
        artifact_id: str,
        receipt_id: str,
    ) -> None:
        """Link one declared output to its finalized run receipt."""
        self._conn.execute(
            "INSERT INTO procedure_run_evidence "
            "(run_id, output_alias, artifact_id, receipt_id) VALUES (?, ?, ?, ?)",
            (run_id, output_alias, artifact_id, receipt_id),
        )

    def get_evidence_artifact(
        self,
        artifact_id: str,
    ) -> ProcedureEvidenceArtifact | None:
        """Load one whole chunkless typed JSON artifact."""
        row = self._conn.execute(
            "SELECT * FROM procedure_evidence_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        oversized = bool(row["oversized"])
        return ProcedureEvidenceArtifact(
            artifact_id=row["artifact_id"],
            content_digest=row["content_digest"],
            byte_count=int(row["byte_count"]),
            payload=None if oversized else json.loads(row["payload_json"]),
            truncated_head=row["truncated_head"],
            oversized=oversized,
            created_at=row["created_at"],
        )

    def list_run_evidence_refs(self, run_id: str) -> list[Any]:
        """Return ready-made refs in declaration order for one run."""
        from cruxible_core.graph.evidence import EvidenceRef

        rows = self._conn.execute(
            "SELECT e.output_alias, e.artifact_id, e.receipt_id, "
            "a.content_digest, a.byte_count, a.oversized "
            "FROM procedure_run_evidence e "
            "JOIN procedure_evidence_artifacts a ON a.artifact_id = e.artifact_id "
            "WHERE e.run_id = ? ORDER BY e.rowid",
            (run_id,),
        ).fetchall()
        return [
            EvidenceRef(
                source="procedure_run",
                source_record_id=run_id,
                artifact_id=row["artifact_id"],
                label=row["output_alias"],
                metadata={
                    "receipt_id": row["receipt_id"],
                    "content_digest": row["content_digest"],
                    "byte_count": int(row["byte_count"]),
                    "oversized": bool(row["oversized"]),
                },
            )
            for row in rows
        ]

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
            acceptance_config_digest=row["acceptance_config_digest"],
            acceptance_lock_digest=row["acceptance_lock_digest"],
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
            refusal_reason=row["refusal_reason"],
        )


def _dump_optional_actor(actor: Any | None) -> str | None:
    return None if actor is None else json.dumps(dump_actor_context(actor))


def _load_optional_actor(value: str | None) -> Any | None:
    return None if value is None else load_actor_context(json.loads(value))


def _format_optional_datetime(value: Any | None) -> str | None:
    return None if value is None else format_datetime(value)


__all__ = ["ProcedureStore"]
