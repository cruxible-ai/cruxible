"""Append-only SQLite persistence for procedure outcome readings."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    load_actor_context,
)
from cruxible_core.instance_protocol import ProcedureReadingStoreProtocol
from cruxible_core.procedure.types import (
    LinkedOutcomeGradeSummary,
    LinkedOutcomeSummary,
    ProcedureReading,
    ProcedureRunFiredNode,
)
from cruxible_core.sqlite_ddl import execute_schema_script
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS procedure_readings (
    reading_id TEXT PRIMARY KEY,
    subject_grain TEXT NOT NULL
        CHECK (subject_grain IN ('procedure_unit', 'node', 'arm')),
    procedure_id TEXT NOT NULL,
    definition_digest TEXT,
    node_id TEXT,
    node_local_digest TEXT,
    from_node_id TEXT,
    from_node_local_digest TEXT,
    arm_label TEXT
        CHECK (arm_label IS NULL OR arm_label IN ('on_true', 'on_false')),
    arm_subtree_digest TEXT,
    parameter_pins_json TEXT NOT NULL DEFAULT '{}',
    grade TEXT NOT NULL CHECK (grade IN ('contract', 'attestation')),
    measurement_name TEXT,
    contract_id TEXT,
    resolution_id TEXT,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('satisfied', 'contradicted', 'indeterminate')),
    value_json TEXT,
    run_id TEXT,
    episode_ref TEXT,
    situation_shape_json TEXT,
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor_context_json TEXT NOT NULL,
    actor_org_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    note TEXT,
    idempotency_key TEXT,
    receipt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_procedure_readings_procedure
    ON procedure_readings(procedure_id, recorded_at DESC, reading_id DESC);
CREATE INDEX IF NOT EXISTS idx_procedure_readings_node
    ON procedure_readings(node_local_digest, grade, recorded_at DESC)
    WHERE node_local_digest IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_procedure_readings_arm
    ON procedure_readings(from_node_local_digest, node_local_digest, grade)
    WHERE from_node_local_digest IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_procedure_readings_contract
    ON procedure_readings(contract_id) WHERE contract_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_procedure_readings_idempotency
    ON procedure_readings(idempotency_key, procedure_id, actor_org_id, actor_id)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS procedure_run_fired_nodes (
    run_id TEXT NOT NULL REFERENCES procedure_runs(run_id),
    sequence INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_local_digest TEXT NOT NULL,
    node_subtree_digest TEXT NOT NULL,
    from_node_id TEXT,
    from_node_local_digest TEXT,
    arm_label TEXT,
    attempt_count INTEGER,
    PRIMARY KEY (run_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_procedure_run_fired_nodes_local
    ON procedure_run_fired_nodes(node_local_digest, run_id);
CREATE INDEX IF NOT EXISTS idx_procedure_run_fired_nodes_arm
    ON procedure_run_fired_nodes(from_node_local_digest, node_local_digest, run_id);
"""

_MAX_READING_IDS_PER_STATEMENT = 500


class ProcedureReadingStore(ProcedureReadingStoreProtocol):
    """Store immutable procedure readings in the unified state transaction."""

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

    def save_reading(self, reading: ProcedureReading) -> str:
        """Insert one reading without committing."""
        actor = reading.actor_context
        self._conn.execute(
            "INSERT INTO procedure_readings "
            "(reading_id, subject_grain, procedure_id, definition_digest, node_id, "
            "node_local_digest, from_node_id, from_node_local_digest, arm_label, "
            "arm_subtree_digest, parameter_pins_json, grade, measurement_name, "
            "contract_id, resolution_id, verdict, value_json, run_id, episode_ref, "
            "situation_shape_json, evidence_refs_json, observed_at, recorded_at, "
            "actor_context_json, actor_org_id, actor_id, note, idempotency_key, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                reading.reading_id,
                reading.subject_grain,
                reading.procedure_id,
                reading.definition_digest,
                reading.node_id,
                reading.node_local_digest,
                reading.from_node_id,
                reading.from_node_local_digest,
                reading.arm_label,
                reading.arm_subtree_digest,
                json.dumps(reading.parameter_pins, sort_keys=True),
                reading.grade,
                reading.measurement_name,
                reading.contract_id,
                reading.resolution_id,
                reading.verdict,
                _dump_optional_json(reading.value),
                reading.run_id,
                reading.episode_ref,
                _dump_optional_json(reading.situation_shape),
                json.dumps(
                    [ref.model_dump(mode="json") for ref in reading.evidence_refs],
                    sort_keys=True,
                ),
                format_datetime(reading.observed_at),
                format_datetime(reading.recorded_at),
                json.dumps(dump_actor_context(actor), sort_keys=True),
                actor.org_id,
                actor.actor_id,
                reading.note,
                reading.idempotency_key,
                reading.receipt_id,
            ),
        )
        return reading.reading_id

    def get_reading(self, reading_id: str) -> ProcedureReading | None:
        row = self._conn.execute(
            "SELECT * FROM procedure_readings WHERE reading_id = ?", (reading_id,)
        ).fetchone()
        return None if row is None else self._row_to_reading(row)

    def find_idempotent_reading(
        self,
        *,
        idempotency_key: str,
        procedure_id: str,
        actor_org_id: str,
        actor_id: str,
    ) -> ProcedureReading | None:
        row = self._conn.execute(
            "SELECT * FROM procedure_readings WHERE idempotency_key = ? AND procedure_id = ? "
            "AND actor_org_id = ? AND actor_id = ?",
            (idempotency_key, procedure_id, actor_org_id, actor_id),
        ).fetchone()
        return None if row is None else self._row_to_reading(row)

    def list_readings(
        self,
        *,
        procedure_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProcedureReading]:
        where = " WHERE procedure_id = ?" if procedure_id is not None else ""
        params: tuple[Any, ...] = (procedure_id,) if procedure_id is not None else ()
        rows = self._conn.execute(
            f"SELECT * FROM procedure_readings{where} "
            "ORDER BY recorded_at DESC, reading_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_reading(row) for row in rows]

    def save_fired_nodes(self, fired_nodes: Sequence[ProcedureRunFiredNode]) -> None:
        """Insert an invocation's ordered fired-node facts without committing."""
        self._conn.executemany(
            "INSERT INTO procedure_run_fired_nodes "
            "(run_id, sequence, node_id, node_local_digest, node_subtree_digest, "
            "from_node_id, from_node_local_digest, arm_label, attempt_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    node.run_id,
                    node.sequence,
                    node.node_id,
                    node.node_local_digest,
                    node.node_subtree_digest,
                    node.from_node_id,
                    node.from_node_local_digest,
                    node.arm_label,
                    node.attempt_count,
                )
                for node in fired_nodes
            ],
        )

    def list_run_fired_nodes(self, run_id: str) -> list[ProcedureRunFiredNode]:
        """Load every fired node for a run in execution order."""
        rows = self._conn.execute(
            "SELECT * FROM procedure_run_fired_nodes WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return [
            ProcedureRunFiredNode(
                run_id=row["run_id"],
                sequence=row["sequence"],
                node_id=row["node_id"],
                node_local_digest=row["node_local_digest"],
                node_subtree_digest=row["node_subtree_digest"],
                from_node_id=row["from_node_id"],
                from_node_local_digest=row["from_node_local_digest"],
                arm_label=row["arm_label"],
                attempt_count=row["attempt_count"],
            )
            for row in rows
        ]

    def get_linked_outcome_summaries(
        self,
        procedure_ids: Sequence[str],
    ) -> dict[str, LinkedOutcomeSummary]:
        """Aggregate both grades with one grouped statement per id chunk."""
        unique_ids = tuple(dict.fromkeys(procedure_ids))
        summaries: dict[str, LinkedOutcomeSummary] = {}
        for start in range(0, len(unique_ids), _MAX_READING_IDS_PER_STATEMENT):
            chunk = unique_ids[start : start + _MAX_READING_IDS_PER_STATEMENT]
            placeholders = ", ".join("?" for _ in chunk)
            rows = self._conn.execute(
                "SELECT procedure_id, grade, COUNT(*) AS readings, "
                "SUM(CASE WHEN verdict = 'satisfied' THEN 1 ELSE 0 END) AS satisfied, "
                "SUM(CASE WHEN verdict = 'contradicted' THEN 1 ELSE 0 END) "
                "AS contradicted, "
                "SUM(CASE WHEN verdict = 'indeterminate' THEN 1 ELSE 0 END) "
                "AS indeterminate "
                f"FROM procedure_readings WHERE procedure_id IN ({placeholders}) "
                "GROUP BY procedure_id, grade",
                chunk,
            ).fetchall()
            for row in rows:
                procedure_id = str(row["procedure_id"])
                grade_summary = LinkedOutcomeGradeSummary(
                    readings=int(row["readings"]),
                    satisfied=int(row["satisfied"]),
                    contradicted=int(row["contradicted"]),
                    indeterminate=int(row["indeterminate"]),
                )
                existing = summaries.get(procedure_id, LinkedOutcomeSummary())
                field = "contract_grade" if row["grade"] == "contract" else "attestation_grade"
                summaries[procedure_id] = existing.model_copy(update={field: grade_summary})
        return summaries

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    @staticmethod
    def _row_to_reading(row: sqlite3.Row) -> ProcedureReading:
        return ProcedureReading(
            reading_id=row["reading_id"],
            subject_grain=row["subject_grain"],
            procedure_id=row["procedure_id"],
            definition_digest=row["definition_digest"],
            node_id=row["node_id"],
            node_local_digest=row["node_local_digest"],
            from_node_id=row["from_node_id"],
            from_node_local_digest=row["from_node_local_digest"],
            arm_label=row["arm_label"],
            arm_subtree_digest=row["arm_subtree_digest"],
            parameter_pins=json.loads(row["parameter_pins_json"]),
            grade=row["grade"],
            measurement_name=row["measurement_name"],
            contract_id=row["contract_id"],
            resolution_id=row["resolution_id"],
            verdict=row["verdict"],
            value=_load_optional_json(row["value_json"]),
            run_id=row["run_id"],
            episode_ref=row["episode_ref"],
            situation_shape=_load_optional_json(row["situation_shape_json"]),
            evidence_refs=json.loads(row["evidence_refs_json"]),
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            actor_context=_load_required_actor_context(row["actor_context_json"]),
            note=row["note"],
            idempotency_key=row["idempotency_key"],
            receipt_id=row["receipt_id"],
        )


def _dump_optional_json(value: Any | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _load_optional_json(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


def _load_required_actor_context(value: str) -> GovernedActorContext:
    actor = load_actor_context(json.loads(value))
    if actor is None:
        raise ValueError("procedure reading actor context cannot be null")
    return actor


__all__ = ["ProcedureReadingStore"]
