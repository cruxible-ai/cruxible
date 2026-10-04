"""Persistent registry mapping opaque server IDs to backend locations.

Locations are stored relative to the state root that holds the registry, so a
copied state root names its own copies, never the original's instances. A row
whose location resolves outside the state root (an absolute row from before
relative storage, copied from elsewhere, or a relative row escaping through
``..`` or a symlink) is never served: `instance_root` refuses it. Containment
compares resolved real paths component by component, case-insensitively, so a
differently-cased spelling of the same directory on a case-insensitive volume
cannot pass as another one, and an alias cannot pass as the root.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.temporal import format_datetime, utc_now
from cruxible_client.contracts.workspace_layout import ensure_workspace_directory
from cruxible_core.errors import ConfigError, InstanceLocationRefusedError
from cruxible_core.server.config import (
    STATE_ROOT_OWN_ENTRIES,
    contained_relative,
    get_server_state_root,
    within_state_root,
)
from cruxible_core.storage.preview_fence import refuse_write_while_previewing

LOCAL_FILESYSTEM_BACKEND = "local_filesystem"
GOVERNED_DAEMON_BACKEND = "governed_daemon"
_INSTANCE_ID_RE = re.compile(r"^inst_[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FLOOR_DELIVERY_MIGRATION_STEP = "2026-10-01-floor-delivery-column"


def _migrate_floor_delivery_column(conn: sqlite3.Connection) -> None:
    """2026-10-01-floor-delivery-column: safe in either registry migration order.

    Keyed by the column's existence, so a registry where an earlier build ran
    this step outside the recorded chain is left exactly as it is.
    """

    columns = {row[1] for row in conn.execute("PRAGMA table_info(instances)")}
    if "floor_delivery" not in columns:
        conn.execute(
            "ALTER TABLE instances ADD COLUMN floor_delivery INTEGER NOT NULL DEFAULT 0 "
            "CHECK (floor_delivery IN (0,1) AND "
            "(floor_delivery=0 OR workspace_root IS NOT NULL))"
        )
        conn.execute("UPDATE instances SET floor_delivery=1 WHERE workspace_root IS NOT NULL")


@dataclass(frozen=True)
class InstanceRecord:
    """Persistent mapping from opaque instance ID to backend metadata.

    ``location`` is absolute: the stored state-root-relative path joined to
    the registry's state root (or, for a row outside it, the stored absolute
    path). ``within_state_root`` says whether it resolves under that root;
    only such a row is ever served (`InstanceRegistry.instance_root`).
    """

    instance_id: str
    backend: str
    location: str
    workspace_root: str | None
    created_at: str
    within_state_root: bool = True
    #: Whether this host delivers floor exports to its bound workspace (G4c);
    #: only a host with a workspace may deliver.
    floor_delivery: bool = False


@dataclass(frozen=True)
class PreparedInstance:
    """One validated governed host row, ready to insert (or to answer a preview)."""

    instance_id: str
    location: str
    workspace_root: str | None


@dataclass(frozen=True)
class RegisteredInstance:
    """Registry result for get-or-create flows."""

    record: InstanceRecord
    created: bool


class InstanceRegistry:
    """SQLite-backed registry of server-owned instance IDs."""

    def __init__(self, state_root: Path) -> None:
        # Anchored to the CONFIGURED state root, never to where the registry
        # file resolves: a copied root whose ``daemon/`` links back to the
        # original's would otherwise adopt the original as its root. Every
        # step to the registry must be this root's own (`within_state_root`),
        # checked before the file is opened or migrated.
        self.state_root = Path(os.path.realpath(state_root))
        for entry in STATE_ROOT_OWN_ENTRIES:
            within_state_root(self.state_root, *entry)
        self.db_path = self.state_root / "daemon" / "registry.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._migrate()

    def _migrate(self) -> None:
        """Run each registry migration step not yet recorded, in declaration order.

        The chain is append-only. Every step is idempotent and keyed by its own
        id in ``registry_migrations``, never by a schema version or a column
        position, so steps added on separate branches compose in either order.
        """

        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_migrations (
                    step TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {row[0] for row in conn.execute("SELECT step FROM registry_migrations")}
        for step, run in self._MIGRATIONS:
            if step in applied:
                continue
            run(self)
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO registry_migrations(step, applied_at) VALUES (?, ?)",
                    (step, format_datetime(utc_now())),
                )

    def relative_location(self, path: str | Path) -> PurePath | None:
        """``path`` relative to the state root when it resolves under it, else None."""

        return contained_relative(Path(path), self.state_root)

    def instance_root(self, record: InstanceRecord) -> Path:
        """The instance directory a daemon may serve for ``record``, or a typed refusal."""

        relative = self.relative_location(record.location)
        if relative is None:
            raise InstanceLocationRefusedError(
                instance_id=record.instance_id,
                location=record.location,
                state_root=str(self.state_root),
            )
        # Rebuilt under this root rather than taken from the row: on a
        # case-sensitive volume a differently-cased spelling compares equal
        # here, and the directory served must be this root's own.
        return (self.state_root / relative).resolve(strict=False)

    def _relativize_locations(self) -> None:
        """Rewrite absolute rows under this state root as state-root-relative.

        The migration for rows written before relative storage. A row whose
        absolute location lies outside this state root is left exactly as it
        is, and refused when served: it names another state root's instance
        (typically the original of a copied state root).
        """

        with self._connect() as conn:
            rows = conn.execute("SELECT instance_id, location FROM instances").fetchall()
            for row in rows:
                stored = row["location"]
                if not Path(stored).is_absolute():
                    continue
                relative = self.relative_location(stored)
                if relative is None:
                    continue
                conn.execute(
                    "UPDATE instances SET location = ? WHERE instance_id = ?",
                    (relative.as_posix(), row["instance_id"]),
                )

    def _add_floor_delivery_column(self) -> None:
        """Add G4c's ``floor_delivery`` column, default on for attached hosts.

        One transaction (SQLite DDL is transactional), so an interruption
        leaves the registry without the column and the step reruns whole.
        """

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _migrate_floor_delivery_column(conn)

    #: (step id, step): the append-only migration chain `_migrate` runs.
    _MIGRATIONS: tuple[tuple[str, Callable[[InstanceRegistry], None]], ...] = (
        ("2026-10-01-relative-locations", _relativize_locations),
        (FLOOR_DELIVERY_MIGRATION_STEP, _add_floor_delivery_column),
    )

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

    def set_floor_delivery(self, instance_id: str, enabled: bool) -> InstanceRecord:
        """Set delivery only for a registered governed host with a local workspace."""

        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE instances SET floor_delivery=? WHERE instance_id=? AND backend=? "
                "AND workspace_root IS NOT NULL",
                (int(enabled), instance_id, GOVERNED_DAEMON_BACKEND),
            )
        if cursor.rowcount != 1:
            raise ConfigError("Floor delivery requires a bound local workspace")
        record = self.get(instance_id)
        assert record is not None
        return record

    def get(self, instance_id: str) -> InstanceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at, floor_delivery
                FROM instances
                WHERE instance_id = ?
                """,
                (instance_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_instances(self) -> list[InstanceRecord]:
        """Return all registered instances ordered by instance ID."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at, floor_delivery
                FROM instances
                ORDER BY instance_id
                """
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

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

    def list_governed_instances(self) -> list[InstanceRecord]:
        """Return every registered governed (daemon-backed) instance record."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at, floor_delivery
                FROM instances
                WHERE backend = ?
                ORDER BY instance_id
                """,
                (GOVERNED_DAEMON_BACKEND,),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

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
        return (self.state_root / "instances" / instance_id).resolve()

    def prepare_governed_instance(
        self,
        instance_id: str,
        workspace_root: str | Path | None = None,
    ) -> PreparedInstance:
        """Validate one new governed host row and every conflict it would meet.

        Reads only. The commit (`create_governed_instance`) inserts exactly what
        this prepared, so a preview and its commit refuse the same requests: an
        invalid ID, a location another row already holds, or a worktree already
        attached to another host.
        """

        location = str(self.governed_instance_location(instance_id))
        resolved_workspace_root: str | None = None
        if workspace_root is not None:
            resolved_workspace_root = str(
                ensure_workspace_directory(
                    Path(workspace_root).expanduser().resolve(), state_root=self.state_root
                )
            )
            attached = self._get_by_backend_workspace_root(
                GOVERNED_DAEMON_BACKEND, resolved_workspace_root
            )
            if attached is not None and attached.instance_id != instance_id:
                raise ConfigError(
                    f"Workspace {resolved_workspace_root!r} is already attached to Cruxible "
                    f"host {attached.instance_id!r}; release it with `cruxible "
                    f"workspace detach --instance-id {attached.instance_id}` or choose "
                    f"another Git worktree before creating {instance_id!r}"
                )
        relative = self.relative_location(location)
        stored = relative.as_posix() if relative is not None else location
        holder = self._get_by_backend_location(GOVERNED_DAEMON_BACKEND, stored)
        if holder is not None and holder.instance_id != instance_id:
            raise ConfigError(
                f"Location {location!r} is already registered to Cruxible host "
                f"{holder.instance_id!r}"
            )
        return PreparedInstance(
            instance_id=instance_id,
            location=location,
            workspace_root=resolved_workspace_root,
        )

    def create_governed_instance_with_id(
        self,
        instance_id: str,
        workspace_root: str | Path | None = None,
    ) -> RegisteredInstance:
        """Prepare and register one governed host with a caller-selected ID."""

        return self.create_governed_instance(
            self.prepare_governed_instance(instance_id, workspace_root=workspace_root)
        )

    def create_governed_instance(
        self,
        prepared: PreparedInstance,
        *,
        observe: Callable[[StateCoordinate], None] | None = None,
    ) -> RegisteredInstance:
        """Register the governed host `prepare_governed_instance` validated.

        ``observe`` sees the host's row (absent, for a new host) inside the
        inserting transaction (R12's pin check).
        """

        return self._insert_instance(
            backend=GOVERNED_DAEMON_BACKEND,
            location=prepared.location,
            workspace_root=prepared.workspace_root,
            preferred_instance_id=prepared.instance_id,
            observe=observe,
        )

    def host_state(self, instance_id: str) -> StateCoordinate:
        """The state coordinate of one host's registry row (R12); absent rows digest too."""

        with self._connect() as conn:
            return self._host_state_conn(conn, instance_id)

    @staticmethod
    def _host_state_conn(conn: sqlite3.Connection, instance_id: str) -> StateCoordinate:
        row = conn.execute(
            "SELECT backend, location, workspace_root, floor_delivery FROM instances "
            "WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        return StateCoordinate.of(
            f"host:{instance_id}", None if row is None else [row[0], row[1], row[2], row[3]]
        )

    def workspace_state(self, instance_id: str) -> StateCoordinate:
        """The state coordinate of one host's worktree binding (R12)."""

        record = self.get(instance_id)
        return _workspace_state(instance_id, None if record is None else record.workspace_root)

    def attach_governed_workspace(
        self,
        instance_id: str,
        workspace_root: str | Path,
        *,
        observe: Callable[[StateCoordinate], None] | None = None,
        observe_host: Callable[[StateCoordinate], None] | None = None,
    ) -> InstanceRecord:
        """Attach one exact local workspace without replacing an existing attachment.

        ``observe`` sees the host's binding, and ``observe_host`` its whole
        registry row (`host_state`), inside the attaching transaction before
        it changes (R12's pin check); the transaction is held through the
        write, so the row cannot move between the check and the attach.
        """

        # The registry is the last gate: no row ever names a root that is no workspace.
        ensure_workspace_directory(
            Path(workspace_root).expanduser().resolve(), state_root=self.state_root
        )
        refuse_write_while_previewing("instance registry")
        _validate_instance_id(instance_id)
        try:
            resolved = str(Path(workspace_root).expanduser().resolve(strict=True))
        except OSError as exc:
            raise ConfigError("Attached workspace path does not exist") from exc
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT backend, workspace_root FROM instances WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                if row is None or row["backend"] != GOVERNED_DAEMON_BACKEND:
                    raise ConfigError(f"Instance '{instance_id}' is not a governed daemon host")
                if observe is not None:
                    observe(_workspace_state(instance_id, row["workspace_root"]))
                if observe_host is not None:
                    observe_host(self._host_state_conn(conn, instance_id))
                if row["workspace_root"] is not None:
                    if row["workspace_root"] != resolved:
                        raise ConfigError("Cruxible host is already attached to another workspace")
                else:
                    # Attaching turns floor delivery on (G4c).
                    conn.execute(
                        "UPDATE instances SET workspace_root = ?, floor_delivery = 1 "
                        "WHERE instance_id = ?",
                        (resolved, instance_id),
                    )
        except sqlite3.IntegrityError as exc:
            raise ConfigError("Workspace is already attached to another Cruxible host") from exc
        record = self.get(instance_id)
        assert record is not None
        return record

    def detach_governed_workspace(
        self,
        instance_id: str,
        *,
        expected_workspace_root: str | Path,
        observe: Callable[[StateCoordinate], None] | None = None,
    ) -> InstanceRecord:
        """Release exactly this attachment (the expected worktree, nothing else).

        ``observe`` sees the binding inside the releasing transaction.
        """

        refuse_write_while_previewing("instance registry")
        _validate_instance_id(instance_id)
        expected = str(Path(expected_workspace_root).expanduser().resolve(strict=False))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if observe is not None:
                row = conn.execute(
                    "SELECT workspace_root FROM instances WHERE instance_id = ?",
                    (instance_id,),
                ).fetchone()
                observe(_workspace_state(instance_id, None if row is None else row[0]))
            cursor = conn.execute(
                """
                UPDATE instances
                SET workspace_root = NULL, floor_delivery = 0
                WHERE instance_id = ? AND workspace_root = ?
                """,
                (instance_id, expected),
            )
        if cursor.rowcount != 1:
            raise ConfigError("Cruxible workspace attachment changed during rollback")
        record = self.get(instance_id)
        assert record is not None
        return record

    def _insert_instance(
        self,
        *,
        backend: str,
        location: str,
        workspace_root: str | None,
        preferred_instance_id: str | None = None,
        observe: Callable[[StateCoordinate], None] | None = None,
    ) -> RegisteredInstance:
        if workspace_root is not None:
            # The last gate before a row names a workspace (as attach is).
            ensure_workspace_directory(Path(workspace_root), state_root=self.state_root)
        refuse_write_while_previewing("instance registry")
        if Path(location).is_absolute():
            relative = self.relative_location(location)
            if relative is not None:
                location = relative.as_posix()
        created_at = format_datetime(utc_now())
        instance_id = preferred_instance_id or new_id("inst", length=16, separator="_")
        with self._connect() as conn:
            if observe is not None:
                conn.execute("BEGIN IMMEDIATE")
                observe(self._host_state_conn(conn, instance_id))
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO instances(
                    instance_id,
                    backend,
                    location,
                    workspace_root,
                    created_at,
                    floor_delivery
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    instance_id,
                    backend,
                    location,
                    workspace_root,
                    created_at,
                    int(backend == GOVERNED_DAEMON_BACKEND and workspace_root is not None),
                ),
            )

        record = self.get(instance_id)
        if record is None and workspace_root is not None:
            record = self._get_by_backend_workspace_root(backend, workspace_root)
        if record is None:
            record = self._get_by_backend_location(backend, location)
        assert record is not None
        return RegisteredInstance(record=record, created=cursor.rowcount == 1)

    def _get_by_backend_location(self, backend: str, location: str) -> InstanceRecord | None:
        """Look a row up by its STORED location (state-root-relative when inside)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT instance_id, backend, location, workspace_root, created_at, floor_delivery
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
                SELECT instance_id, backend, location, workspace_root, created_at, floor_delivery
                FROM instances
                WHERE backend = ? AND workspace_root = ?
                """,
                (backend, workspace_root),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def _row_to_record(self, row: sqlite3.Row) -> InstanceRecord:
        stored = Path(row["location"])
        location = stored if stored.is_absolute() else self.state_root / stored
        return InstanceRecord(
            instance_id=row["instance_id"],
            backend=row["backend"],
            location=str(location),
            workspace_root=row["workspace_root"],
            created_at=row["created_at"],
            within_state_root=self.relative_location(location) is not None,
            floor_delivery=bool(row["floor_delivery"]),
        )


def _workspace_state(instance_id: str, workspace_root: str | None) -> StateCoordinate:
    return StateCoordinate.of(f"host_workspace:{instance_id}", {"workspace_root": workspace_root})


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
        state_root = get_server_state_root()
        _registry = InstanceRegistry(state_root)
    return _registry


def reset_registry() -> None:
    """Clear the process-global registry cache. Used by tests."""
    global _registry
    _registry = None
