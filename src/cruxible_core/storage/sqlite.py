"""SQLite storage backend for durable Cruxible instance state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.legacy_identity import dump_legacy_identity_map
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, mint_claim_id
from cruxible_core.instance_protocol import StateSnapshot
from cruxible_core.primitives import canonical_json
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.resolution_contracts.store import ResolutionContractStore
from cruxible_core.sqlite_ddl import execute_schema_script
from cruxible_core.storage.protocols import (
    GraphRepositoryProtocol,
    SnapshotRepositoryProtocol,
    UnitOfWorkProtocol,
)
from cruxible_core.storage.resolution_evidence import LegacyResolutionEvidenceReader
from cruxible_core.temporal import format_datetime, utc_now

StorageIntegrityError = sqlite3.IntegrityError
# Broader low-level storage error family (OperationalError, etc.). Re-exported so
# non-storage layers (e.g. the HTTP error handlers) can recognize and genericize
# DB errors without importing sqlite3 directly across the storage boundary.
StorageDatabaseError = sqlite3.DatabaseError

_GRAPH_SCHEMA = """\
CREATE TABLE IF NOT EXISTS storage_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_entities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    node_id TEXT NOT NULL UNIQUE,
    properties_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_entities_type ON graph_entities(entity_type);

"""

# Split out so migration 0004 can recreate exactly this table + these indexes
# during the rebuild without re-running the rest of the graph schema.
_GRAPH_RELATIONSHIPS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS graph_relationships (
    claim_id TEXT PRIMARY KEY,
    edge_key INTEGER NOT NULL UNIQUE,
    from_type TEXT NOT NULL,
    from_id TEXT NOT NULL,
    to_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    FOREIGN KEY (from_type, from_id) REFERENCES graph_entities(entity_type, entity_id),
    FOREIGN KEY (to_type, to_id) REFERENCES graph_entities(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_relationships_from
    ON graph_relationships(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_graph_relationships_to
    ON graph_relationships(to_type, to_id);
CREATE INDEX IF NOT EXISTS idx_graph_relationships_type
    ON graph_relationships(relationship_type);
CREATE INDEX IF NOT EXISTS idx_graph_relationships_identity
    ON graph_relationships(
        from_type, from_id, to_type, to_id, relationship_type, edge_key
    );
"""

_GRAPH_SCHEMA = _GRAPH_SCHEMA + _GRAPH_RELATIONSHIPS_SCHEMA

_SNAPSHOT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS instance_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    parent_snapshot_id TEXT,
    origin_snapshot_id TEXT,
    label TEXT,
    config_digest TEXT NOT NULL,
    lock_digest TEXT,
    graph_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_created_at
    ON snapshots(created_at, snapshot_id);

CREATE TABLE IF NOT EXISTS snapshot_artifacts (
    snapshot_id TEXT NOT NULL,
    artifact_name TEXT NOT NULL,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, artifact_name),
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id) ON DELETE CASCADE
);
"""

_UNIFIED_STATE_MIGRATION = "0001_unified_sqlite_state"
SNAPSHOT_SCHEMA_MIGRATION = "0002_snapshot_tables"
READ_REVISION_MIGRATION = "0003_read_revision"
CLAIM_IDENTITY_MIGRATION = "0004_claim_identity"

# Every migration ``_initialize_connection`` knows how to apply. The steady-state
# pre-check compares against this set to decide whether it needs the write lock
# at all; a new migration MUST be added here or initialization silently stops
# running it on already-stamped databases.
_ALL_STORAGE_MIGRATIONS = frozenset(
    {
        _UNIFIED_STATE_MIGRATION,
        SNAPSHOT_SCHEMA_MIGRATION,
        READ_REVISION_MIGRATION,
        CLAIM_IDENTITY_MIGRATION,
    }
)

# Per-instance reconcile map for LEGACY (pre-identity) upstream images, stored in
# ``instance_state``. Keyed by the claim 5-tuple, valued by the id this instance
# minted for it, so re-pulling the SAME pre-upgrade release reuses the ids
# instead of silently re-minting every upstream identity -- which would stale
# every record-time claim_id stamp while every recorded digest stayed identical.
# It lives in the state DB (not on disk beside the bundle) because it is
# per-instance reconciliation state and must be written in the SAME transaction
# as the backfill it describes.
LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY = "legacy_claim_identity_map"

# Monotonic state counter, stored in instance_state and incremented in the SAME
# transaction as every state-mutating commit. It is a freshness marker for read
# envelopes and continuation tokens; receipts prove computation, never
# freshness. It only ever moves forward — restores and rollbacks bump it too.
READ_REVISION_STATE_KEY = "read_revision"

# Tables whose writes are audit/proof records rather than state mutations.
# Read paths persist query receipts, so writes
# to these tables must NOT advance read_revision (reads never bump it). The
# membership test is "can writing this row change what an ordinary read
# returns?", not "does this row look like history?" -- see the docstring below.
_AUDIT_ONLY_TABLES = frozenset(
    {
        "receipts",
        "receipt_entities",
        "execution_traces",
        "procedure_evidence_artifacts",
        "procedure_run_evidence",
        # Fired-node rows are written only inside the run-finalization unit of
        # work. The procedure_runs update advances the revision once for the
        # whole commit; if fired nodes ever gain an independent write path,
        # this audit-only classification must be revisited.
        "procedure_run_fired_nodes",
        "instance_state",
        "storage_migrations",
    }
)
"""Tables whose writes are audit records, never state, so they never bump ``read_revision``.

Adding a table here is a real claim: that writing it cannot change what any read
of the instance's state returns. Verify that against the READ consumers, not
just the write path, before extending the set.

DO NOT add resolution-contract tables. Their state participates in ordinary
reads and queues, so changing it must advance ``read_revision``. Continuation
tokens validate on that counter; exempting these writes could otherwise let a
paginated read span two states without detecting the change. Historical
relationship-attestation tables are read only through the PC-C compatibility
reader and have no live mutation path.

``procedure_runs`` was here and is NOT any more, for the same reason. It was a
defensible classification only while the run ledger was write-only history read
through its own dedicated ``procedure runs`` listing. It stopped being one the
moment the procedure list and detail surfaces began deriving a ``track_record``
block from those rows: starting a run and finalizing one each change what a
plain ``procedure list``/``get`` returns, so leaving them revision-silent would
let a page read at revision N, a run land, and the next page's token still
validate against an unchanged counter -- exactly the paginated-read-spanning-
two-states hole described above.

The two evidence tables stay. Not because a run's declared evidence is
uninteresting to reads, but because those rows are only ever written in the
SAME unit of work as the run row they belong to
(``_persist_procedure_evidence_outputs_in_uow`` runs inside the finalize
transaction). The run write is what advances the revision; the evidence rows
ride that same commit and can never move independently of it. If evidence ever
becomes writable outside a run's transaction, this entry has to be revisited.
"""


_WAL_SWITCH_TIMEOUT_SECONDS = 5.0


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    _ensure_wal_journal(conn)


def _ensure_wal_journal(conn: sqlite3.Connection) -> None:
    """Switch to WAL, waiting out a concurrent writer instead of failing.

    Changing the journal mode needs an EXCLUSIVE lock, and unlike ordinary
    statements it does NOT honour ``busy_timeout`` -- it returns SQLITE_BUSY
    immediately. That matters now that initialization takes a migration lock:
    a second process opening the same not-yet-WAL database while the first is
    mid-migration would otherwise die on connect, before it ever got the chance
    to wait for the lock. A database ALREADY in WAL needs no switch at all,
    which is the overwhelmingly common case.

    ``PRAGMA journal_mode = WAL`` also fails QUIETLY: when the switch is
    refused it returns the CURRENT (unchanged) mode as its result row instead
    of raising. Checking the returned row is therefore the only way to tell a
    real switch from a silent no-op, and a silent no-op here would leave the
    database in rollback-journal mode where readers block behind writers --
    precisely the contention this module is arranged to avoid.
    """
    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if str(current).lower() == "wal":
        return
    deadline = time.monotonic() + _WAL_SWITCH_TIMEOUT_SECONDS
    while True:
        try:
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            if row is not None and str(row[0]).lower() == "wal":
                return
            switch_error: Exception = sqlite3.OperationalError(
                "could not switch journal_mode to WAL "
                f"(mode is still {None if row is None else row[0]!r})"
            )
        except sqlite3.OperationalError as exc:
            switch_error = exc
        if time.monotonic() >= deadline:
            raise switch_error
        time.sleep(0.05)


def backup_sqlite_database(source: Path, target: Path) -> None:
    """Copy a SQLite database using SQLite's online backup API."""
    source_conn = sqlite3.connect(source)
    target_conn = sqlite3.connect(target)
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


class SQLiteGraphRepository:
    """Repository for live graph rows in the unified SQLite state database."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def load_graph(self) -> EntityGraph:
        nodes = []
        for row in self._conn.execute(
            "SELECT entity_type, entity_id, node_id, properties_json, metadata_json "
            "FROM graph_entities ORDER BY entity_type, entity_id"
        ).fetchall():
            nodes.append(
                {
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "properties": json.loads(row["properties_json"]),
                    "metadata": json.loads(row["metadata_json"]),
                    "id": row["node_id"],
                }
            )

        edges = []
        for row in self._conn.execute(
            "SELECT claim_id, edge_key, from_type, from_id, to_type, to_id, "
            "relationship_type, properties_json, metadata_json "
            "FROM graph_relationships ORDER BY edge_key"
        ).fetchall():
            edges.append(
                {
                    "relationship_type": row["relationship_type"],
                    "claim_id": row["claim_id"],
                    "properties": json.loads(row["properties_json"]),
                    "metadata": json.loads(row["metadata_json"]),
                    "source": f"{row['from_type']}:{row['from_id']}",
                    "target": f"{row['to_type']}:{row['to_id']}",
                    "key": int(row["edge_key"]),
                }
            )

        return EntityGraph.from_dict(
            {
                "directed": True,
                "multigraph": True,
                "graph": {},
                "nodes": nodes,
                "edges": edges,
            }
        )

    def save_graph(self, graph: EntityGraph) -> None:
        """Replace live graph rows with a full graph image.

        Full replace uses a plain INSERT, not the incremental upsert: the table
        was just emptied, so any conflict is a duplicate identity WITHIN the
        image itself, and surfacing that as an IntegrityError is the second of
        the three claim_id uniqueness layers.
        """
        self._conn.execute("DELETE FROM graph_relationships")
        self._conn.execute("DELETE FROM graph_entities")

        self.upsert_entities(graph.iter_all_entities())
        self._insert_relationships(
            RelationshipInstance(
                relationship_type=edge["relationship_type"],
                from_type=edge["from_type"],
                from_id=edge["from_id"],
                to_type=edge["to_type"],
                to_id=edge["to_id"],
                edge_key=edge["edge_key"],
                claim_id=edge["claim_id"],
                properties=dict(edge["properties"]),
                metadata=edge["metadata"],
            )
            for edge in graph.iter_edges()
        )

    def upsert_entities(self, entities: Iterable[EntityInstance]) -> None:
        """Persist entity rows touched by an incremental mutation."""
        for entity in entities:
            self._conn.execute(
                "INSERT INTO graph_entities "
                "(entity_type, entity_id, node_id, properties_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET "
                "node_id = excluded.node_id, "
                "properties_json = excluded.properties_json, "
                "metadata_json = excluded.metadata_json",
                (
                    entity.entity_type,
                    entity.entity_id,
                    entity.node_id(),
                    canonical_json(entity.properties),
                    canonical_json(entity.metadata.to_metadata_dict()),
                ),
            )

    @staticmethod
    def _relationship_row(relationship: RelationshipInstance) -> tuple[Any, ...]:
        """Validate one relationship and render its durable row tuple."""
        claim_id = relationship.claim_id
        if not claim_id or not claim_id.strip():
            raise ValueError(
                "Durable relationship writes require a claim_id "
                f"({relationship.relationship_label()}); it is minted by "
                "graph.operations.apply_relationship or by the legacy-image backfill"
            )
        edge_key = relationship.edge_key
        if edge_key is None:
            raise ValueError("Incremental relationship writes require a stable edge_key")
        if not isinstance(edge_key, int):
            try:
                edge_key = int(edge_key)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Graph edge key {edge_key!r} is not stable") from exc
        return (
            claim_id,
            edge_key,
            relationship.from_type,
            relationship.from_id,
            relationship.to_type,
            relationship.to_id,
            relationship.relationship_type,
            canonical_json(relationship.properties),
            canonical_json(relationship.metadata.model_dump(mode="json", exclude_none=True)),
        )

    def _insert_relationships(self, relationships: Iterable[RelationshipInstance]) -> None:
        """Insert relationship rows with no conflict handling (full-replace path)."""
        for relationship in relationships:
            self._conn.execute(
                "INSERT INTO graph_relationships "
                "(claim_id, edge_key, from_type, from_id, to_type, to_id, "
                "relationship_type, properties_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                self._relationship_row(relationship),
            )

    def upsert_relationships(self, relationships: Iterable[RelationshipInstance]) -> None:
        """Persist relationship rows touched by an incremental mutation.

        Conflict-targets ``claim_id`` -- the durable identity -- but RETARGETING
        is refused: on conflict the stored immutable tuple (endpoints +
        relationship type) is compared with the incoming one and a mismatch
        raises. Without that comparison, conflict-target-on-id would let a
        miscopied id silently repoint an existing identity at different
        endpoints while uniqueness stayed perfectly satisfied. On-conflict
        updates therefore touch only the MUTABLE columns: properties, metadata,
        and the per-load ``edge_key``.
        """
        for relationship in relationships:
            row = self._relationship_row(relationship)
            claim_id = row[0]
            existing = self._conn.execute(
                "SELECT from_type, from_id, to_type, to_id, relationship_type "
                "FROM graph_relationships WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if existing is not None:
                stored = (
                    existing["from_type"],
                    existing["from_id"],
                    existing["to_type"],
                    existing["to_id"],
                    existing["relationship_type"],
                )
                incoming = (row[2], row[3], row[4], row[5], row[6])
                if stored != incoming:
                    raise ValueError(
                        f"Refusing to retarget claim '{claim_id}': stored identity "
                        f"{stored} does not match the incoming identity {incoming}. "
                        "claim_id is immutable and never moves between claims."
                    )
            self._conn.execute(
                "INSERT INTO graph_relationships "
                "(claim_id, edge_key, from_type, from_id, to_type, to_id, "
                "relationship_type, properties_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(claim_id) DO UPDATE SET "
                "edge_key = excluded.edge_key, "
                "properties_json = excluded.properties_json, "
                "metadata_json = excluded.metadata_json",
                row,
            )

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM graph_entities").fetchone()
        return row is None or int(row["count"]) == 0


class SQLiteSnapshotRepository:
    """Repository for DB-authoritative snapshot metadata, artifacts, and head state."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection

    def save_snapshot(
        self,
        snapshot: StateSnapshot,
        artifacts: Mapping[str, bytes | str],
    ) -> None:
        """Persist a snapshot row and its portable artifact payloads."""
        normalized_artifacts = {
            name: content.encode("utf-8") if isinstance(content, str) else bytes(content)
            for name, content in artifacts.items()
        }
        snapshot_json_bytes = normalized_artifacts.get("snapshot.json")
        if snapshot_json_bytes is None:
            snapshot_json_bytes = json.dumps(
                snapshot.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            ).encode("utf-8")

        self._conn.execute(
            "INSERT INTO snapshots "
            "(snapshot_id, created_at, parent_snapshot_id, origin_snapshot_id, label, "
            "config_digest, lock_digest, graph_digest, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                format_datetime(snapshot.created_at),
                snapshot.parent_snapshot_id,
                snapshot.origin_snapshot_id,
                snapshot.label,
                snapshot.config_digest,
                snapshot.lock_digest,
                snapshot.graph_digest,
                snapshot_json_bytes.decode("utf-8"),
            ),
        )
        for artifact_name, content in normalized_artifacts.items():
            self._conn.execute(
                "INSERT INTO snapshot_artifacts "
                "(snapshot_id, artifact_name, content, sha256, media_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_id,
                    artifact_name,
                    sqlite3.Binary(content),
                    hashlib.sha256(content).hexdigest(),
                    _snapshot_artifact_media_type(artifact_name),
                ),
            )

    def get_snapshot(self, snapshot_id: str) -> StateSnapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return StateSnapshot.model_validate_json(row["snapshot_json"])

    def list_snapshots(self, limit: int | None = None) -> list[StateSnapshot]:
        query = "SELECT snapshot_json FROM snapshots ORDER BY created_at DESC, snapshot_id DESC"
        params: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (limit,)
        return [
            StateSnapshot.model_validate_json(row["snapshot_json"])
            for row in self._conn.execute(query, params).fetchall()
        ]

    def get_snapshot_artifact(self, snapshot_id: str, artifact_name: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT content FROM snapshot_artifacts WHERE snapshot_id = ? AND artifact_name = ?",
            (snapshot_id, artifact_name),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["content"])

    def list_snapshot_artifacts(self, snapshot_id: str) -> dict[str, bytes]:
        return {
            str(row["artifact_name"]): bytes(row["content"])
            for row in self._conn.execute(
                "SELECT artifact_name, content FROM snapshot_artifacts "
                "WHERE snapshot_id = ? ORDER BY artifact_name",
                (snapshot_id,),
            ).fetchall()
        }

    def set_instance_state(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO instance_state(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json = excluded.value_json, "
            "updated_at = excluded.updated_at",
            (
                key,
                json.dumps(value, sort_keys=True),
                format_datetime(utc_now()),
            ),
        )

    def get_instance_state(self, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT value_json FROM instance_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def get_read_revision(self) -> int:
        """Return the monotonic read revision (0 for a never-mutated state DB)."""
        value = self.get_instance_state(READ_REVISION_STATE_KEY)
        return int(value) if isinstance(value, int) else 0


def _snapshot_artifact_media_type(artifact_name: str) -> str:
    if artifact_name.endswith(".json"):
        return "application/json"
    if artifact_name.endswith((".yaml", ".yml")):
        return "application/yaml"
    return "application/octet-stream"


class SQLiteUnitOfWork(UnitOfWorkProtocol):
    """Single SQLite transaction spanning graph and audit repositories."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        _configure_connection(self._conn)
        self.graph: GraphRepositoryProtocol = SQLiteGraphRepository(self._conn)
        self.snapshots: SnapshotRepositoryProtocol = SQLiteSnapshotRepository(self._conn)
        self.receipts = SQLiteReceiptStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.resolution_evidence = LegacyResolutionEvidenceReader(connection=self._conn)
        self.resolution_contracts = ResolutionContractStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self._entered = False
        self._started_transaction = False
        self._after_commit: list[Any] = []
        self._after_rollback: list[Any] = []
        # State-mutation tracking for the monotonic read revision: the SQLite
        # authorizer observes every INSERT/UPDATE/DELETE prepared on this
        # connection; touching any non-audit table marks the unit of work as a
        # state mutation, and commit() then advances read_revision inside the
        # same transaction. Audit-only writes (receipts and traces) never bump
        # it, so read paths that persist proof records keep
        # the revision unchanged.
        self._state_mutated = False
        self._conn.set_authorizer(self._authorize)

    def _authorize(self, action: int, arg1: Any, arg2: Any, db_name: Any, source: Any) -> int:
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            table = arg1 if isinstance(arg1, str) else ""
            if table and table not in _AUDIT_ONLY_TABLES and not table.startswith("sqlite_"):
                self._state_mutated = True
        return sqlite3.SQLITE_OK

    def __enter__(self) -> SQLiteUnitOfWork:
        self.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def begin(self) -> None:
        if self._entered:
            return
        self._entered = True
        if not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
            self._started_transaction = True

    def register_after_commit(self, callback: Any) -> None:
        self._after_commit.append(callback)

    def register_after_rollback(self, callback: Any) -> None:
        self._after_rollback.append(callback)

    def commit(self) -> None:
        try:
            if self._started_transaction:
                if self._state_mutated:
                    self._advance_read_revision()
                self._conn.commit()
        except Exception:
            self.rollback()
            raise
        callbacks = list(self._after_commit)
        self._after_commit.clear()
        # The commit phase has passed for this unit of work. Post-commit
        # callback failures must not execute cleanup for state already accepted
        # by the transaction owner.
        self._after_rollback.clear()
        for callback in callbacks:
            callback()

    def _advance_read_revision(self) -> None:
        """Increment read_revision inside the still-open transaction.

        Runs exactly once per state-mutating commit, immediately before the
        SQLite COMMIT, so the revision advance is atomic with the mutation it
        marks. Rollbacks discard it with everything else.
        """
        self._conn.execute(
            "INSERT INTO instance_state(key, value_json, updated_at) VALUES (?, '1', ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json = CAST(CAST(value_json AS INTEGER) + 1 AS TEXT), "
            "updated_at = excluded.updated_at",
            (READ_REVISION_STATE_KEY, format_datetime(utc_now())),
        )
        self._state_mutated = False

    def rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.rollback()
        self._state_mutated = False
        for callback in reversed(self._after_rollback):
            callback()
        self._after_commit.clear()
        self._after_rollback.clear()

    def close(self) -> None:
        self._conn.close()


class SQLiteStorageBackend:
    """Factory and migration boundary for an instance-local SQLite state DB."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        _configure_connection(conn)
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            self._initialize_connection(conn)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def unit_of_work(self) -> Iterator[SQLiteUnitOfWork]:
        self.initialize()
        with SQLiteUnitOfWork(self.db_path) as uow:
            yield uow

    @contextmanager
    def graph_repository(self) -> Iterator[SQLiteGraphRepository]:
        conn = self.connect()
        try:
            self._initialize_connection(conn)
            conn.commit()
            yield SQLiteGraphRepository(conn)
        finally:
            conn.close()

    @contextmanager
    def snapshot_repository(self) -> Iterator[SQLiteSnapshotRepository]:
        conn = self.connect()
        try:
            self._initialize_connection(conn)
            conn.commit()
            yield SQLiteSnapshotRepository(conn)
        finally:
            conn.close()

    def has_migration(self, migration_id: str) -> bool:
        conn = self.connect()
        try:
            self._initialize_connection(conn)
            conn.commit()
            return self.has_migration_on_connection(conn, migration_id)
        finally:
            conn.close()

    @staticmethod
    def has_migration_on_connection(conn: sqlite3.Connection, migration_id: str) -> bool:
        row = conn.execute(
            "SELECT migration_id FROM storage_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _schema_is_current(conn: sqlite3.Connection) -> bool:
        """Cheap, lock-free "is this database already at this version?" check.

        Deliberately reads only what the upgrade path WRITES: every migration
        id it stamps, plus one proof column per ALTERING migration -- the 0004
        table rebuild, the 0005 column add, and the 0009 column add. The
        migration row alone would be satisfied by a half-applied change that a
        torn write left behind; the column alone would be satisfied by a
        fresh-schema database that never stamped.

        Any missing table (a brand-new file has no ``storage_migrations``)
        answers "not current" rather than raising -- absence is exactly the
        signal that initialization has work to do.
        """
        try:
            applied = {
                str(row["migration_id"])
                for row in conn.execute("SELECT migration_id FROM storage_migrations")
            }
        except sqlite3.OperationalError:
            return False
        if not applied.issuperset(_ALL_STORAGE_MIGRATIONS):
            return False
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(graph_relationships)")}
        if "claim_id" not in columns:
            return False
        return True

    @staticmethod
    def mark_migration_on_connection(conn: sqlite3.Connection, migration_id: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO storage_migrations(migration_id, applied_at) VALUES (?, ?)",
            (migration_id, format_datetime(utc_now())),
        )

    def _initialize_connection(self, conn: sqlite3.Connection) -> None:
        """Create/upgrade every table under ONE migration lock.

        THE LOCK: ``BEGIN IMMEDIATE`` takes SQLite's RESERVED write lock on the
        connection that then performs the whole upgrade, and it is taken BEFORE
        any schema statement runs. That closes the two-process race the previous
        sequence permitted, where two initializers could interleave DDL and a
        concurrent reader (a snapshot, say) could observe a half-upgraded
        database. A second initializer blocks on the lock (``busy_timeout``) and,
        when it wins it, sees the migration row already recorded and does
        nothing. The migration is therefore observed as wholly-old or
        wholly-new, never mid-upgrade.

        The lock is why NOTHING here may call ``executescript``: it commits any
        pending transaction, which would release the lock mid-upgrade. Every
        store routes its DDL through ``storage.schema.execute_schema_script``
        instead. ``_configure_connection`` runs BEFORE ``BEGIN`` because
        ``PRAGMA journal_mode`` / ``foreign_keys`` are no-ops inside a
        transaction.

        THE LOCK IS TAKEN ONLY WHEN THERE IS WORK TO DO. Every read path runs
        through here (``graph_repository``, ``snapshot_repository``, and the
        ~15 ``_ensure_state_initialized`` call sites), so taking a write lock
        unconditionally made an ORDINARY READ fail ``database is locked``
        whenever any writer held a transaction longer than ``busy_timeout``.
        ``_schema_is_current`` is a cheap read OUTSIDE any transaction; when it
        says the database is already at this version we return having begun
        nothing. The check is repeated INSIDE the lock so a second initializer
        that queued behind the real upgrade still does nothing, which is what
        keeps the wholly-old/wholly-new atomicity property intact.
        """
        _configure_connection(conn)
        if self._schema_is_current(conn):
            return
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            if self._schema_is_current(conn):
                # Another initializer won the race and completed the upgrade
                # while we queued on the lock. Nothing left to do.
                return
        execute_schema_script(conn, _GRAPH_SCHEMA)
        execute_schema_script(conn, _SNAPSHOT_SCHEMA)
        SQLiteReceiptStore(self.db_path, connection=conn)
        ResolutionContractStore(self.db_path, connection=conn)
        for migration_id in (_UNIFIED_STATE_MIGRATION, SNAPSHOT_SCHEMA_MIGRATION):
            row = conn.execute(
                "SELECT migration_id FROM storage_migrations WHERE migration_id = ?",
                (migration_id,),
            ).fetchone()
            if row is None:
                self.mark_migration_on_connection(conn, migration_id)
        if not self.has_migration_on_connection(conn, READ_REVISION_MIGRATION):
            # Backfill for pre-revision state DBs: seed the monotonic counter
            # from the snapshot count (every snapshot was a mutation commit),
            # so existing instances start with a plausible, non-zero history.
            # INSERT OR IGNORE keeps any value already present.
            conn.execute(
                "INSERT OR IGNORE INTO instance_state(key, value_json, updated_at) "
                "SELECT ?, CAST((SELECT COUNT(*) FROM snapshots) AS TEXT), ?",
                (READ_REVISION_STATE_KEY, format_datetime(utc_now())),
            )
            self.mark_migration_on_connection(conn, READ_REVISION_MIGRATION)
        if not self.has_migration_on_connection(conn, CLAIM_IDENTITY_MIGRATION):
            self._migrate_claim_identity(conn)
            self.mark_migration_on_connection(conn, CLAIM_IDENTITY_MIGRATION)

    @staticmethod
    def _migrate_claim_identity(conn: sqlite3.Connection) -> None:
        """Rebuild ``graph_relationships`` around ``claim_id`` (migration 0004).

        ONE atomic table rebuild, inside the caller's migration lock/transaction:
        the primary key moves from the derived ``relationship_id`` string
        (``"edge:{key}"`` -- which silently repointed across pulls, because
        ``edge_key`` is a per-load counter) to the minted, immutable
        ``claim_id``, one id per existing row. The derived column is DELETED, not
        kept alongside: a second, contradictory identity column is exactly the
        legacy cruft this work removes.

        Historical attestation, feedback, and group-conflict records need NO
        value rewrite: they resolve tuple-first, so they keep resolving. The new
        record-time stamp columns are additive and only new records carry them.

        A database created fresh on this version already has the new shape (the
        schema above declares it); this runs only for a database that still
        carries the old PK.

        THE MINTED IDS ARE SEEDED INTO THE RECONCILE MAP, in this same
        transaction. Without that, an upgraded overlay whose upstream is still
        pre-identity would find an EMPTY map on its next pull and re-mint an id
        for every upstream tuple it had just been given one for -- staling every
        record-time stamp while every recorded content digest stayed identical.
        The map is what makes the id this migration mints the id that survives.
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(graph_relationships)")}
        if "claim_id" in columns:
            return

        rows = conn.execute(
            "SELECT edge_key, from_type, from_id, to_type, to_id, relationship_type, "
            "properties_json, metadata_json FROM graph_relationships ORDER BY edge_key"
        ).fetchall()
        identity_map: dict[tuple[str, str, str, str, str], list[str]] = {}
        conn.execute("ALTER TABLE graph_relationships RENAME TO graph_relationships_pre_0004")
        # Indexes follow the table on RENAME, so drop them before recreating the
        # canonical names on the rebuilt table.
        for index_name in (
            "idx_graph_relationships_from",
            "idx_graph_relationships_to",
            "idx_graph_relationships_type",
            "idx_graph_relationships_identity",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        execute_schema_script(conn, _GRAPH_RELATIONSHIPS_SCHEMA)
        for row in rows:
            claim_id = mint_claim_id()
            # Rows arrive ORDER BY edge_key, so parallel edges on one tuple are
            # seeded in the same positional order a later backfill assigns.
            identity_map.setdefault(
                (
                    row["relationship_type"],
                    row["from_type"],
                    row["from_id"],
                    row["to_type"],
                    row["to_id"],
                ),
                [],
            ).append(claim_id)
            conn.execute(
                "INSERT INTO graph_relationships "
                "(claim_id, edge_key, from_type, from_id, to_type, to_id, "
                "relationship_type, properties_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim_id,
                    row["edge_key"],
                    row["from_type"],
                    row["from_id"],
                    row["to_type"],
                    row["to_id"],
                    row["relationship_type"],
                    row["properties_json"],
                    row["metadata_json"],
                ),
            )
        conn.execute("DROP TABLE graph_relationships_pre_0004")
        if identity_map:
            conn.execute(
                "INSERT INTO instance_state(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value_json = excluded.value_json, updated_at = excluded.updated_at",
                (
                    LEGACY_CLAIM_IDENTITY_MAP_STATE_KEY,
                    json.dumps(dump_legacy_identity_map(identity_map), sort_keys=True),
                    format_datetime(utc_now()),
                ),
            )
