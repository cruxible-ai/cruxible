"""SQLite persistence for the compute-slot binding ledger.

Two tables, one invariant. ``slot_bindings`` holds the CURRENT row per binding;
``slot_binding_revisions`` is append-only and holds every revision that row has
ever had, including the current one. History is therefore readable without
reconstructing it from receipts, and a rebind never destroys the record of what
the install was running on before.

THE PARTIAL UNIQUE INDEX IS THE INVARIANT. "One active binding per slot per
install" is enforced by ``idx_slot_bindings_active`` at the database, not by a
read-then-write in the service: two concurrent binds against the same slot on
separate connections cannot both leave an active row behind, so the duplicate
check and the insert cannot drift apart. The service's own check produces the
good error message; the index is what makes the guarantee true.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from cruxible_core.bindings.types import (
    BindingStatus,
    SlotBinding,
    SlotBindingRevision,
)
from cruxible_core.governance.actors import (
    GovernedActorContext,
    dump_actor_context,
    load_actor_context,
)
from cruxible_core.primitives import canonical_json
from cruxible_core.sqlite_ddl import execute_schema_script
from cruxible_core.temporal import format_datetime, parse_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS slot_bindings (
    binding_id TEXT PRIMARY KEY,
    install_id TEXT NOT NULL,
    slot_name TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    contract_in TEXT NOT NULL,
    contract_out TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    third_party_consent INTEGER NOT NULL DEFAULT 0,
    consent_actor_id TEXT,
    consent_org_id TEXT,
    consent_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    bound_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT,
    actor_context_json TEXT,
    receipt_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_bindings_active
    ON slot_bindings(install_id, slot_name) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_slot_bindings_install
    ON slot_bindings(install_id, slot_name, status);
CREATE INDEX IF NOT EXISTS idx_slot_bindings_provider
    ON slot_bindings(provider_name);

CREATE TABLE IF NOT EXISTS slot_binding_revisions (
    binding_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    change_kind TEXT NOT NULL
        CHECK (change_kind IN ('bind', 'rebind', 'retire')),
    install_id TEXT NOT NULL,
    slot_name TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    contract_in TEXT NOT NULL,
    contract_out TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    third_party_consent INTEGER NOT NULL DEFAULT 0,
    consent_actor_id TEXT,
    consent_org_id TEXT,
    consent_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    note TEXT,
    recorded_at TEXT NOT NULL,
    actor_context_json TEXT,
    receipt_id TEXT,
    PRIMARY KEY (binding_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_slot_binding_revisions_slot
    ON slot_binding_revisions(install_id, slot_name, revision);
"""


class BindingStoreProtocol(ABC):
    """Interface for compute-slot binding persistence."""

    @abstractmethod
    def save_binding(self, binding: SlotBinding) -> str: ...
    @abstractmethod
    def update_binding(self, binding: SlotBinding) -> None: ...
    @abstractmethod
    def save_revision(self, revision: SlotBindingRevision) -> None: ...
    @abstractmethod
    def get_binding(self, binding_id: str) -> SlotBinding | None: ...
    @abstractmethod
    def get_active_binding(self, *, install_id: str, slot_name: str) -> SlotBinding | None: ...
    @abstractmethod
    def list_bindings(
        self,
        *,
        install_id: str | None = None,
        slot_name: str | None = None,
        status: BindingStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SlotBinding]: ...
    @abstractmethod
    def count_bindings(
        self,
        *,
        install_id: str | None = None,
        slot_name: str | None = None,
        status: BindingStatus | None = None,
    ) -> int: ...
    @abstractmethod
    def list_revisions(
        self,
        binding_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SlotBindingRevision]: ...
    @abstractmethod
    def count_revisions(self, binding_id: str) -> int: ...
    @abstractmethod
    def close(self) -> None: ...


class BindingStore(BindingStoreProtocol):
    """SQLite-backed ledger of compute-slot bindings and their revisions."""

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

    # -- writes ------------------------------------------------------------

    def save_binding(self, binding: SlotBinding) -> str:
        """Insert one new binding row without committing.

        Raises ``sqlite3.IntegrityError`` when the slot already carries an
        active binding — that is the partial unique index doing its job.
        """
        self._conn.execute(
            "INSERT INTO slot_bindings "
            "(binding_id, install_id, slot_name, provider_name, contract_in, "
            "contract_out, billing_mode, third_party_consent, consent_actor_id, "
            "consent_org_id, consent_at, revision, status, bound_at, updated_at, "
            "retired_at, actor_context_json, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding.binding_id,
                binding.install_id,
                binding.slot_name,
                binding.provider_name,
                binding.contract_in,
                binding.contract_out,
                binding.billing_mode,
                int(binding.third_party_consent),
                binding.consent_actor_id,
                binding.consent_org_id,
                format_datetime(binding.consent_at),
                binding.revision,
                binding.status,
                format_datetime(binding.bound_at),
                format_datetime(binding.updated_at),
                format_datetime(binding.retired_at),
                _dump_actor(binding),
                binding.receipt_id,
            ),
        )
        return binding.binding_id

    def update_binding(self, binding: SlotBinding) -> None:
        """Replace the current row for an existing binding, without committing."""
        self._conn.execute(
            "UPDATE slot_bindings SET provider_name = ?, contract_in = ?, "
            "contract_out = ?, billing_mode = ?, third_party_consent = ?, "
            "consent_actor_id = ?, consent_org_id = ?, consent_at = ?, revision = ?, "
            "status = ?, updated_at = ?, retired_at = ?, actor_context_json = ?, "
            "receipt_id = ? WHERE binding_id = ?",
            (
                binding.provider_name,
                binding.contract_in,
                binding.contract_out,
                binding.billing_mode,
                int(binding.third_party_consent),
                binding.consent_actor_id,
                binding.consent_org_id,
                format_datetime(binding.consent_at),
                binding.revision,
                binding.status,
                format_datetime(binding.updated_at),
                format_datetime(binding.retired_at),
                _dump_actor(binding),
                binding.receipt_id,
                binding.binding_id,
            ),
        )

    def save_revision(self, revision: SlotBindingRevision) -> None:
        """Append one history row without committing."""
        self._conn.execute(
            "INSERT INTO slot_binding_revisions "
            "(binding_id, revision, change_kind, install_id, slot_name, provider_name, "
            "contract_in, contract_out, billing_mode, third_party_consent, "
            "consent_actor_id, consent_org_id, consent_at, status, note, recorded_at, "
            "actor_context_json, receipt_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.binding_id,
                revision.revision,
                revision.change_kind,
                revision.install_id,
                revision.slot_name,
                revision.provider_name,
                revision.contract_in,
                revision.contract_out,
                revision.billing_mode,
                int(revision.third_party_consent),
                revision.consent_actor_id,
                revision.consent_org_id,
                format_datetime(revision.consent_at),
                revision.status,
                revision.note,
                format_datetime(revision.recorded_at),
                _dump_actor(revision),
                revision.receipt_id,
            ),
        )

    # -- reads -------------------------------------------------------------

    def get_binding(self, binding_id: str) -> SlotBinding | None:
        row = self._conn.execute(
            "SELECT * FROM slot_bindings WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        return None if row is None else _row_to_binding(row)

    def get_active_binding(self, *, install_id: str, slot_name: str) -> SlotBinding | None:
        """Return the one active binding for a slot. This is what run-start calls."""
        row = self._conn.execute(
            "SELECT * FROM slot_bindings "
            "WHERE install_id = ? AND slot_name = ? AND status = 'active'",
            (install_id, slot_name),
        ).fetchone()
        return None if row is None else _row_to_binding(row)

    def list_bindings(
        self,
        *,
        install_id: str | None = None,
        slot_name: str | None = None,
        status: BindingStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SlotBinding]:
        where, params = _binding_filter(install_id, slot_name, status)
        sql = (
            f"SELECT * FROM slot_bindings{where} "
            "ORDER BY install_id ASC, slot_name ASC, binding_id ASC"
        )
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = [*params, limit, offset]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params = [*params, offset]
        return [_row_to_binding(row) for row in self._conn.execute(sql, params).fetchall()]

    def count_bindings(
        self,
        *,
        install_id: str | None = None,
        slot_name: str | None = None,
        status: BindingStatus | None = None,
    ) -> int:
        where, params = _binding_filter(install_id, slot_name, status)
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM slot_bindings{where}", params
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def list_revisions(
        self,
        binding_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SlotBindingRevision]:
        sql = "SELECT * FROM slot_binding_revisions WHERE binding_id = ? ORDER BY revision ASC"
        params: list[object] = [binding_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        return [_row_to_revision(row) for row in self._conn.execute(sql, params).fetchall()]

    def count_revisions(self, binding_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS count FROM slot_binding_revisions WHERE binding_id = ?",
            (binding_id,),
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()


def _binding_filter(
    install_id: str | None,
    slot_name: str | None,
    status: BindingStatus | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if install_id is not None:
        clauses.append("install_id = ?")
        params.append(install_id)
    if slot_name is not None:
        clauses.append("slot_name = ?")
        params.append(slot_name)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _dump_actor(record: SlotBinding | SlotBindingRevision) -> str | None:
    actor = dump_actor_context(record.actor_context)
    return None if actor is None else canonical_json(actor)


def _require_time(value: str) -> datetime:
    """Parse a NOT NULL timestamp column, refusing a row that lost one."""
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError("binding ledger row is missing a required timestamp")
    return parsed


def _load_actor(value: str | None) -> GovernedActorContext | None:
    return None if not value else load_actor_context(json.loads(value))


def _row_to_binding(row: sqlite3.Row) -> SlotBinding:
    return SlotBinding(
        binding_id=row["binding_id"],
        install_id=row["install_id"],
        slot_name=row["slot_name"],
        provider_name=row["provider_name"],
        contract_in=row["contract_in"],
        contract_out=row["contract_out"],
        billing_mode=row["billing_mode"],
        third_party_consent=bool(row["third_party_consent"]),
        consent_actor_id=row["consent_actor_id"],
        consent_org_id=row["consent_org_id"],
        consent_at=parse_datetime(row["consent_at"]),
        revision=int(row["revision"]),
        status=row["status"],
        bound_at=_require_time(row["bound_at"]),
        updated_at=_require_time(row["updated_at"]),
        retired_at=parse_datetime(row["retired_at"]),
        actor_context=_load_actor(row["actor_context_json"]),
        receipt_id=row["receipt_id"],
    )


def _row_to_revision(row: sqlite3.Row) -> SlotBindingRevision:
    return SlotBindingRevision(
        binding_id=row["binding_id"],
        revision=int(row["revision"]),
        change_kind=row["change_kind"],
        install_id=row["install_id"],
        slot_name=row["slot_name"],
        provider_name=row["provider_name"],
        contract_in=row["contract_in"],
        contract_out=row["contract_out"],
        billing_mode=row["billing_mode"],
        third_party_consent=bool(row["third_party_consent"]),
        consent_actor_id=row["consent_actor_id"],
        consent_org_id=row["consent_org_id"],
        consent_at=parse_datetime(row["consent_at"]),
        status=row["status"],
        note=row["note"],
        recorded_at=_require_time(row["recorded_at"]),
        actor_context=_load_actor(row["actor_context_json"]),
        receipt_id=row["receipt_id"],
    )


__all__ = ["BindingStore", "BindingStoreProtocol"]
