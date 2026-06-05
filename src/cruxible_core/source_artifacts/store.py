"""SQLite persistence for source artifact manifests, chunks, and archives."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from cruxible_core.source_artifacts.types import (
    SourceArtifactChunk,
    SourceArtifactRecord,
)

_SCHEMA = """\
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
    created_at TEXT NOT NULL
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


class SourceArtifactStoreProtocol(ABC):
    """Interface for source artifact manifests and optional archived content."""

    @abstractmethod
    def save_artifact(
        self,
        record: SourceArtifactRecord,
        chunks: list[SourceArtifactChunk],
        *,
        archive_content: bytes | None = None,
        archive_media_type: str = "text/markdown",
    ) -> str: ...

    @abstractmethod
    def get_artifact(self, source_artifact_id: str) -> SourceArtifactRecord | None: ...
    @abstractmethod
    def list_chunks(self, source_artifact_id: str) -> list[SourceArtifactChunk]: ...
    @abstractmethod
    def get_chunk(
        self,
        source_artifact_id: str,
        chunk_id: str,
    ) -> SourceArtifactChunk | None: ...
    @abstractmethod
    def find_chunks(
        self,
        source_artifact_id: str,
        *,
        heading_path: list[str],
        block_selector: str,
    ) -> list[SourceArtifactChunk]: ...
    @abstractmethod
    def get_archive_content(self, content_hash: str) -> bytes | None: ...
    @abstractmethod
    def close(self) -> None: ...


class SourceArtifactStore(SourceArtifactStoreProtocol):
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
            self._conn.executescript(_SCHEMA)

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
            "archive_content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
