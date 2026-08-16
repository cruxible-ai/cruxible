"""SQLite reader for historical resolution-contract attestation evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.playbill.actor_context import load_actor_context
from cruxible_core.resolution_contracts.evidence import (
    LegacyResolutionAttestationV1,
    LegacyResolutionDispositionV1,
)


class LegacyResolutionEvidenceReader:
    """Read old rows for historical replay; expose no attestation mutation path."""

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._conn = connection if connection is not None else sqlite3.connect(str(db_path))
        self._owns_connection = connection is None
        self._conn.row_factory = sqlite3.Row

    def _has_table(self, name: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            is not None
        )

    def get_attestation(self, attestation_id: str) -> LegacyResolutionAttestationV1 | None:
        if not self._has_table("attestations"):
            return None
        row = self._conn.execute(
            "SELECT * FROM attestations WHERE attestation_id = ?",
            (attestation_id,),
        ).fetchone()
        return None if row is None else self._attestation(row)

    def get_latest_dispositions(
        self,
        attestation_ids: Sequence[str],
    ) -> dict[str, LegacyResolutionDispositionV1]:
        if not attestation_ids or not self._has_table("attestation_dispositions"):
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
        return {row["attestation_id"]: self._disposition(row) for row in rows}

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    @staticmethod
    def _attestation(row: sqlite3.Row) -> LegacyResolutionAttestationV1:
        actor = load_actor_context(json.loads(row["actor_context_json"]))
        if actor is None:
            raise ValueError("stored legacy attestation actor context is invalid")
        return LegacyResolutionAttestationV1(
            attestation_id=row["attestation_id"],
            relationship_type=row["relationship_type"],
            from_type=row["from_type"],
            from_id=row["from_id"],
            to_type=row["to_type"],
            to_id=row["to_id"],
            edge_key=row["edge_key"],
            claim_id=row["claim_id"] if "claim_id" in row.keys() else None,
            claim_content_digest=row["claim_content_digest"],
            claim_state_at_record=row["claim_state_at_record"],
            stance=row["stance"],
            evidence_refs=tuple(
                EvidenceRef.model_validate(item) for item in json.loads(row["evidence_refs_json"])
            ),
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            actor_context=actor,
        )

    @staticmethod
    def _disposition(row: sqlite3.Row) -> LegacyResolutionDispositionV1:
        reviewer = load_actor_context(json.loads(row["reviewer_actor_context_json"]))
        if reviewer is None:
            raise ValueError("stored legacy disposition actor context is invalid")
        return LegacyResolutionDispositionV1(
            disposition_id=row["disposition_id"],
            attestation_id=row["attestation_id"],
            verdict=row["verdict"],
            reviewer_actor_context=reviewer,
            recorded_at=row["recorded_at"],
        )


__all__ = ["LegacyResolutionEvidenceReader"]
