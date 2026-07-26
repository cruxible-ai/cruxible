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

from cruxible_core.attestation.store import AttestationStore
from cruxible_core.decision.store import DecisionStore
from cruxible_core.errors import MutationError
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.governance.actors import dump_actor_context, load_actor_context
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.legacy_identity import dump_legacy_identity_map
from cruxible_core.graph.types import EntityInstance, RelationshipInstance, mint_claim_id
from cruxible_core.group.store import GroupStore
from cruxible_core.primitives import canonical_json
from cruxible_core.procedure.store import ProcedureStore
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.resolution_contracts.store import ResolutionContractStore
from cruxible_core.snapshot.types import StateSnapshot
from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol
from cruxible_core.source_artifacts.types import (
    SourceArtifactChunk,
    SourceArtifactRecord,
)
from cruxible_core.sqlite_ddl import execute_schema_script
from cruxible_core.storage.protocols import (
    GraphRepositoryProtocol,
    SnapshotRepositoryProtocol,
    UnitOfWorkProtocol,
)
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

# Source artifacts are insert-only revisions of a logical id. The partial
# unique index is what makes "one current manifest per logical id" a database
# guarantee rather than a service-layer convention: two concurrent
# registrations cannot both leave a non-superseded row behind, so the duplicate
# check and the insert cannot drift apart across connections.
_SOURCE_ARTIFACT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS source_artifacts (
    artifact_revision_id TEXT PRIMARY KEY,
    source_artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    source_kind TEXT NOT NULL,
    source_retention TEXT NOT NULL,
    original_uri TEXT,
    label TEXT,
    parser_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    byte_count INTEGER NOT NULL,
    local_path TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    archive_content_hash TEXT,
    created_at TEXT NOT NULL,
    registered_actor_context TEXT,
    superseded_by TEXT,
    superseded_at TEXT,
    drift_observed_hash TEXT,
    drift_observed_at TEXT,
    -- STICKY: set once, on the first drift ever observed for this revision, and
    -- never cleared. The pair above is CURRENT state and is legitimately
    -- cleared when the file matches the manifest again; without this pair,
    -- restoring the original bytes also erased every trace that the evidence
    -- base had been tampered with in between.
    first_drift_observed_hash TEXT,
    first_drift_observed_at TEXT,
    UNIQUE (source_artifact_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_kind
    ON source_artifacts(source_kind);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_content_hash
    ON source_artifacts(content_hash);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_logical_id
    ON source_artifacts(source_artifact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_artifacts_current_revision
    ON source_artifacts(source_artifact_id) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS source_artifact_chunks (
    artifact_revision_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    heading_path_json TEXT NOT NULL,
    block_selector TEXT NOT NULL,
    block_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    preview TEXT,
    label TEXT,
    PRIMARY KEY (artifact_revision_id, chunk_id),
    FOREIGN KEY (artifact_revision_id)
        REFERENCES source_artifacts(artifact_revision_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_artifact_chunks_locator
    ON source_artifact_chunks(artifact_revision_id, block_selector);

CREATE TABLE IF NOT EXISTS source_artifact_archives (
    content_hash TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    content BLOB NOT NULL,
    byte_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
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
# Read paths persist query receipts and decision-record audit events, so writes
# to these tables must NOT advance read_revision (reads never bump it).
_AUDIT_ONLY_TABLES = frozenset(
    {
        "receipts",
        "receipt_entities",
        "execution_traces",
        "procedure_runs",
        "procedure_evidence_artifacts",
        "procedure_run_evidence",
        "decision_events",
        "instance_state",
        "storage_migrations",
    }
)


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


class SQLiteSourceArtifactStore(SourceArtifactStoreProtocol):
    """Stores source artifact manifests, parsed chunks, and optional source copies."""

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
            # Migration runs first: the revisioned schema script declares
            # indexes over columns a pre-revision table does not have.
            self._migrate_to_revisioned_artifacts()
            execute_schema_script(self._conn, _SOURCE_ARTIFACT_SCHEMA)
            self._ensure_actor_context_columns()
            self._ensure_first_drift_columns()

    def _artifact_columns(self) -> set[str]:
        return {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(source_artifacts)").fetchall()
        }

    def _ensure_actor_context_columns(self) -> None:
        if "registered_actor_context" not in self._artifact_columns():
            self._conn.execute(
                "ALTER TABLE source_artifacts ADD COLUMN registered_actor_context TEXT"
            )

    def _ensure_first_drift_columns(self) -> None:
        """Additive: the sticky first-drift pair on a DB that predates it."""
        columns = self._artifact_columns()
        if not columns:
            return
        for column in ("first_drift_observed_hash", "first_drift_observed_at"):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE source_artifacts ADD COLUMN {column} TEXT")

    def _migrate_to_revisioned_artifacts(self) -> None:
        """Rebuild a pre-revision state DB onto the insert-only schema.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing single-row-per-id
        table untouched, so without this an upgraded instance would keep
        writing overwrite-shaped rows against code that assumes revisions.
        Every existing manifest becomes revision 1 and the current head.
        """
        columns = self._artifact_columns()
        if not columns or "artifact_revision_id" in columns:
            return
        # The oldest state DBs predate the actor column too; add it so the
        # copy below can read a uniform legacy shape.
        self._ensure_actor_context_columns()
        execute_schema_script(
            self._conn,
            """
            ALTER TABLE source_artifacts RENAME TO source_artifacts_pre_revision;
            ALTER TABLE source_artifact_chunks RENAME TO source_artifact_chunks_pre_revision;
            """,
        )
        execute_schema_script(self._conn, _SOURCE_ARTIFACT_SCHEMA)
        self._conn.execute(
            "INSERT INTO source_artifacts "
            "(artifact_revision_id, source_artifact_id, revision, source_kind, "
            "source_retention, original_uri, label, parser_version, content_hash, "
            "byte_count, local_path, archived, archive_content_hash, created_at, "
            "registered_actor_context) "
            "SELECT source_artifact_id || '@1', source_artifact_id, 1, source_kind, "
            "source_retention, original_uri, label, parser_version, content_hash, "
            "byte_count, local_path, archived, archive_content_hash, created_at, "
            "registered_actor_context FROM source_artifacts_pre_revision"
        )
        self._conn.execute(
            "INSERT INTO source_artifact_chunks "
            "(artifact_revision_id, chunk_id, heading_path_json, block_selector, "
            "block_type, content_hash, line_start, line_end, preview, label) "
            "SELECT source_artifact_id || '@1', chunk_id, heading_path_json, block_selector, "
            "block_type, content_hash, line_start, line_end, preview, label "
            "FROM source_artifact_chunks_pre_revision"
        )
        execute_schema_script(
            self._conn,
            """
            DROP TABLE source_artifact_chunks_pre_revision;
            DROP TABLE source_artifacts_pre_revision;
            """,
        )

    def save_artifact(
        self,
        record: SourceArtifactRecord,
        chunks: list[SourceArtifactChunk],
        *,
        archive_content: bytes | None = None,
        archive_media_type: str = "text/markdown",
    ) -> str:
        """Insert one immutable artifact revision and its chunks. Does NOT commit.

        The previous current revision (if any) is marked superseded by this one
        instead of being overwritten, so the manifest that existing evidence
        refs pinned their content hash against stays readable. Chunks are
        inserted against the new revision id; nothing is deleted, so a pinned
        chunk of an older revision keeps its line ranges and content hash.
        """
        superseded_at = format_datetime(utc_now())
        head = self._current_revision_id(record.source_artifact_id)
        if head is not None:
            if head == record.artifact_revision_id:
                raise MutationError(
                    f"Source artifact revision '{record.artifact_revision_id}' already exists; "
                    "source artifact revisions are insert-only and are never replaced"
                )
            # Clear the old head before inserting the new one: the partial
            # unique index permits exactly one non-superseded row per logical id.
            self._conn.execute(
                "UPDATE source_artifacts SET superseded_by = ?, superseded_at = ? "
                "WHERE artifact_revision_id = ? AND superseded_by IS NULL",
                (record.artifact_revision_id, superseded_at, head),
            )
        try:
            self._conn.execute(
                "INSERT INTO source_artifacts "
                "(artifact_revision_id, source_artifact_id, revision, source_kind, "
                "source_retention, original_uri, label, parser_version, content_hash, "
                "byte_count, local_path, archived, archive_content_hash, created_at, "
                "registered_actor_context, superseded_by, superseded_at, "
                "drift_observed_hash, drift_observed_at, "
                "first_drift_observed_hash, first_drift_observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.artifact_revision_id,
                    record.source_artifact_id,
                    record.revision,
                    record.source_kind,
                    record.source_retention,
                    record.original_uri,
                    record.label,
                    record.parser_version,
                    record.content_hash,
                    record.byte_count,
                    record.local_path,
                    int(record.archived),
                    record.archive_content_hash,
                    record.created_at,
                    json.dumps(dump_actor_context(record.registered_actor_context)),
                    record.superseded_by,
                    record.superseded_at,
                    record.drift_observed_hash,
                    record.drift_observed_at,
                    record.first_drift_observed_hash,
                    record.first_drift_observed_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise MutationError(
                f"Source artifact revision '{record.artifact_revision_id}' conflicts with an "
                "existing revision; source artifact revisions are insert-only"
            ) from exc
        for chunk in chunks:
            self._conn.execute(
                "INSERT INTO source_artifact_chunks "
                "(artifact_revision_id, chunk_id, heading_path_json, block_selector, "
                "block_type, content_hash, line_start, line_end, preview, label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.artifact_revision_id,
                    chunk.chunk_id,
                    json.dumps(chunk.heading_path),
                    chunk.block_selector,
                    chunk.block_type,
                    chunk.content_hash,
                    chunk.line_start,
                    chunk.line_end,
                    chunk.preview,
                    chunk.label,
                ),
            )
        if archive_content is not None:
            if record.archive_content_hash is None:
                raise ValueError("archive_content requires archive_content_hash")
            self._conn.execute(
                "INSERT OR REPLACE INTO source_artifact_archives "
                "(content_hash, media_type, content, byte_count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.archive_content_hash,
                    archive_media_type,
                    sqlite3.Binary(archive_content),
                    len(archive_content),
                    record.created_at,
                ),
            )
        return record.artifact_revision_id

    def _current_revision_id(self, source_artifact_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT artifact_revision_id FROM source_artifacts "
            "WHERE source_artifact_id = ? AND superseded_by IS NULL",
            (source_artifact_id,),
        ).fetchone()
        return str(row["artifact_revision_id"]) if row is not None else None

    def get_artifact(self, source_artifact_id: str) -> SourceArtifactRecord | None:
        """Return the current revision of a logical artifact."""
        row = self._conn.execute(
            "SELECT * FROM source_artifacts WHERE source_artifact_id = ? AND superseded_by IS NULL",
            (source_artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    def get_artifact_revision(self, artifact_revision_id: str) -> SourceArtifactRecord | None:
        """Return one revision by physical id, superseded or not."""
        row = self._conn.execute(
            "SELECT * FROM source_artifacts WHERE artifact_revision_id = ?",
            (artifact_revision_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    def list_artifact_revisions(self, source_artifact_id: str) -> list[SourceArtifactRecord]:
        """Return the full revision history of a logical artifact, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM source_artifacts WHERE source_artifact_id = ? ORDER BY revision",
            (source_artifact_id,),
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def record_content_drift(
        self,
        artifact_revision_id: str,
        *,
        observed_hash: str | None,
        observed_at: str | None,
    ) -> bool:
        """Record (or clear) the last observed local-content drift for a revision.

        Two pairs, deliberately. ``drift_observed_*`` is CURRENT state and is
        cleared when the file matches its manifest again — leaving a stale marker
        on a restored file would misreport the evidence base. ``first_drift_*``
        is STICKY: written once, on the first drift ever seen for this revision,
        and never cleared. Clearing both meant that restoring the original bytes
        also erased every trace that the source had been altered, so the one
        reader who most needs to know — someone auditing whether the evidence
        behind a decision was tampered with — saw a pristine record.

        Idempotent by design: the UPDATE is a no-op once the stored observation
        already matches, so a read path that keeps seeing the same drifted file
        writes exactly once rather than once per read. Returns whether a row
        actually changed.
        """
        cursor = self._conn.execute(
            "UPDATE source_artifacts SET drift_observed_hash = ?, drift_observed_at = ?, "
            # COALESCE keeps the FIRST value: only a NULL is filled in, and only
            # when this call is reporting a drift rather than clearing one.
            "first_drift_observed_hash = COALESCE(first_drift_observed_hash, ?), "
            "first_drift_observed_at = COALESCE(first_drift_observed_at, ?) "
            "WHERE artifact_revision_id = ? AND ("
            "drift_observed_hash IS NOT ? "
            "OR (? IS NOT NULL AND first_drift_observed_hash IS NULL))",
            (
                observed_hash,
                observed_at,
                observed_hash,
                observed_at,
                artifact_revision_id,
                observed_hash,
                observed_hash,
            ),
        )
        changed = cursor.rowcount > 0
        if changed and self._owns_connection:
            # A store opened outside a caller's unit of work owns its
            # transaction, so the observation would be lost on close.
            self._conn.commit()
        return changed

    def list_artifacts(self) -> list[SourceArtifactRecord]:
        """List the current revision of every logical artifact."""
        rows = self._conn.execute(
            "SELECT * FROM source_artifacts WHERE superseded_by IS NULL "
            "ORDER BY created_at DESC, source_artifact_id DESC",
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def list_chunks(self, source_artifact_id: str) -> list[SourceArtifactChunk]:
        rows = self._conn.execute(
            "SELECT c.* FROM source_artifact_chunks c "
            "JOIN source_artifacts a ON a.artifact_revision_id = c.artifact_revision_id "
            "WHERE a.source_artifact_id = ? AND a.superseded_by IS NULL "
            "ORDER BY c.line_start, c.block_selector, c.chunk_id",
            (source_artifact_id,),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def list_revision_chunks(self, artifact_revision_id: str) -> list[SourceArtifactChunk]:
        rows = self._conn.execute(
            "SELECT * FROM source_artifact_chunks WHERE artifact_revision_id = ? "
            "ORDER BY line_start, block_selector, chunk_id",
            (artifact_revision_id,),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def get_chunk(
        self,
        source_artifact_id: str,
        chunk_id: str,
    ) -> SourceArtifactChunk | None:
        row = self._conn.execute(
            "SELECT c.* FROM source_artifact_chunks c "
            "JOIN source_artifacts a ON a.artifact_revision_id = c.artifact_revision_id "
            "WHERE a.source_artifact_id = ? AND a.superseded_by IS NULL AND c.chunk_id = ?",
            (source_artifact_id, chunk_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_chunk(row)

    def find_chunks(
        self,
        source_artifact_id: str,
        *,
        heading_path: list[str],
        block_selector: str,
    ) -> list[SourceArtifactChunk]:
        rows = self._conn.execute(
            "SELECT c.* FROM source_artifact_chunks c "
            "JOIN source_artifacts a ON a.artifact_revision_id = c.artifact_revision_id "
            "WHERE a.source_artifact_id = ? AND a.superseded_by IS NULL "
            "AND c.heading_path_json = ? AND c.block_selector = ? "
            "ORDER BY c.line_start, c.chunk_id",
            (
                source_artifact_id,
                json.dumps(heading_path),
                block_selector,
            ),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def get_archive_content(self, content_hash: str) -> bytes | None:
        row = self._conn.execute(
            "SELECT content FROM source_artifact_archives WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["content"])

    def close(self) -> None:
        if self._owns_connection:
            self._conn.close()

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> SourceArtifactRecord:
        return SourceArtifactRecord(
            source_artifact_id=row["source_artifact_id"],
            artifact_revision_id=row["artifact_revision_id"],
            revision=int(row["revision"]),
            superseded_by=row["superseded_by"],
            superseded_at=row["superseded_at"],
            drift_observed_hash=row["drift_observed_hash"],
            drift_observed_at=row["drift_observed_at"],
            first_drift_observed_hash=row["first_drift_observed_hash"],
            first_drift_observed_at=row["first_drift_observed_at"],
            source_kind=row["source_kind"],
            source_retention=row["source_retention"],
            original_uri=row["original_uri"],
            label=row["label"],
            parser_version=row["parser_version"],
            content_hash=row["content_hash"],
            byte_count=int(row["byte_count"]),
            local_path=row["local_path"],
            archived=bool(row["archived"]),
            archive_content_hash=row["archive_content_hash"],
            created_at=row["created_at"],
            registered_actor_context=load_actor_context(
                json.loads(row["registered_actor_context"])
                if "registered_actor_context" in row.keys() and row["registered_actor_context"]
                else None
            ),
        )

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row) -> SourceArtifactChunk:
        heading_path = json.loads(row["heading_path_json"])
        if not isinstance(heading_path, list):
            heading_path = []
        return SourceArtifactChunk(
            chunk_id=row["chunk_id"],
            heading_path=[str(item) for item in heading_path],
            block_selector=row["block_selector"],
            block_type=row["block_type"],
            content_hash=row["content_hash"],
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            preview=row["preview"],
            label=row["label"],
        )


def _snapshot_artifact_media_type(artifact_name: str) -> str:
    if artifact_name.endswith(".json"):
        return "application/json"
    if artifact_name.endswith((".yaml", ".yml")):
        return "text/yaml"
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
        self.feedback = FeedbackStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.groups = GroupStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.procedures = ProcedureStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.attestations = AttestationStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.resolution_contracts = ResolutionContractStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.decisions = DecisionStore(
            self.db_path,
            connection=self._conn,
            initialize_schema=False,
        )
        self.source_artifacts = SQLiteSourceArtifactStore(
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
        # same transaction. Audit-only writes (receipts, traces, decision
        # events) never bump it, so read paths that persist proof records keep
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
        id it stamps, plus the one column whose presence proves the 0004 table
        rebuild actually ran (the migration row alone would be satisfied by a
        half-applied rebuild that a torn write left behind, and the column alone
        would be satisfied by a fresh-schema database that never stamped).

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
        return "claim_id" in columns

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
        FeedbackStore(self.db_path, connection=conn)
        GroupStore(self.db_path, connection=conn)
        ProcedureStore(self.db_path, connection=conn)
        AttestationStore(self.db_path, connection=conn)
        ResolutionContractStore(self.db_path, connection=conn)
        DecisionStore(self.db_path, connection=conn)
        SQLiteSourceArtifactStore(self.db_path, connection=conn)
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
