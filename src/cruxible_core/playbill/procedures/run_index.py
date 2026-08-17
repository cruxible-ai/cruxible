"""Disposable SQLite index rebuilt exclusively from verified Procedure exhaust."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.canonical import CanonicalValue, Sha256Value
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.errors import PlaybillExecutionError
from cruxible_core.playbill.exhaust import (
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)

IndexedProcedureRunStatusV1 = Literal[
    "running",
    "succeeded",
    "refused",
    "failed",
    "budget_exhausted",
]


class ProcedureRunIndexEntryV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    admission_binding_digest: str
    status: IndexedProcedureRunStatusV1
    first_sequence: int
    last_sequence: int
    final_payload_digest: str | None = None
    effect_intent_count: int = 0
    effect_result_count: int = 0

    @field_validator("admission_binding_digest", "final_payload_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value


_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS procedure_run_index (
    run_id TEXT PRIMARY KEY,
    admission_binding_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    first_sequence INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL,
    final_payload_digest TEXT,
    effect_intent_count INTEGER NOT NULL DEFAULT 0,
    effect_result_count INTEGER NOT NULL DEFAULT 0
)
"""


class ProcedureRunIndex:
    """A cache only: deleting the database and rebuilding must change no answer."""

    def __init__(self, path: Path) -> None:
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise PlaybillExecutionError("Procedure run index must be a regular file")
        if not path.parent.is_dir() or path.parent.is_symlink():
            raise PlaybillExecutionError("Procedure run index parent must be a regular directory")
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_INDEX_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get(self, run_id: str) -> ProcedureRunIndexEntryV1 | None:
        row = self._conn.execute(
            "SELECT * FROM procedure_run_index WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return ProcedureRunIndexEntryV1(
            run_id=row["run_id"],
            admission_binding_digest=row["admission_binding_digest"],
            status=row["status"],
            first_sequence=int(row["first_sequence"]),
            last_sequence=int(row["last_sequence"]),
            final_payload_digest=row["final_payload_digest"],
            effect_intent_count=int(row["effect_intent_count"]),
            effect_result_count=int(row["effect_result_count"]),
        )

    def apply_record(
        self,
        stored: StoredProcedureJournalRecordV1,
        *,
        payload: CanonicalValue,
    ) -> None:
        record = stored.record
        admission = record.admission_binding_digest
        if admission is None:
            raise PlaybillExecutionError("Procedure exhaust record lacks an admission binding")
        existing = self.get(record.run_id)
        if existing is None:
            if record.event_kind != "attempt_started":
                raise PlaybillExecutionError(
                    "Procedure run exhaust must begin with attempt_started"
                )
            self._conn.execute(
                "INSERT INTO procedure_run_index "
                "(run_id, admission_binding_digest, status, first_sequence, last_sequence) "
                "VALUES (?, ?, 'running', ?, ?)",
                (record.run_id, admission, record.sequence, record.sequence),
            )
        elif existing.admission_binding_digest != admission:
            raise PlaybillExecutionError("run_id collides across distinct admission bindings")
        elif existing.status != "running":
            raise PlaybillExecutionError("Procedure run exhaust continues after finalization")
        else:
            self._conn.execute(
                "UPDATE procedure_run_index SET last_sequence = ? WHERE run_id = ?",
                (record.sequence, record.run_id),
            )

        if record.event_kind == "effect_intent":
            self._conn.execute(
                "UPDATE procedure_run_index SET effect_intent_count = effect_intent_count + 1 "
                "WHERE run_id = ?",
                (record.run_id,),
            )
        elif record.event_kind == "effect_result":
            current = self.get(record.run_id)
            if current is None or current.effect_result_count >= current.effect_intent_count:
                raise PlaybillExecutionError("effect result has no unmatched durable intent")
            self._conn.execute(
                "UPDATE procedure_run_index SET effect_result_count = effect_result_count + 1 "
                "WHERE run_id = ?",
                (record.run_id,),
            )
        elif record.event_kind == "attempt_finalized":
            if not isinstance(payload, dict) or payload.get("status") not in {
                "succeeded",
                "refused",
                "failed",
                "budget_exhausted",
            }:
                raise PlaybillExecutionError("attempt-finalized payload has no valid status")
            self._conn.execute(
                "UPDATE procedure_run_index SET status = ?, final_payload_digest = ? "
                "WHERE run_id = ?",
                (payload["status"], record.payload_digest, record.run_id),
            )
        self._conn.commit()

    def rebuild(
        self,
        records: tuple[StoredProcedureJournalRecordV1, ...],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> None:
        """Reproduce the cache from authenticated records plus effective CAS coverage."""

        access = BodyAccessContext(principal_id="procedure-run-index", can_read_body=True)
        self._conn.execute("DELETE FROM procedure_run_index")
        self._conn.commit()
        for stored in records:
            content = bodies.read(stored.record.payload_digest, access=access)
            payload = parse_journal_payload(content)
            self.apply_record(stored, payload=payload)


__all__ = [
    "IndexedProcedureRunStatusV1",
    "ProcedureRunIndex",
    "ProcedureRunIndexEntryV1",
]
