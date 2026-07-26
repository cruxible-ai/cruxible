"""Storage-level tests for insert-only source artifact revisions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cruxible_core.errors import MutationError
from cruxible_core.source_artifacts.types import (
    MARKDOWN_CHUNKS_V1,
    SourceArtifactChunk,
    SourceArtifactRecord,
)
from cruxible_core.storage.sqlite import SQLiteSourceArtifactStore


def _record(revision: int, content_hash: str) -> SourceArtifactRecord:
    return SourceArtifactRecord(
        source_artifact_id="doc",
        revision=revision,
        source_kind="markdown",
        source_retention="manifest_only",
        parser_version=MARKDOWN_CHUNKS_V1,
        content_hash=content_hash,
        byte_count=len(content_hash),
        created_at="2026-06-05T12:00:00Z",
    )


def _chunk(content_hash: str) -> SourceArtifactChunk:
    return SourceArtifactChunk(
        chunk_id="mdchunk_stable",
        heading_path=["Doc"],
        block_selector="paragraph:1",
        block_type="paragraph",
        content_hash=content_hash,
        line_start=3,
        line_end=3,
    )


@pytest.fixture
def store() -> SQLiteSourceArtifactStore:
    return SQLiteSourceArtifactStore(":memory:")


def test_save_artifact_supersedes_instead_of_replacing(
    store: SQLiteSourceArtifactStore,
) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])
    store.save_artifact(_record(2, "sha256:bbb"), [_chunk("sha256:chunk_b")])

    revisions = store.list_artifact_revisions("doc")
    assert [record.artifact_revision_id for record in revisions] == ["doc@1", "doc@2"]
    assert revisions[0].superseded_by == "doc@2"
    assert revisions[1].superseded_by is None

    head = store.get_artifact("doc")
    assert head is not None and head.content_hash == "sha256:bbb"
    # Chunks follow their own revision rather than being deleted and rebuilt,
    # so a chunk id reused across revisions keeps each revision's content hash.
    assert store.list_revision_chunks("doc@1")[0].content_hash == "sha256:chunk_a"
    assert store.list_revision_chunks("doc@2")[0].content_hash == "sha256:chunk_b"
    assert store.list_chunks("doc")[0].content_hash == "sha256:chunk_b"


def test_reinserting_the_same_revision_id_is_refused(
    store: SQLiteSourceArtifactStore,
) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])

    with pytest.raises(MutationError, match="insert-only"):
        store.save_artifact(_record(1, "sha256:ccc"), [_chunk("sha256:chunk_c")])

    head = store.get_artifact("doc")
    assert head is not None and head.content_hash == "sha256:aaa"


def test_only_one_current_revision_per_logical_id_is_possible(
    store: SQLiteSourceArtifactStore,
) -> None:
    """The head guard is a database constraint, not a service-layer convention."""
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])
    store.save_artifact(_record(2, "sha256:bbb"), [_chunk("sha256:chunk_b")])

    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE source_artifacts SET superseded_by = NULL WHERE artifact_revision_id = 'doc@1'"
        )


def test_list_artifacts_returns_only_current_revisions(
    store: SQLiteSourceArtifactStore,
) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])
    store.save_artifact(_record(2, "sha256:bbb"), [_chunk("sha256:chunk_b")])

    listed = store.list_artifacts()
    assert [record.artifact_revision_id for record in listed] == ["doc@2"]


def test_record_content_drift_is_idempotent(store: SQLiteSourceArtifactStore) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])

    drift = {"observed_hash": "sha256:zzz"}
    assert store.record_content_drift("doc@1", observed_at="t1", **drift) is True
    assert store.record_content_drift("doc@1", observed_at="t2", **drift) is False

    head = store.get_artifact("doc")
    assert head is not None
    assert head.drift_observed_hash == "sha256:zzz"
    assert head.drift_observed_at == "t1"

    assert store.record_content_drift("doc@1", observed_hash=None, observed_at=None) is True
    assert store.record_content_drift("doc@1", observed_hash=None, observed_at=None) is False
    head = store.get_artifact("doc")
    assert head is not None and head.drift_observed_hash is None


_PRE_REVISION_SCHEMA = """\
CREATE TABLE source_artifacts (
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
CREATE TABLE source_artifact_chunks (
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
    PRIMARY KEY (source_artifact_id, chunk_id)
);
"""


def test_pre_revision_state_db_migrates_to_revision_one(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_PRE_REVISION_SCHEMA)
    conn.execute(
        "INSERT INTO source_artifacts (source_artifact_id, source_kind, source_retention, "
        "original_uri, label, parser_version, content_hash, byte_count, local_path, archived, "
        "archive_content_hash, created_at) "
        "VALUES ('legacy_doc', 'markdown', 'manifest_only', 'legacy.md', 'legacy', ?, "
        "'sha256:legacy', 12, '/tmp/legacy.md', 0, NULL, '2026-06-01T00:00:00Z')",
        (MARKDOWN_CHUNKS_V1,),
    )
    conn.execute(
        "INSERT INTO source_artifact_chunks (source_artifact_id, chunk_id, heading_path_json, "
        "block_selector, block_type, content_hash, line_start, line_end, preview, label) "
        "VALUES ('legacy_doc', 'mdchunk_legacy', '[\"Doc\"]', 'paragraph:1', 'paragraph', "
        "'sha256:legacy_chunk', 3, 3, NULL, NULL)"
    )
    conn.commit()
    conn.close()

    store = SQLiteSourceArtifactStore(db_path)
    try:
        head = store.get_artifact("legacy_doc")
        assert head is not None
        assert head.artifact_revision_id == "legacy_doc@1"
        assert head.revision == 1
        assert head.content_hash == "sha256:legacy"
        assert head.superseded_by is None
        assert [chunk.chunk_id for chunk in store.list_chunks("legacy_doc")] == ["mdchunk_legacy"]
    finally:
        store.close()


def test_first_drift_is_sticky_when_the_original_bytes_come_back(
    store: SQLiteSourceArtifactStore,
) -> None:
    """Restoring the file must not erase that it was ever altered.

    ``record_content_drift`` cleared BOTH stored fields on a clean read, so an
    artifact that was tampered with and then put back read as pristine — and the
    one reader who most needs the finding (someone auditing whether the evidence
    behind a decision was altered) saw nothing at all.
    """
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])

    store.record_content_drift(
        "doc@1",
        observed_hash="sha256:tampered",
        observed_at="2026-07-25T10:00:00Z",
    )
    drifted = store.get_artifact_revision("doc@1")
    assert drifted is not None
    assert drifted.drift_observed_hash == "sha256:tampered"
    assert drifted.first_drift_observed_hash == "sha256:tampered"
    assert drifted.first_drift_observed_at == "2026-07-25T10:00:00Z"

    store.record_content_drift("doc@1", observed_hash=None, observed_at=None)

    restored = store.get_artifact_revision("doc@1")
    assert restored is not None
    # CURRENT drift state clears — the file matches its manifest again.
    assert restored.drift_observed_hash is None
    assert restored.drift_observed_at is None
    # The first observation does not.
    assert restored.first_drift_observed_hash == "sha256:tampered"
    assert restored.first_drift_observed_at == "2026-07-25T10:00:00Z"


def test_a_second_drift_does_not_overwrite_the_first_observation(
    store: SQLiteSourceArtifactStore,
) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])
    store.record_content_drift(
        "doc@1", observed_hash="sha256:first", observed_at="2026-07-25T10:00:00Z"
    )
    store.record_content_drift("doc@1", observed_hash=None, observed_at=None)
    store.record_content_drift(
        "doc@1", observed_hash="sha256:second", observed_at="2026-07-25T12:00:00Z"
    )

    record = store.get_artifact_revision("doc@1")
    assert record is not None
    assert record.drift_observed_hash == "sha256:second"
    assert record.first_drift_observed_hash == "sha256:first"
    assert record.first_drift_observed_at == "2026-07-25T10:00:00Z"


def test_a_clean_read_of_a_never_drifted_artifact_writes_nothing(
    store: SQLiteSourceArtifactStore,
) -> None:
    """The read path must stay a read once there is nothing new to observe."""
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])

    assert store.record_content_drift("doc@1", observed_hash=None, observed_at=None) is False


def test_repeating_the_same_drift_observation_writes_once(
    store: SQLiteSourceArtifactStore,
) -> None:
    store.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])

    assert (
        store.record_content_drift(
            "doc@1", observed_hash="sha256:tampered", observed_at="2026-07-25T10:00:00Z"
        )
        is True
    )
    assert (
        store.record_content_drift(
            "doc@1", observed_hash="sha256:tampered", observed_at="2026-07-25T11:00:00Z"
        )
        is False
    )


def test_the_sticky_columns_are_added_to_a_pre_existing_state_db(tmp_path: Path) -> None:
    """An instance upgraded in place gets the columns without losing its rows."""
    db = tmp_path / "state.db"
    first = SQLiteSourceArtifactStore(db)
    try:
        first.save_artifact(_record(1, "sha256:aaa"), [_chunk("sha256:chunk_a")])
        first._conn.execute(
            "UPDATE source_artifacts SET first_drift_observed_hash = NULL, "
            "first_drift_observed_at = NULL"
        )
        first._conn.commit()
    finally:
        first.close()

    dropped = sqlite3.connect(db)
    try:
        dropped.execute("ALTER TABLE source_artifacts DROP COLUMN first_drift_observed_hash")
        dropped.execute("ALTER TABLE source_artifacts DROP COLUMN first_drift_observed_at")
        dropped.commit()
    finally:
        dropped.close()

    second = SQLiteSourceArtifactStore(db)
    try:
        columns = {
            row["name"]
            for row in second._conn.execute("PRAGMA table_info(source_artifacts)").fetchall()
        }
        assert {"first_drift_observed_hash", "first_drift_observed_at"} <= columns
        record = second.get_artifact_revision("doc@1")
        assert record is not None
        assert record.first_drift_observed_hash is None
    finally:
        second.close()
