"""Persistent registry mapping opaque server IDs to backend locations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cruxible_core.primitives import new_id
from cruxible_core.server.config import get_server_state_dir
from cruxible_core.temporal import format_datetime, utc_now

LOCAL_FILESYSTEM_BACKEND = "local_filesystem"
GOVERNED_DAEMON_BACKEND = "governed_daemon"


@dataclass(frozen=True)
class InstanceRecord:
    """Persistent mapping from opaque instance ID to backend metadata."""

    instance_id: str
    backend: str
    location: str
    workspace_root: str | None
    created_at: str


@dataclass(frozen=True)
class RegisteredInstance:
    """Registry result for get-or-create flows."""

    record: InstanceRecord
    created: bool


class InstanceRegistry:
    """SQLite-backed registry of server-owned instance IDs."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS instances (
                    instance_id TEXT PRIMARY KEY,
                    backend TEXT NOT NULL,
                    location TEXT NOT NULL,
                    workspace_root TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(backend, location)
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_instances_backend_workspace_root
                ON instances(backend, workspace_root)
                WHERE workspace_root IS NOT NULL
                """
            )

    def get(self, instance_id: str) -> InstanceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at
                FROM instances
                WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def count_instances(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM instances").fetchone()
        assert row is not None
        return int(row["count"])

    def get_or_create_local_instance(self, location: str | Path) -> RegisteredInstance:
        resolved_location = str(Path(location).expanduser().resolve())
        existing = self._get_by_backend_location(LOCAL_FILESYSTEM_BACKEND, resolved_location)
        if existing is not None:
            return RegisteredInstance(record=existing, created=False)

        return self._insert_instance(
            backend=LOCAL_FILESYSTEM_BACKEND,
            location=resolved_location,
            workspace_root=None,
        )

    def get_or_create_governed_instance(
        self,
        workspace_root: str | Path,
    ) -> RegisteredInstance:
        resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        existing = self._get_by_backend_workspace_root(
            GOVERNED_DAEMON_BACKEND,
            resolved_workspace_root,
        )
        if existing is not None:
            return RegisteredInstance(record=existing, created=False)
        return self._create_governed_instance(workspace_root=resolved_workspace_root)

    def create_governed_instance(
        self, workspace_root: str | Path | None = None,
    ) -> RegisteredInstance:
        resolved_workspace_root: str | None = None
        if workspace_root is not None:
            resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        return self._create_governed_instance(workspace_root=resolved_workspace_root)

    def _create_governed_instance(
        self,
        *,
        workspace_root: str | None,
    ) -> RegisteredInstance:
        instance_id = new_id("inst", length=16, separator="_")
        location = str((get_server_state_dir() / "instances" / instance_id).resolve())
        return self._insert_instance(
            backend=GOVERNED_DAEMON_BACKEND,
            location=location,
            workspace_root=workspace_root,
            preferred_instance_id=instance_id,
        )

    def _insert_instance(
        self,
        *,
        backend: str,
        location: str,
        workspace_root: str | None,
        preferred_instance_id: str | None = None,
    ) -> RegisteredInstance:
        created_at = format_datetime(utc_now())
        instance_id = preferred_instance_id or new_id("inst", length=16, separator="_")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO instances(
                    instance_id,
                    backend,
                    location,
                    workspace_root,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    backend,
                    location,
                    workspace_root,
                    created_at,
                ),
            )

        if workspace_root is not None:
            record = self._get_by_backend_workspace_root(backend, workspace_root)
        else:
            record = self._get_by_backend_location(backend, location)
        assert record is not None
        return RegisteredInstance(record=record, created=record.instance_id == instance_id)

    def _get_by_backend_location(self, backend: str, location: str) -> InstanceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at
                FROM instances
                WHERE backend = ? AND location = ?
                """,
                (backend, location),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def _get_by_backend_workspace_root(
        self, backend: str, workspace_root: str,
    ) -> InstanceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at
                FROM instances
                WHERE backend = ? AND workspace_root = ?
                """,
                (backend, workspace_root),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> InstanceRecord:
        return InstanceRecord(
            instance_id=row["instance_id"],
            backend=row["backend"],
            location=row["location"],
            workspace_root=row["workspace_root"],
            created_at=row["created_at"],
        )


_registry: InstanceRegistry | None = None


def get_registry() -> InstanceRegistry:
    """Return the process-global registry instance."""
    global _registry
    if _registry is None:
        state_dir = get_server_state_dir()
        _registry = InstanceRegistry(state_dir / "registry.db")
    return _registry


def reset_registry() -> None:
    """Clear the process-global registry cache. Used by tests."""
    global _registry
    _registry = None
