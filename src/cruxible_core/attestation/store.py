"""Append-only SQLite persistence for attestations and dispositions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cruxible_core.attestation.types import (
    AttestationDisposition,
    AttestationRecord,
    AttestationStance,
    ClaimKey,
    CorroborationSummary,
    StaleContentSummary,
)
from cruxible_core.governance.actors import dump_actor_context, load_actor_context
from cruxible_core.instance_protocol import AttestationStoreProtocol
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS attestations (
    attestation_id TEXT PRIMARY KEY,
    relationship_type TEXT NOT NULL,
    from_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    edge_key INTEGER,
    claim_content_digest TEXT NOT NULL,
    claim_state_at_record TEXT NOT NULL,
    stance TEXT NOT NULL CHECK (stance IN ('support', 'contradict', 'unsure')),
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
CREATE INDEX IF NOT EXISTS idx_attestations_claim
    ON attestations(
        relationship_type, from_type, from_id, to_type, to_id, recorded_at
    );
CREATE INDEX IF NOT EXISTS idx_attestations_stance
    ON attestations(stance, recorded_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attestations_idempotency
    ON attestations(
        idempotency_key, relationship_type, from_type, from_id, to_type, to_id,
        actor_org_id, actor_id
    )
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS attestation_dispositions (
    disposition_id TEXT PRIMARY KEY,
    attestation_id TEXT NOT NULL REFERENCES attestations(attestation_id),
    verdict TEXT NOT NULL CHECK (verdict IN ('upheld', 'corrected', 'invalidated')),
    reviewer_actor_context_json TEXT NOT NULL,
    note TEXT,
    follow_up_receipt_id TEXT,
    receipt_id TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attestation_dispositions_target
    ON attestation_dispositions(attestation_id, recorded_at DESC, disposition_id DESC);
"""


class AttestationStore(AttestationStoreProtocol):
    """Store immutable observation and reviewer-answer records."""

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

    def save_attestation(self, record: AttestationRecord) -> str:
        """Insert one attestation without committing."""
        actor = record.actor_context
        self._conn.execute(
            "INSERT INTO attestations "
            "(attestation_id, relationship_type, from_type, from_id, to_type, to_id, "
            "edge_key, claim_content_digest, claim_state_at_record, stance, "
            "evidence_refs_json, observed_at, recorded_at, actor_context_json, "
            "actor_org_id, actor_id, note, idempotency_key, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.attestation_id,
                record.relationship_type,
                record.from_type,
                record.from_id,
                record.to_type,
                record.to_id,
                record.edge_key,
                record.claim_content_digest,
                record.claim_state_at_record,
                record.stance,
                json.dumps(
                    [ref.model_dump(mode="json") for ref in record.evidence_refs],
                    sort_keys=True,
                ),
                format_datetime(record.observed_at),
                format_datetime(record.recorded_at),
                json.dumps(dump_actor_context(actor), sort_keys=True),
                actor.org_id,
                actor.actor_id,
                record.note,
                record.idempotency_key,
                record.receipt_id,
            ),
        )
        return record.attestation_id

    def get_attestation(self, attestation_id: str) -> AttestationRecord | None:
        """Load one attestation by ID."""
        row = self._conn.execute(
            "SELECT * FROM attestations WHERE attestation_id = ?",
            (attestation_id,),
        ).fetchone()
        return None if row is None else self._row_to_attestation(row)

    def find_idempotent_attestation(
        self,
        *,
        idempotency_key: str,
        claim_key: ClaimKey,
        actor_org_id: str,
        actor_id: str,
    ) -> AttestationRecord | None:
        """Return the original record for one scoped idempotency key."""
        relationship_type, from_type, from_id, to_type, to_id = claim_key
        row = self._conn.execute(
            "SELECT * FROM attestations "
            "WHERE idempotency_key = ? AND relationship_type = ? "
            "AND from_type = ? AND from_id = ? AND to_type = ? AND to_id = ? "
            "AND actor_org_id = ? AND actor_id = ?",
            (
                idempotency_key,
                relationship_type,
                from_type,
                from_id,
                to_type,
                to_id,
                actor_org_id,
                actor_id,
            ),
        ).fetchone()
        return None if row is None else self._row_to_attestation(row)

    def list_attestations(
        self,
        *,
        claim_key: ClaimKey | None = None,
        stance: AttestationStance | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AttestationRecord]:
        """List records with deterministic newest-first ordering."""
        clauses: list[str] = []
        params: list[Any] = []
        if claim_key is not None:
            clauses.extend(
                [
                    "relationship_type = ?",
                    "from_type = ?",
                    "from_id = ?",
                    "to_type = ?",
                    "to_id = ?",
                ]
            )
            params.extend(claim_key)
        if stance is not None:
            clauses.append("stance = ?")
            params.append(stance)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM attestations{where} "
            "ORDER BY recorded_at DESC, attestation_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_attestation(row) for row in rows]

    def count_attestations(
        self,
        *,
        claim_key: ClaimKey | None = None,
        stance: AttestationStance | None = None,
    ) -> int:
        """Count records matching the optional tuple and stance."""
        clauses: list[str] = []
        params: list[Any] = []
        if claim_key is not None:
            clauses.extend(
                [
                    "relationship_type = ?",
                    "from_type = ?",
                    "from_id = ?",
                    "to_type = ?",
                    "to_id = ?",
                ]
            )
            params.extend(claim_key)
        if stance is not None:
            clauses.append("stance = ?")
            params.append(stance)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM attestations{where}",
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def save_disposition(self, disposition: AttestationDisposition) -> str:
        """Insert one disposition without committing."""
        self._conn.execute(
            "INSERT INTO attestation_dispositions "
            "(disposition_id, attestation_id, verdict, reviewer_actor_context_json, "
            "note, follow_up_receipt_id, receipt_id, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disposition.disposition_id,
                disposition.attestation_id,
                disposition.verdict,
                json.dumps(
                    dump_actor_context(disposition.reviewer_actor_context),
                    sort_keys=True,
                ),
                disposition.note,
                disposition.follow_up_receipt_id,
                disposition.receipt_id,
                format_datetime(disposition.recorded_at),
            ),
        )
        return disposition.disposition_id

    def list_dispositions(
        self,
        *,
        attestation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AttestationDisposition]:
        """List disposition history newest first."""
        where = " WHERE attestation_id = ?" if attestation_id is not None else ""
        params: tuple[Any, ...] = (attestation_id,) if attestation_id is not None else ()
        rows = self._conn.execute(
            f"SELECT * FROM attestation_dispositions{where} "
            "ORDER BY recorded_at DESC, disposition_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_disposition(row) for row in rows]

    def get_latest_dispositions(
        self,
        attestation_ids: Sequence[str],
    ) -> dict[str, AttestationDisposition]:
        """Load the latest disposition for every requested attestation in one query."""
        if not attestation_ids:
            return {}
        placeholders = ",".join("?" for _ in attestation_ids)
        rows = self._conn.execute(
            "WITH ranked AS ("
            "SELECT d.*, ROW_NUMBER() OVER ("
            "PARTITION BY attestation_id ORDER BY recorded_at DESC, disposition_id DESC"
            ") AS position FROM attestation_dispositions d "
            f"WHERE attestation_id IN ({placeholders})"
            ") SELECT * FROM ranked WHERE position = 1",
            tuple(attestation_ids),
        ).fetchall()
        return {row["attestation_id"]: self._row_to_disposition(row) for row in rows}

    def summaries_for_claims(
        self,
        claim_digests: Mapping[ClaimKey, str],
    ) -> dict[ClaimKey, CorroborationSummary]:
        """Aggregate all requested claim tuples with one indexed SQL query."""
        if not claim_digests:
            return {}
        tuple_placeholders = ",".join("(?, ?, ?, ?, ?)" for _ in claim_digests)
        params = tuple(part for key in claim_digests for part in key)
        rows = self._conn.execute(
            "WITH latest_dispositions AS ("
            "SELECT d.*, ROW_NUMBER() OVER ("
            "PARTITION BY attestation_id ORDER BY recorded_at DESC, disposition_id DESC"
            ") AS position FROM attestation_dispositions d"
            ") "
            "SELECT a.*, d.verdict AS latest_verdict "
            "FROM attestations a "
            "LEFT JOIN latest_dispositions d "
            "ON d.attestation_id = a.attestation_id AND d.position = 1 "
            "WHERE (a.relationship_type, a.from_type, a.from_id, a.to_type, a.to_id) "
            f"IN ({tuple_placeholders}) "
            "ORDER BY a.recorded_at, a.attestation_id",
            params,
        ).fetchall()
        accumulators: dict[ClaimKey, dict[str, Any]] = {}
        for row in rows:
            key: ClaimKey = (
                row["relationship_type"],
                row["from_type"],
                row["from_id"],
                row["to_type"],
                row["to_id"],
            )
            current_digest = claim_digests[key]
            acc = accumulators.setdefault(
                key,
                {
                    "support_count": 0,
                    "contradict_count": 0,
                    "unsure_count": 0,
                    "invalidated_count": 0,
                    "last_supported_at": None,
                    "last_contradicted_at": None,
                    "actors": set(),
                    "open_contradiction": False,
                    "stale": {
                        "support_count": 0,
                        "contradict_count": 0,
                        "unsure_count": 0,
                        "invalidated_count": 0,
                    },
                },
            )
            stale = row["claim_content_digest"] != current_digest
            invalidated = row["latest_verdict"] == "invalidated"
            stance = row["stance"]
            if stale:
                stale_bucket = acc["stale"]
                stale_bucket["invalidated_count" if invalidated else f"{stance}_count"] += 1
                continue
            if invalidated:
                acc["invalidated_count"] += 1
                continue
            acc[f"{stance}_count"] += 1
            acc["actors"].add((row["actor_org_id"], row["actor_id"]))
            if stance == "support":
                acc["last_supported_at"] = _max_timestamp(
                    acc["last_supported_at"], row["observed_at"]
                )
            elif stance == "contradict":
                acc["last_contradicted_at"] = _max_timestamp(
                    acc["last_contradicted_at"], row["observed_at"]
                )
                if row["latest_verdict"] is None:
                    acc["open_contradiction"] = True

        return {
            key: CorroborationSummary(
                support_count=acc["support_count"],
                contradict_count=acc["contradict_count"],
                unsure_count=acc["unsure_count"],
                invalidated_count=acc["invalidated_count"],
                last_supported_at=acc["last_supported_at"],
                last_contradicted_at=acc["last_contradicted_at"],
                distinct_actor_count=len(acc["actors"]),
                open_contradiction=acc["open_contradiction"],
                stale_content=StaleContentSummary(**acc["stale"]),
            )
            for key, acc in accumulators.items()
        }

    def list_open_contradictions(self) -> list[AttestationRecord]:
        """Load contradiction records with no disposition in one query."""
        rows = self._conn.execute(
            "SELECT a.* FROM attestations a "
            "WHERE a.stance = 'contradict' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM attestation_dispositions d "
            "WHERE d.attestation_id = a.attestation_id"
            ") "
            "ORDER BY a.observed_at DESC, a.attestation_id DESC"
        ).fetchall()
        return [self._row_to_attestation(row) for row in rows]

    def close(self) -> None:
        """Close an owned connection."""
        if self._owns_connection:
            self._conn.close()

    @staticmethod
    def _row_to_attestation(row: sqlite3.Row) -> AttestationRecord:
        actor = load_actor_context(json.loads(row["actor_context_json"]))
        if actor is None:
            raise ValueError("stored attestation actor context is invalid")
        return AttestationRecord(
            attestation_id=row["attestation_id"],
            relationship_type=row["relationship_type"],
            from_type=row["from_type"],
            from_id=row["from_id"],
            to_type=row["to_type"],
            to_id=row["to_id"],
            edge_key=row["edge_key"],
            claim_content_digest=row["claim_content_digest"],
            claim_state_at_record=row["claim_state_at_record"],
            stance=row["stance"],
            evidence_refs=json.loads(row["evidence_refs_json"]),
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            actor_context=actor,
            note=row["note"],
            idempotency_key=row["idempotency_key"],
            receipt_id=row["receipt_id"],
        )

    @staticmethod
    def _row_to_disposition(row: sqlite3.Row) -> AttestationDisposition:
        reviewer = load_actor_context(json.loads(row["reviewer_actor_context_json"]))
        if reviewer is None:
            raise ValueError("stored disposition reviewer actor context is invalid")
        return AttestationDisposition(
            disposition_id=row["disposition_id"],
            attestation_id=row["attestation_id"],
            verdict=row["verdict"],
            reviewer_actor_context=reviewer,
            note=row["note"],
            follow_up_receipt_id=row["follow_up_receipt_id"],
            receipt_id=row["receipt_id"],
            recorded_at=row["recorded_at"],
        )


def _max_timestamp(current: str | None, candidate: str) -> str:
    if current is None or candidate > current:
        return candidate
    return current


__all__ = ["AttestationStore"]
