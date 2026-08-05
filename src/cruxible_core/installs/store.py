"""SQLite persistence for the install ledger (migration 0005).

Three tables, one job each:

``installs``
    The authoritative row per install attempt: which artifact, which phase,
    who, when. ``phase`` is UPDATED in place — it is current state, and the
    audit trail of how it got there lives in the events table beside it.

``install_phase_events``
    Append-only phase history. Every advance writes one row with its receipt,
    so "when did this install become active, and on whose authority" is
    answerable without reading receipts back.

``install_owned_objects``
    The ownership record composition ownership cannot express: install →
    (kind, name) → the digest that install put there. The partial unique index
    on ``ownership_held`` is what makes "one live owner per object" a DATABASE
    guarantee rather than a service convention; two concurrent installs cannot
    both leave a claim on the same name behind, so the collision check and the
    insert cannot drift apart across connections.

``ownership_held`` is denormalized onto the owned-object row on purpose. The
predicate that matters ("does the owning install still hold its names?") is a
property of the INSTALL, but SQLite forbids subqueries and foreign-table
references in a partial index WHERE clause — so a flag maintained in the same
transaction as the phase change is the only way to get the uniqueness
guarantee out of the database instead of out of a convention.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from cruxible_core.governance.actors import dump_actor_context, load_actor_context
from cruxible_core.installs.types import (
    OWNERSHIP_HOLDING_PHASES,
    ArtifactRef,
    InstallPhase,
    InstallPhaseEvent,
    InstallRecord,
    ObjectReference,
    OwnedObject,
    OwnedObjectKind,
    OwnershipCollision,
)
from cruxible_core.instance_protocol import InstallLedgerStoreProtocol
from cruxible_core.sqlite_ddl import execute_schema_script

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS installs (
    install_id TEXT PRIMARY KEY,
    artifact_kind TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    artifact_version TEXT NOT NULL,
    artifact_digest TEXT NOT NULL,
    phase TEXT NOT NULL
        CHECK (phase IN (
            'preparing', 'pending_acceptance', 'active',
            'failed', 'rolling_back', 'rolled_back'
        )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor_context_json TEXT,
    failure_reason TEXT,
    receipt_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_installs_phase
    ON installs(phase, created_at DESC, install_id DESC);
CREATE INDEX IF NOT EXISTS idx_installs_artifact
    ON installs(artifact_kind, artifact_id, artifact_version);

CREATE TABLE IF NOT EXISTS install_phase_events (
    event_id TEXT PRIMARY KEY,
    install_id TEXT NOT NULL REFERENCES installs(install_id),
    sequence INTEGER NOT NULL,
    from_phase TEXT,
    to_phase TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor_context_json TEXT,
    reason TEXT,
    receipt_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_install_phase_events_sequence
    ON install_phase_events(install_id, sequence);

CREATE TABLE IF NOT EXISTS install_owned_objects (
    install_id TEXT NOT NULL REFERENCES installs(install_id),
    object_kind TEXT NOT NULL
        CHECK (object_kind IN ('contract', 'named_query', 'procedure', 'enum')),
    object_name TEXT NOT NULL,
    installed_digest TEXT NOT NULL,
    customized INTEGER NOT NULL DEFAULT 0,
    current_digest TEXT,
    references_json TEXT NOT NULL DEFAULT '[]',
    ownership_held INTEGER NOT NULL DEFAULT 1,
    recorded_at TEXT NOT NULL,
    receipt_id TEXT,
    PRIMARY KEY (install_id, object_kind, object_name)
);
CREATE INDEX IF NOT EXISTS idx_install_owned_objects_name
    ON install_owned_objects(object_kind, object_name);

-- One LIVE owner per (kind, name). Only a ROLLED_BACK install has released its
-- names (ownership_held cleared in the same transaction as the phase change).
-- A failed or rolling-back install still holds them: it may already have
-- written those objects and has to traverse rollback to take them back, so a
-- fresh install must not be able to claim them out from under the cleanup.
CREATE UNIQUE INDEX IF NOT EXISTS idx_install_owned_objects_live_owner
    ON install_owned_objects(object_kind, object_name)
    WHERE ownership_held = 1;
"""


class InstallLedgerStore(InstallLedgerStoreProtocol):
    """Store install records, their phase history, and their owned objects."""

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

    # -- installs ----------------------------------------------------------

    def save_install(self, install: InstallRecord) -> str:
        """Insert one install record without committing."""
        self._conn.execute(
            "INSERT INTO installs "
            "(install_id, artifact_kind, artifact_id, artifact_version, artifact_digest, "
            "phase, created_at, updated_at, actor_context_json, failure_reason, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                install.install_id,
                install.artifact.artifact_kind,
                install.artifact.artifact_id,
                install.artifact.artifact_version,
                install.artifact.artifact_digest,
                install.phase,
                install.created_at,
                install.updated_at,
                json.dumps(dump_actor_context(install.actor_context), sort_keys=True),
                install.failure_reason,
                install.receipt_id,
            ),
        )
        return install.install_id

    def get_install(self, install_id: str) -> InstallRecord | None:
        row = self._conn.execute(
            "SELECT * FROM installs WHERE install_id = ?", (install_id,)
        ).fetchone()
        return None if row is None else _row_to_install(row)

    def set_install_phase(
        self,
        install_id: str,
        *,
        phase: InstallPhase,
        updated_at: str,
        failure_reason: str | None,
        receipt_id: str | None,
    ) -> None:
        """Move one install to *phase*, releasing its names when it stops holding them.

        In practice that means: released on the commit that reaches
        ``rolled_back``, held in every other phase. Legality is the service's
        job, not this one's. The ownership release is NOT: it must be atomic
        with the phase change, or the unique index would keep refusing a
        re-install of an artifact whose previous attempt has finished rolling
        back.
        """
        self._conn.execute(
            "UPDATE installs SET phase = ?, updated_at = ?, failure_reason = ?, "
            "receipt_id = COALESCE(?, receipt_id) WHERE install_id = ?",
            (phase, updated_at, failure_reason, receipt_id, install_id),
        )
        self._conn.execute(
            "UPDATE install_owned_objects SET ownership_held = ? WHERE install_id = ?",
            (1 if phase in OWNERSHIP_HOLDING_PHASES else 0, install_id),
        )

    def list_installs(
        self,
        *,
        phase: InstallPhase | None = None,
        artifact_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[InstallRecord]:
        clause, params = _install_filter(phase, artifact_id)
        rows = self._conn.execute(
            f"SELECT * FROM installs{clause} ORDER BY created_at DESC, install_id DESC "
            "LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row_to_install(row) for row in rows]

    def count_installs(
        self,
        *,
        phase: InstallPhase | None = None,
        artifact_id: str | None = None,
    ) -> int:
        clause, params = _install_filter(phase, artifact_id)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS total FROM installs{clause}", params
        ).fetchone()
        return int(row["total"])

    # -- phase history -----------------------------------------------------

    def append_phase_event(self, event: InstallPhaseEvent) -> str:
        self._conn.execute(
            "INSERT INTO install_phase_events "
            "(event_id, install_id, sequence, from_phase, to_phase, occurred_at, "
            "actor_context_json, reason, receipt_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.install_id,
                event.sequence,
                event.from_phase,
                event.to_phase,
                event.occurred_at,
                json.dumps(dump_actor_context(event.actor_context), sort_keys=True),
                event.reason,
                event.receipt_id,
            ),
        )
        return event.event_id

    def next_phase_sequence(self, install_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS last FROM install_phase_events "
            "WHERE install_id = ?",
            (install_id,),
        ).fetchone()
        return int(row["last"]) + 1

    def list_phase_events(self, install_id: str) -> list[InstallPhaseEvent]:
        rows = self._conn.execute(
            "SELECT * FROM install_phase_events WHERE install_id = ? ORDER BY sequence",
            (install_id,),
        ).fetchall()
        return [_row_to_phase_event(row) for row in rows]

    # -- owned objects -----------------------------------------------------

    def save_owned_object(self, owned: OwnedObject, *, ownership_held: bool = True) -> None:
        self._conn.execute(
            "INSERT INTO install_owned_objects "
            "(install_id, object_kind, object_name, installed_digest, customized, "
            "current_digest, references_json, ownership_held, recorded_at, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owned.install_id,
                owned.object_kind,
                owned.object_name,
                owned.installed_digest,
                int(owned.customized),
                owned.current_digest,
                json.dumps(
                    [reference.model_dump(mode="json") for reference in owned.references],
                    sort_keys=True,
                ),
                int(ownership_held),
                owned.recorded_at,
                owned.receipt_id,
            ),
        )

    def set_owned_object_customization(
        self,
        install_id: str,
        *,
        object_kind: OwnedObjectKind,
        object_name: str,
        customized: bool,
        current_digest: str,
    ) -> None:
        self._conn.execute(
            "UPDATE install_owned_objects SET customized = ?, current_digest = ? "
            "WHERE install_id = ? AND object_kind = ? AND object_name = ?",
            (int(customized), current_digest, install_id, object_kind, object_name),
        )

    def get_owned_object(
        self,
        install_id: str,
        *,
        object_kind: OwnedObjectKind,
        object_name: str,
    ) -> OwnedObject | None:
        row = self._conn.execute(
            "SELECT * FROM install_owned_objects "
            "WHERE install_id = ? AND object_kind = ? AND object_name = ?",
            (install_id, object_kind, object_name),
        ).fetchone()
        return None if row is None else _row_to_owned_object(row)

    def list_owned_objects(self, install_id: str) -> list[OwnedObject]:
        rows = self._conn.execute(
            "SELECT * FROM install_owned_objects WHERE install_id = ? "
            "ORDER BY object_kind, object_name",
            (install_id,),
        ).fetchall()
        return [_row_to_owned_object(row) for row in rows]

    def find_live_owner(
        self,
        *,
        object_kind: OwnedObjectKind,
        object_name: str,
    ) -> OwnershipCollision | None:
        """Return the ownership-holding claim on (kind, name), if any."""
        row = self._conn.execute(
            "SELECT owned.install_id AS install_id, owned.installed_digest AS installed_digest, "
            "installs.phase AS phase FROM install_owned_objects AS owned "
            "JOIN installs ON installs.install_id = owned.install_id "
            "WHERE owned.object_kind = ? AND owned.object_name = ? "
            "AND owned.ownership_held = 1 LIMIT 1",
            (object_kind, object_name),
        ).fetchone()
        if row is None:
            return None
        return OwnershipCollision(
            object_kind=object_kind,
            object_name=object_name,
            owning_install_id=row["install_id"],
            owning_install_phase=row["phase"],
            installed_digest=row["installed_digest"],
        )

    def list_referencing_objects(
        self,
        *,
        exclude_install_id: str,
    ) -> list[tuple[OwnedObject, InstallPhase]]:
        """Return every ownership-holding owned object OUTSIDE one install.

        The reference filter is applied in Python because references are a JSON
        list; the row set is bounded by the number of installed objects, which
        is small by construction (a config's whole surface), so a scan here is
        honest rather than a lurking table scan on a growth table.
        """
        rows = self._conn.execute(
            "SELECT owned.*, installs.phase AS install_phase FROM install_owned_objects AS owned "
            "JOIN installs ON installs.install_id = owned.install_id "
            "WHERE owned.install_id != ? AND owned.ownership_held = 1 "
            "ORDER BY owned.install_id, owned.object_kind, owned.object_name",
            (exclude_install_id,),
        ).fetchall()
        return [(_row_to_owned_object(row), row["install_phase"]) for row in rows]

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()


def _install_filter(
    phase: InstallPhase | None,
    artifact_id: str | None,
) -> tuple[str, tuple[str, ...]]:
    conditions: list[str] = []
    params: list[str] = []
    if phase is not None:
        conditions.append("phase = ?")
        params.append(phase)
    if artifact_id is not None:
        conditions.append("artifact_id = ?")
        params.append(artifact_id)
    clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return clause, tuple(params)


def _row_to_install(row: sqlite3.Row) -> InstallRecord:
    return InstallRecord(
        install_id=row["install_id"],
        artifact=ArtifactRef(
            artifact_kind=row["artifact_kind"],
            artifact_id=row["artifact_id"],
            artifact_version=row["artifact_version"],
            artifact_digest=row["artifact_digest"],
        ),
        phase=row["phase"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        actor_context=load_actor_context(_load_json(row["actor_context_json"])),
        failure_reason=row["failure_reason"],
        receipt_id=row["receipt_id"],
    )


def _row_to_phase_event(row: sqlite3.Row) -> InstallPhaseEvent:
    return InstallPhaseEvent(
        event_id=row["event_id"],
        install_id=row["install_id"],
        sequence=int(row["sequence"]),
        from_phase=row["from_phase"],
        to_phase=row["to_phase"],
        occurred_at=row["occurred_at"],
        actor_context=load_actor_context(_load_json(row["actor_context_json"])),
        reason=row["reason"],
        receipt_id=row["receipt_id"],
    )


def _row_to_owned_object(row: sqlite3.Row) -> OwnedObject:
    raw_references = _load_json(row["references_json"])
    items: Sequence[object] = raw_references if isinstance(raw_references, list) else ()
    references = [ObjectReference.model_validate(item) for item in items]
    return OwnedObject(
        install_id=row["install_id"],
        object_kind=row["object_kind"],
        object_name=row["object_name"],
        installed_digest=row["installed_digest"],
        customized=bool(row["customized"]),
        current_digest=row["current_digest"],
        references=list(references),
        recorded_at=row["recorded_at"],
        receipt_id=row["receipt_id"],
    )


def _load_json(value: str | None) -> object:
    if value is None:
        return None
    return json.loads(value)


__all__ = ["InstallLedgerStore"]
