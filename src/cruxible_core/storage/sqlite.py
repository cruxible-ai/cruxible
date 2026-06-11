"""SQLite storage backend for durable Cruxible instance state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from cruxible_core.decision.store import DecisionStore
from cruxible_core.feedback.store import FeedbackStore
from cruxible_core.governance.actors import dump_actor_context, load_actor_context
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.store import GroupStore
from cruxible_core.primitives import canonical_json
from cruxible_core.receipt.store import SQLiteReceiptStore
from cruxible_core.snapshot.types import StateSnapshot
from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol
from cruxible_core.source_artifacts.types import (
    SourceArtifactChunk,
    SourceArtifactRecord,
)
from cruxible_core.storage.protocols import (
    GraphRepositoryProtocol,
    SnapshotRepositoryProtocol,
    UnitOfWorkProtocol,
)
from cruxible_core.temporal import format_datetime, utc_now

StorageIntegrityError = sqlite3.IntegrityError

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

CREATE TABLE IF NOT EXISTS graph_relationships (
    relationship_id TEXT PRIMARY KEY,
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

_SOURCE_ARTIFACT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS source_artifacts (
    source_artifact_id TEXT PRIMARY KEY,
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
    registered_actor_context TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_kind
    ON source_artifacts(source_kind);
CREATE INDEX IF NOT EXISTS idx_source_artifacts_content_hash
    ON source_artifacts(content_hash);

CREATE TABLE IF NOT EXISTS source_artifact_chunks (
    source_artifact_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    heading_path_json TEXT NOT NULL,
    block_selector TEXT NOT NULL,
    block_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    preview TEXT,
    label TEXT,
    PRIMARY KEY (source_artifact_id, chunk_id),
    FOREIGN KEY (source_artifact_id)
        REFERENCES source_artifacts(source_artifact_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_source_artifact_chunks_locator
    ON source_artifact_chunks(source_artifact_id, block_selector);

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


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")


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
            "SELECT edge_key, from_type, from_id, to_type, to_id, relationship_type, "
            "properties_json, metadata_json "
            "FROM graph_relationships ORDER BY edge_key"
        ).fetchall():
            edges.append(
                {
                    "relationship_type": row["relationship_type"],
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
        """Replace live graph rows with a full graph image."""
        self._conn.execute("DELETE FROM graph_relationships")
        self._conn.execute("DELETE FROM graph_entities")

        self.upsert_entities(graph.iter_all_entities())
        self.upsert_relationships(
            RelationshipInstance(
                relationship_type=edge["relationship_type"],
                from_type=edge["from_type"],
                from_id=edge["from_id"],
                to_type=edge["to_type"],
                to_id=edge["to_id"],
                edge_key=edge["edge_key"],
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
                    canonical_json(entity.metadata),
                ),
            )

    def upsert_relationships(self, relationships: Iterable[RelationshipInstance]) -> None:
        """Persist relationship rows touched by an incremental mutation."""
        for relationship in relationships:
            edge_key = relationship.edge_key
            if edge_key is None:
                raise ValueError("Incremental relationship writes require a stable edge_key")
            if not isinstance(edge_key, int):
                try:
                    edge_key = int(edge_key)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Graph edge key {edge_key!r} is not stable") from exc
            self._conn.execute(
                "INSERT INTO graph_relationships "
                "(relationship_id, edge_key, from_type, from_id, to_type, to_id, "
                "relationship_type, properties_json, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(relationship_id) DO UPDATE SET "
                "edge_key = excluded.edge_key, "
                "from_type = excluded.from_type, "
                "from_id = excluded.from_id, "
                "to_type = excluded.to_type, "
                "to_id = excluded.to_id, "
                "relationship_type = excluded.relationship_type, "
                "properties_json = excluded.properties_json, "
                "metadata_json = excluded.metadata_json",
                (
                    f"edge:{int(edge_key)}",
                    edge_key,
                    relationship.from_type,
                    relationship.from_id,
                    relationship.to_type,
                    relationship.to_id,
                    relationship.relationship_type,
                    canonical_json(relationship.properties),
                    canonical_json(
                        relationship.metadata.model_dump(mode="json", exclude_none=True)
                    ),
                ),
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
            self._conn.executescript(_SOURCE_ARTIFACT_SCHEMA)
            self._ensure_actor_context_columns()

    def _ensure_actor_context_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(source_artifacts)").fetchall()
        }
        if "registered_actor_context" not in columns:
            self._conn.execute(
                "ALTER TABLE source_artifacts ADD COLUMN registered_actor_context TEXT"
            )

    def save_artifact(
        self,
        record: SourceArtifactRecord,
        chunks: list[SourceArtifactChunk],
        *,
        archive_content: bytes | None = None,
        archive_media_type: str = "text/markdown",
    ) -> str:
        """Persist one artifact manifest and its parsed chunk index. Does NOT commit."""
        self._conn.execute(
            "INSERT OR REPLACE INTO source_artifacts "
            "(source_artifact_id, source_kind, source_retention, original_uri, label, "
            "parser_version, content_hash, byte_count, local_path, archived, "
            "archive_content_hash, created_at, registered_actor_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.source_artifact_id,
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
            ),
        )
        self._conn.execute(
            "DELETE FROM source_artifact_chunks WHERE source_artifact_id = ?",
            (record.source_artifact_id,),
        )
        for chunk in chunks:
            self._conn.execute(
                "INSERT INTO source_artifact_chunks "
                "(source_artifact_id, chunk_id, heading_path_json, block_selector, "
                "block_type, content_hash, line_start, line_end, preview, label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.source_artifact_id,
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
        return record.source_artifact_id

    def get_artifact(self, source_artifact_id: str) -> SourceArtifactRecord | None:
        row = self._conn.execute(
            "SELECT * FROM source_artifacts WHERE source_artifact_id = ?",
            (source_artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_artifact(row)

    def list_chunks(self, source_artifact_id: str) -> list[SourceArtifactChunk]:
        rows = self._conn.execute(
            "SELECT * FROM source_artifact_chunks "
            "WHERE source_artifact_id = ? "
            "ORDER BY line_start, block_selector, chunk_id",
            (source_artifact_id,),
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def get_chunk(
        self,
        source_artifact_id: str,
        chunk_id: str,
    ) -> SourceArtifactChunk | None:
        row = self._conn.execute(
            "SELECT * FROM source_artifact_chunks "
            "WHERE source_artifact_id = ? AND chunk_id = ?",
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
            "SELECT * FROM source_artifact_chunks "
            "WHERE source_artifact_id = ? AND heading_path_json = ? "
            "AND block_selector = ? "
            "ORDER BY line_start, chunk_id",
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

    def rollback(self) -> None:
        if self._conn.in_transaction:
            self._conn.rollback()
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
    def mark_migration_on_connection(conn: sqlite3.Connection, migration_id: str) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO storage_migrations(migration_id, applied_at) VALUES (?, ?)",
            (migration_id, format_datetime(utc_now())),
        )

    def _initialize_connection(self, conn: sqlite3.Connection) -> None:
        _configure_connection(conn)
        conn.executescript(_GRAPH_SCHEMA)
        conn.executescript(_SNAPSHOT_SCHEMA)
        SQLiteReceiptStore(self.db_path, connection=conn)
        FeedbackStore(self.db_path, connection=conn)
        GroupStore(self.db_path, connection=conn)
        DecisionStore(self.db_path, connection=conn)
        SQLiteSourceArtifactStore(self.db_path, connection=conn)
        for migration_id in (_UNIFIED_STATE_MIGRATION, SNAPSHOT_SCHEMA_MIGRATION):
            row = conn.execute(
                "SELECT migration_id FROM storage_migrations WHERE migration_id = ?",
                (migration_id,),
            ).fetchone()
            if row is None:
                self.mark_migration_on_connection(conn, migration_id)
