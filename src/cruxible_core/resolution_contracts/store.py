"""Append-only SQLite persistence for resolution contracts and their answers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cruxible_core.governance.actors import dump_actor_context, load_actor_context
from cruxible_core.instance_protocol import ResolutionContractStoreProtocol
from cruxible_core.resolution_contracts.types import (
    ContractActivation,
    ContractDeclaration,
    ContractResolution,
    ResolutionContract,
    ResolutionDisposition,
)
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS resolution_contracts (
    contract_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    subject_content_digest TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    measurement_kind TEXT NOT NULL CHECK (measurement_kind IN ('query', 'attestation')),
    check_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    actor_context_json TEXT NOT NULL,
    actor_org_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    idempotency_key TEXT,
    receipt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_resolution_contracts_subject
    ON resolution_contracts(entity_type, entity_id, opened_at DESC, contract_id DESC);
CREATE INDEX IF NOT EXISTS idx_resolution_contracts_clock
    ON resolution_contracts(check_at, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_contracts_idempotency
    ON resolution_contracts(
        idempotency_key, entity_type, entity_id, actor_org_id, actor_id
    )
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS resolution_contract_activations (
    activation_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL UNIQUE
        REFERENCES resolution_contracts(contract_id),
    acceptance_receipt_id TEXT,
    subject_content_digest TEXT NOT NULL,
    activated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolution_contract_resolutions (
    resolution_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES resolution_contracts(contract_id),
    sequence INTEGER NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('satisfied', 'contradicted', 'indeterminate')),
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    actor_context_json TEXT NOT NULL,
    note TEXT,
    resolving_query_receipt_id TEXT,
    resolving_attestation_ids_json TEXT NOT NULL DEFAULT '[]',
    receipt_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_contract_resolutions_sequence
    ON resolution_contract_resolutions(contract_id, sequence);

CREATE TABLE IF NOT EXISTS resolution_contract_dispositions (
    disposition_id TEXT PRIMARY KEY,
    resolution_id TEXT NOT NULL
        REFERENCES resolution_contract_resolutions(resolution_id),
    sequence INTEGER NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('upheld', 'overturned')),
    reviewer_actor_context_json TEXT NOT NULL,
    note TEXT,
    receipt_id TEXT,
    recorded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_contract_dispositions_sequence
    ON resolution_contract_dispositions(resolution_id, sequence);
"""


class ResolutionContractStore(ResolutionContractStoreProtocol):
    """Store immutable contracts, activations, resolutions, and dispositions."""

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

    # -- contracts ---------------------------------------------------------

    def save_contract(self, contract: ResolutionContract) -> str:
        """Insert one prepared contract without committing."""
        actor = contract.actor_context
        self._conn.execute(
            "INSERT INTO resolution_contracts "
            "(contract_id, entity_type, entity_id, subject_content_digest, "
            "declaration_json, measurement_kind, check_at, expires_at, opened_at, "
            "actor_context_json, actor_org_id, actor_id, idempotency_key, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                contract.contract_id,
                contract.entity_type,
                contract.entity_id,
                contract.subject_content_digest,
                json.dumps(
                    contract.declaration.model_dump(mode="json", exclude_none=True),
                    sort_keys=True,
                ),
                contract.declaration.measurement.kind,
                format_datetime(contract.declaration.check_at),
                format_datetime(contract.declaration.expires_at),
                format_datetime(contract.opened_at),
                json.dumps(dump_actor_context(actor), sort_keys=True),
                actor.org_id,
                actor.actor_id,
                contract.idempotency_key,
                contract.receipt_id,
            ),
        )
        return contract.contract_id

    def get_contract(self, contract_id: str) -> ResolutionContract | None:
        """Load one contract by ID."""
        row = self._conn.execute(
            "SELECT * FROM resolution_contracts WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        return None if row is None else self._row_to_contract(row)

    def find_idempotent_contract(
        self,
        *,
        idempotency_key: str,
        entity_type: str,
        entity_id: str,
        actor_org_id: str,
        actor_id: str,
    ) -> ResolutionContract | None:
        """Return the original contract for one scoped idempotency key."""
        row = self._conn.execute(
            "SELECT * FROM resolution_contracts "
            "WHERE idempotency_key = ? AND entity_type = ? AND entity_id = ? "
            "AND actor_org_id = ? AND actor_id = ?",
            (idempotency_key, entity_type, entity_id, actor_org_id, actor_id),
        ).fetchone()
        return None if row is None else self._row_to_contract(row)

    def list_contracts(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResolutionContract]:
        """List contracts with deterministic newest-first ordering."""
        where, params = self._subject_clause(entity_type, entity_id)
        rows = self._conn.execute(
            f"SELECT * FROM resolution_contracts{where} "
            "ORDER BY opened_at DESC, contract_id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [self._row_to_contract(row) for row in rows]

    def count_contracts(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> int:
        """Count contracts matching the optional subject coordinates."""
        where, params = self._subject_clause(entity_type, entity_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM resolution_contracts{where}",
            tuple(params),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def find_eligible_contracts(
        self,
        *,
        entity_type: str,
        entity_id: str,
        subject_content_digest: str,
        now: str,
    ) -> list[ResolutionContract]:
        """Return unexpired, never-activated contracts pinned to this content.

        Eligibility is the whole of the guard's satisfaction test, evaluated in
        SQL so the lookup and the activation that follows share one
        transaction. Ordering is oldest-first: the guard consumes the contract
        that has been waiting longest.
        """
        rows = self._conn.execute(
            "SELECT c.* FROM resolution_contracts c "
            "WHERE c.entity_type = ? AND c.entity_id = ? "
            "AND c.subject_content_digest = ? AND c.expires_at > ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM resolution_contract_activations a "
            "WHERE a.contract_id = c.contract_id"
            ") "
            "ORDER BY c.opened_at ASC, c.contract_id ASC",
            (entity_type, entity_id, subject_content_digest, now),
        ).fetchall()
        return [self._row_to_contract(row) for row in rows]

    # -- activations -------------------------------------------------------

    def save_activation(self, activation: ContractActivation) -> str:
        """Insert one activation without committing."""
        self._conn.execute(
            "INSERT INTO resolution_contract_activations "
            "(activation_id, contract_id, acceptance_receipt_id, "
            "subject_content_digest, activated_at) VALUES (?, ?, ?, ?, ?)",
            (
                activation.activation_id,
                activation.contract_id,
                activation.acceptance_receipt_id,
                activation.subject_content_digest,
                format_datetime(activation.activated_at),
            ),
        )
        return activation.activation_id

    def get_activations(self, contract_ids: Sequence[str]) -> dict[str, ContractActivation]:
        """Load activations for many contracts in one query."""
        if not contract_ids:
            return {}
        placeholders = ",".join("?" for _ in contract_ids)
        rows = self._conn.execute(
            f"SELECT * FROM resolution_contract_activations WHERE contract_id IN ({placeholders})",
            tuple(contract_ids),
        ).fetchall()
        return {row["contract_id"]: self._row_to_activation(row) for row in rows}

    # -- resolutions -------------------------------------------------------

    def save_resolution(self, resolution: ContractResolution) -> str:
        """Insert one resolution without committing."""
        self._conn.execute(
            "INSERT INTO resolution_contract_resolutions "
            "(resolution_id, contract_id, sequence, verdict, evidence_refs_json, "
            "observed_at, recorded_at, actor_context_json, note, "
            "resolving_query_receipt_id, resolving_attestation_ids_json, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resolution.resolution_id,
                resolution.contract_id,
                resolution.sequence,
                resolution.verdict,
                json.dumps(
                    [ref.model_dump(mode="json") for ref in resolution.evidence_refs],
                    sort_keys=True,
                ),
                format_datetime(resolution.observed_at),
                format_datetime(resolution.recorded_at),
                json.dumps(dump_actor_context(resolution.actor_context), sort_keys=True),
                resolution.note,
                resolution.resolving_query_receipt_id,
                json.dumps(list(resolution.resolving_attestation_ids), sort_keys=True),
                resolution.receipt_id,
            ),
        )
        return resolution.resolution_id

    def get_resolution(self, resolution_id: str) -> ContractResolution | None:
        """Load one resolution by ID."""
        row = self._conn.execute(
            "SELECT * FROM resolution_contract_resolutions WHERE resolution_id = ?",
            (resolution_id,),
        ).fetchone()
        return None if row is None else self._row_to_resolution(row)

    def list_resolutions(self, contract_id: str) -> list[ContractResolution]:
        """List one contract's resolution history, oldest sequence first."""
        rows = self._conn.execute(
            "SELECT * FROM resolution_contract_resolutions "
            "WHERE contract_id = ? ORDER BY sequence ASC",
            (contract_id,),
        ).fetchall()
        return [self._row_to_resolution(row) for row in rows]

    def get_latest_resolutions(
        self,
        contract_ids: Sequence[str],
    ) -> dict[str, ContractResolution]:
        """Load the highest-sequence resolution for many contracts in one query."""
        if not contract_ids:
            return {}
        placeholders = ",".join("?" for _ in contract_ids)
        rows = self._conn.execute(
            "WITH ranked AS ("
            "SELECT r.*, ROW_NUMBER() OVER ("
            "PARTITION BY contract_id ORDER BY sequence DESC"
            ") AS position FROM resolution_contract_resolutions r "
            f"WHERE contract_id IN ({placeholders})"
            ") SELECT * FROM ranked WHERE position = 1",
            tuple(contract_ids),
        ).fetchall()
        return {row["contract_id"]: self._row_to_resolution(row) for row in rows}

    # -- dispositions ------------------------------------------------------

    def save_disposition(self, disposition: ResolutionDisposition) -> str:
        """Insert one reviewer disposition without committing."""
        self._conn.execute(
            "INSERT INTO resolution_contract_dispositions "
            "(disposition_id, resolution_id, sequence, verdict, "
            "reviewer_actor_context_json, note, receipt_id, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                disposition.disposition_id,
                disposition.resolution_id,
                disposition.sequence,
                disposition.verdict,
                json.dumps(
                    dump_actor_context(disposition.reviewer_actor_context),
                    sort_keys=True,
                ),
                disposition.note,
                disposition.receipt_id,
                format_datetime(disposition.recorded_at),
            ),
        )
        return disposition.disposition_id

    def get_dispositions(
        self,
        resolution_ids: Sequence[str],
    ) -> dict[str, ResolutionDisposition]:
        """Load the STANDING (highest-sequence) disposition for many resolutions.

        Dispositions are latest-wins: a reviewer who mistakenly upheld a
        resolution supersedes that answer by recording another one, and every
        read of "the" disposition means the newest.
        """
        if not resolution_ids:
            return {}
        placeholders = ",".join("?" for _ in resolution_ids)
        rows = self._conn.execute(
            "WITH ranked AS ("
            "SELECT d.*, ROW_NUMBER() OVER ("
            "PARTITION BY resolution_id ORDER BY sequence DESC"
            ") AS position FROM resolution_contract_dispositions d "
            f"WHERE resolution_id IN ({placeholders})"
            ") SELECT * FROM ranked WHERE position = 1",
            tuple(resolution_ids),
        ).fetchall()
        return {row["resolution_id"]: self._row_to_disposition(row) for row in rows}

    def list_dispositions(self, resolution_id: str) -> list[ResolutionDisposition]:
        """List one resolution's full disposition history, oldest sequence first."""
        rows = self._conn.execute(
            "SELECT * FROM resolution_contract_dispositions "
            "WHERE resolution_id = ? ORDER BY sequence ASC",
            (resolution_id,),
        ).fetchall()
        return [self._row_to_disposition(row) for row in rows]

    # -- derived queues ----------------------------------------------------

    def list_activated_unresolved(
        self, *, before: str, use_expiry: bool
    ) -> list[ResolutionContract]:
        """Return activated contracts whose clock has passed and are unanswered.

        "Unanswered" means every resolution on the contract was overturned by
        its STANDING (highest-sequence) reviewer disposition — an overturn
        re-opens the contract, and a later disposition supersedes an earlier
        one. A contract carrying a standing resolution has been answered and
        must not keep nagging.
        """
        column = "expires_at" if use_expiry else "check_at"
        rows = self._conn.execute(
            "SELECT c.* FROM resolution_contracts c "
            "JOIN resolution_contract_activations a ON a.contract_id = c.contract_id "
            f"WHERE c.{column} <= ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM resolution_contract_resolutions r "
            "WHERE r.contract_id = c.contract_id "
            "AND COALESCE(("
            "SELECT d.verdict FROM resolution_contract_dispositions d "
            "WHERE d.resolution_id = r.resolution_id "
            "ORDER BY d.sequence DESC LIMIT 1"
            "), '') <> 'overturned'"
            ") "
            "ORDER BY c.check_at ASC, c.contract_id ASC",
            (before,),
        ).fetchall()
        return [self._row_to_contract(row) for row in rows]

    def list_undisposed_contradictions(self) -> list[tuple[ResolutionContract, ContractResolution]]:
        """Return standing contradicted resolutions with no reviewer disposition.

        "No disposition" means no disposition at all, not "no standing
        disposition": once a reviewer has answered, the attention is theirs to
        re-file, and a superseding disposition keeps the queue drained.
        """
        rows = self._conn.execute(
            "SELECT c.*, r.resolution_id AS r_resolution_id, r.contract_id AS r_contract_id, "
            "r.sequence AS r_sequence, r.verdict AS r_verdict, "
            "r.evidence_refs_json AS r_evidence_refs_json, r.observed_at AS r_observed_at, "
            "r.recorded_at AS r_recorded_at, r.actor_context_json AS r_actor_context_json, "
            "r.note AS r_note, "
            "r.resolving_query_receipt_id AS r_resolving_query_receipt_id, "
            "r.resolving_attestation_ids_json AS r_resolving_attestation_ids_json, "
            "r.receipt_id AS r_receipt_id "
            "FROM resolution_contract_resolutions r "
            "JOIN resolution_contracts c ON c.contract_id = r.contract_id "
            "WHERE r.verdict = 'contradicted' AND NOT EXISTS ("
            "SELECT 1 FROM resolution_contract_dispositions d "
            "WHERE d.resolution_id = r.resolution_id"
            ") "
            "ORDER BY r.recorded_at DESC, r.resolution_id DESC"
        ).fetchall()
        results: list[tuple[ResolutionContract, ContractResolution]] = []
        for row in rows:
            resolution = self._row_to_resolution(
                {
                    "resolution_id": row["r_resolution_id"],
                    "contract_id": row["r_contract_id"],
                    "sequence": row["r_sequence"],
                    "verdict": row["r_verdict"],
                    "evidence_refs_json": row["r_evidence_refs_json"],
                    "observed_at": row["r_observed_at"],
                    "recorded_at": row["r_recorded_at"],
                    "actor_context_json": row["r_actor_context_json"],
                    "note": row["r_note"],
                    "resolving_query_receipt_id": row["r_resolving_query_receipt_id"],
                    "resolving_attestation_ids_json": row["r_resolving_attestation_ids_json"],
                    "receipt_id": row["r_receipt_id"],
                }
            )
            results.append((self._row_to_contract(row), resolution))
        return results

    def close(self) -> None:
        """Close an owned connection."""
        if self._owns_connection:
            self._conn.close()

    # -- row mapping -------------------------------------------------------

    @staticmethod
    def _subject_clause(
        entity_type: str | None,
        entity_id: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    @staticmethod
    def _row_to_contract(row: Any) -> ResolutionContract:
        actor = load_actor_context(json.loads(row["actor_context_json"]))
        if actor is None:
            raise ValueError("stored resolution contract actor context is invalid")
        return ResolutionContract(
            contract_id=row["contract_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            subject_content_digest=row["subject_content_digest"],
            declaration=ContractDeclaration.model_validate(json.loads(row["declaration_json"])),
            opened_at=row["opened_at"],
            actor_context=actor,
            idempotency_key=row["idempotency_key"],
            receipt_id=row["receipt_id"],
        )

    @staticmethod
    def _row_to_activation(row: Any) -> ContractActivation:
        return ContractActivation(
            activation_id=row["activation_id"],
            contract_id=row["contract_id"],
            acceptance_receipt_id=row["acceptance_receipt_id"],
            subject_content_digest=row["subject_content_digest"],
            activated_at=row["activated_at"],
        )

    @staticmethod
    def _row_to_resolution(row: Any) -> ContractResolution:
        actor = load_actor_context(json.loads(row["actor_context_json"]))
        if actor is None:
            raise ValueError("stored resolution actor context is invalid")
        return ContractResolution(
            resolution_id=row["resolution_id"],
            contract_id=row["contract_id"],
            sequence=int(row["sequence"]),
            verdict=row["verdict"],
            evidence_refs=json.loads(row["evidence_refs_json"]),
            observed_at=row["observed_at"],
            recorded_at=row["recorded_at"],
            actor_context=actor,
            note=row["note"],
            resolving_query_receipt_id=row["resolving_query_receipt_id"],
            resolving_attestation_ids=json.loads(row["resolving_attestation_ids_json"]),
            receipt_id=row["receipt_id"],
        )

    @staticmethod
    def _row_to_disposition(row: Any) -> ResolutionDisposition:
        reviewer = load_actor_context(json.loads(row["reviewer_actor_context_json"]))
        if reviewer is None:
            raise ValueError("stored resolution disposition reviewer context is invalid")
        return ResolutionDisposition(
            disposition_id=row["disposition_id"],
            resolution_id=row["resolution_id"],
            sequence=int(row["sequence"]),
            verdict=row["verdict"],
            reviewer_actor_context=reviewer,
            note=row["note"],
            receipt_id=row["receipt_id"],
            recorded_at=row["recorded_at"],
        )


__all__ = ["ResolutionContractStore"]
