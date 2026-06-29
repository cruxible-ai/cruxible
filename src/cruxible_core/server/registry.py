"""Persistent registry mapping opaque server IDs to backend locations."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cruxible_core.errors import ConfigError
from cruxible_core.primitives import new_id
from cruxible_core.server.config import get_server_state_dir
from cruxible_core.temporal import format_datetime, utc_now

LOCAL_FILESYSTEM_BACKEND = "local_filesystem"
GOVERNED_DAEMON_BACKEND = "governed_daemon"
_INSTANCE_ID_RE = re.compile(r"^inst_[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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

    def list_instances(self) -> list[InstanceRecord]:
        """Return all registered instances ordered by instance ID."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at
                FROM instances
                ORDER BY instance_id
                """
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

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

    def get_governed_instance_by_workspace_root(
        self,
        workspace_root: str | Path,
    ) -> InstanceRecord | None:
        """Return a governed instance already registered for *workspace_root*."""
        resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        return self._get_by_backend_workspace_root(
            GOVERNED_DAEMON_BACKEND,
            resolved_workspace_root,
        )

    def get_governed_instance_by_location(
        self,
        location: str | Path,
    ) -> InstanceRecord | None:
        """Return the governed instance registered at *location*, if any."""
        resolved_location = str(Path(location).expanduser().resolve())
        return self._get_by_backend_location(
            GOVERNED_DAEMON_BACKEND,
            resolved_location,
        )

    def list_governed_instances(self) -> list[InstanceRecord]:
        """Return every registered governed (daemon-backed) instance record."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at
                FROM instances
                WHERE backend = ?
                ORDER BY instance_id
                """,
                (GOVERNED_DAEMON_BACKEND,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def create_governed_instance(
        self,
        workspace_root: str | Path | None = None,
    ) -> RegisteredInstance:
        resolved_workspace_root: str | None = None
        if workspace_root is not None:
            resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        return self._create_governed_instance(workspace_root=resolved_workspace_root)

    def generate_governed_instance_id(self) -> str:
        """Return an unused governed instance ID without inserting a registry row."""
        for _attempt in range(100):
            instance_id = new_id("inst", length=16, separator="_")
            if (
                self.get(instance_id) is None
                and not self.governed_instance_location(instance_id).exists()
            ):
                return instance_id
        raise ConfigError("Failed to generate a unique hosted instance ID")

    def governed_instance_location(self, instance_id: str) -> Path:
        """Return the server-owned governed instance path for a valid instance ID."""
        _validate_instance_id(instance_id)
        state_dir = Path(get_server_state_dir())
        return (state_dir / "instances" / instance_id).resolve()

    def create_governed_instance_with_id(
        self,
        instance_id: str,
        workspace_root: str | Path | None = None,
    ) -> RegisteredInstance:
        """Register a server-owned governed instance with a caller-selected ID."""
        location = str(self.governed_instance_location(instance_id))
        resolved_workspace_root: str | None = None
        if workspace_root is not None:
            resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        return self._insert_instance(
            backend=GOVERNED_DAEMON_BACKEND,
            location=location,
            workspace_root=resolved_workspace_root,
            preferred_instance_id=instance_id,
        )

    def update_governed_instance_location(
        self,
        instance_id: str,
        location: str | Path,
        workspace_root: str | Path | None = None,
    ) -> InstanceRecord:
        """Update an existing governed instance registry row after restore repair."""
        _validate_instance_id(instance_id)
        resolved_location = str(Path(location).expanduser().resolve())
        resolved_workspace_root: str | None = None
        if workspace_root is not None:
            resolved_workspace_root = str(Path(workspace_root).expanduser().resolve())
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE instances
                SET backend = ?, location = ?, workspace_root = ?
                WHERE instance_id = ?
                """,
                (
                    GOVERNED_DAEMON_BACKEND,
                    resolved_location,
                    resolved_workspace_root,
                    instance_id,
                ),
            )
        record = self.get(instance_id)
        assert record is not None
        return record

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
        self,
        backend: str,
        workspace_root: str,
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


def _validate_instance_id(instance_id: str) -> None:
    if not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise ConfigError(
            "Hosted instance_id must start with 'inst_' and contain only letters, "
            "numbers, '.', '_', or '-'"
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
