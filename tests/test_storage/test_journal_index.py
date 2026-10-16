"""Warm event reads and interrupted index publication preserve retained authority."""

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import JournalIntegrityError
from cruxible_core.exhaust.backends import LocalJournalBackend
from cruxible_core.exhaust.journal_index import JournalIndex
from tests.test_storage.test_journal_backends import _activate, _append, _backend, _draft, _stream


def test_index_rebuild_and_warm_exact_reads(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    expected = tuple(_append(backend, str(i)) for i in range(12))
    backend.index.path.unlink()
    assert backend.select_records(_stream(), run_id="run-a") == expected
    original = LocalJournalBackend._read_frames
    offsets = []

    def frames(*args, **kwargs):
        offsets.append(kwargs.get("offset", 0))
        yield from original(*args, **kwargs)

    monkeypatch.setattr(LocalJournalBackend, "_read_frames", staticmethod(frames))
    reopened = LocalJournalBackend(backend.root)
    selected = reopened.range_from_sequences(
        _stream(), "runs-2026-08", first_sequence=8, last_sequence=8
    )
    assert reopened.read_exact_range(selected) == (expected[7],)
    assert offsets and all(offset > 0 for offset in offsets)
    assert reopened.read_head(_stream(), "runs-2026-08").sequence == 12


def test_append_response_lost_before_index_update_is_recovered(tmp_path: Path, monkeypatch) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    first = _append(backend, "first")

    def crash_after_write(self, *args, **kwargs):
        raise RuntimeError("crash")

    with monkeypatch.context() as patch:
        patch.setattr(JournalIndex, "sync_appended", crash_after_write)
        with pytest.raises(RuntimeError, match="crash"):
            _append(backend, "second")
    reopened = LocalJournalBackend(backend.root)
    records = reopened.select_records(_stream(), run_id="run-a")
    assert len(records) == 2
    assert records[0] == first
    assert not tuple(reopened.index.dirty.iterdir())
    assert len(reopened.select_records(_stream(), run_id="run-a")) == 2


def test_selected_bytes_are_verified_not_served_from_sql(tmp_path: Path) -> None:
    backend = _backend(tmp_path, "journal")
    _activate(backend)
    _append(backend, "first")
    path = backend._record_log_path_for_testing(_stream(), "runs-2026-08")
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"run-a"', b'"run-b"'))
    with pytest.raises(JournalIntegrityError):
        backend.select_records(_stream(), run_id="run-a")


# -- history scaling: every run lookup is one bounded index range ------------------


def _run_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_core.exhaust.records import JournalStreamIdentityV1

    backend = LocalJournalBackend(tmp_path)
    stream = JournalStreamIdentityV1(
        instance_id="inst", journal_family="procedure-exhaust-v1", stream_id="procedures"
    )
    key = canonical_bytes(stream.model_dump(mode="json")).decode()
    sequences: dict[str, int] = {}

    def record(partition: str, run_id: str, kind: str, occurrence: str | None = None) -> None:
        sequences[partition] = sequences.get(partition, 0) + 1
        with backend.index.connection() as conn:
            conn.execute(
                "INSERT INTO records(stream,partition_id,sequence,offset,size,digest,previous,"
                "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    partition,
                    sequences[partition],
                    0,
                    0,
                    "d",
                    "p",
                    kind,
                    run_id,
                    occurrence,
                    "sha256:0",
                    "2026-09-01T00:00:00Z",
                ),
            )

    return backend, stream, key, record


def _reference_counts(conn, key: str, partition: str | None) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    where = "a.stream=? AND a.event_kind='admission_bound' AND a.run_id IS NOT NULL"
    args: list[str] = [key]
    if partition is not None:
        where += " AND a.partition_id=?"
        args.append(partition)
    row = conn.execute(
        "SELECT count(*), coalesce(sum(NOT EXISTS(SELECT 1 FROM records f "
        "WHERE f.stream=a.stream AND f.run_id=a.run_id "
        "AND f.event_kind='attempt_finalized')),0) FROM records a WHERE " + where,
        args,
    ).fetchone()
    return int(row[0]), int(row[1])


def test_run_counts_and_open_runs_track_inserts_and_partition_rebuilds(tmp_path: Path) -> None:
    backend, stream, key, record = _run_world(tmp_path)
    for index in range(6):
        partition = "line:a" if index % 2 else "line:b"
        record(partition, f"RUN-{index}", "admission_bound", f"occ-{index}")
        record(partition, f"RUN-{index}", "node_fired")
        if index % 3:
            record(partition, f"RUN-{index}", "attempt_finalized")
    # A run admitted in one partition and finalized from another.
    record("line:a", "RUN-x", "admission_bound")
    record("direct:x", "RUN-x", "attempt_finalized")

    def check() -> None:
        for partition in (None, "line:a", "line:b", "direct:x"):
            with backend.index.connection() as conn:
                expected = _reference_counts(conn, key, partition)
            assert backend.index.run_counts(stream, partition_id=partition) == expected
            running, _more = backend.index.run_locators(
                stream, limit=50, partition_id=partition, running_only=True
            )
            assert len(running) == expected[1]
            assert all(not item.finished for item in running)

    check()
    assert backend.index.run_counts(stream) == (7, 2)
    # Replacing a partition deletes its rows: the finalization of RUN-x goes away
    # with it, so RUN-x is running again until the rebuilt rows land.
    with backend.index.connection() as conn:
        conn.execute("DELETE FROM records WHERE partition_id='direct:x'")
    check()
    assert backend.index.run_counts(stream) == (7, 3)
    with backend.index.connection() as conn:
        conn.execute("DELETE FROM records WHERE partition_id='line:b'")
    check()
    assert backend.index.run_counts(stream, partition_id="line:b") == (0, 0)


def test_an_index_written_under_an_older_schema_is_rebuilt_from_the_journal(
    tmp_path: Path,
) -> None:
    import sqlite3

    backend = _backend(tmp_path, "journal")
    _activate(backend)
    expected = tuple(_append(backend, str(i)) for i in range(3))
    with backend.index.connection() as conn:
        identity = conn.execute("SELECT identity FROM index_identity").fetchone()[0]
    # A file from the previous schema: no version, no derived run tables, and
    # rows the old code indexed (here: none, so a stale read would miss them).
    backend.index.path.unlink()
    raw = sqlite3.connect(backend.index.path)
    raw.executescript(_SCHEMA_V1)
    raw.execute("INSERT INTO index_identity VALUES(1,?)", (identity,))
    raw.execute("INSERT INTO initialized VALUES(1)")
    raw.commit()
    raw.close()

    assert backend.select_records(_stream(), run_id="run-a") == expected
    with backend.index.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2
        assert conn.execute("SELECT identity FROM index_identity").fetchone()[0] != identity
        assert conn.execute("SELECT count(*) FROM open_runs").fetchone()[0] == 0


_SCHEMA_V1 = """
CREATE TABLE initialized (singleton INTEGER PRIMARY KEY);
CREATE TABLE index_identity (singleton INTEGER PRIMARY KEY, identity TEXT NOT NULL);
CREATE TABLE partitions (
 stream TEXT NOT NULL, partition_id TEXT NOT NULL, end_offset INTEGER NOT NULL,
 sequence INTEGER NOT NULL, digest TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(stream, partition_id));
CREATE TABLE records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, partition_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL,
 digest TEXT NOT NULL, previous TEXT NOT NULL, event_kind TEXT NOT NULL,
 run_id TEXT, occurrence_id TEXT, payload_digest TEXT NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(stream, partition_id, sequence));
CREATE INDEX records_occurrence ON records(stream, partition_id, occurrence_id, event_kind);
CREATE TABLE capture_selectors (
 record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
 contract_digest TEXT NOT NULL);
CREATE TABLE capture_progress(stream TEXT PRIMARY KEY, record_id INTEGER NOT NULL);
"""


def _plans(monkeypatch, run) -> list[tuple[str, list[str]]]:  # type: ignore[no-untyped-def]
    import sqlite3

    statements: list[tuple[str, tuple]] = []

    class Recording(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):  # type: ignore[no-untyped-def,override]
            statements.append((sql, tuple(parameters)))
            return super().execute(sql, parameters)

    from cruxible_core.indexes.resident import close_idle_connections

    original = sqlite3.connect
    # The index connection is resident: close it so the run reconnects, recorded.
    close_idle_connections()
    monkeypatch.setattr(
        sqlite3, "connect", lambda path, **kwargs: original(path, factory=Recording, **kwargs)
    )
    run()
    monkeypatch.setattr(sqlite3, "connect", original)
    raw = original(":memory:")
    from cruxible_core.exhaust.journal_index import _rebuild_schema

    _rebuild_schema(raw)
    plans = []
    for sql, args in statements:
        if not sql.lstrip().upper().startswith("SELECT"):
            continue
        plans.append((sql, [row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + sql, args)]))
    return plans


def test_run_reads_use_bounded_index_ranges(tmp_path: Path, monkeypatch) -> None:
    """EXPLAIN QUERY PLAN pins: no run read scans history or the admission kind.

    Before: run_counts searched every attempt_finalized row per admission
    (records_kind_position), the node count searched every node_fired row per
    run, and an occurrence lookup walked the Line partition by sequence.
    """

    backend, stream, _key_text, record = _run_world(tmp_path)
    record("line:a", "RUN-1", "admission_bound", "occ-1")

    def run() -> None:
        backend.index.run_counts(stream)
        backend.index.run_counts(stream, partition_id="line:a")
        backend.index.run_locators(stream, limit=5)
        backend.index.run_locators(stream, limit=5, partition_id="line:a")
        backend.index.run_locators(stream, limit=5, running_only=True)
        backend.index.run_locators(stream, limit=5, partition_id="line:a", running_only=True)
        backend.index.run_locators(stream, limit=1, run_id="RUN-1")
        backend.index.run_ids_with_prefix(stream, "RUN", limit=5)
        backend.index.select(
            stream,
            partition_id="line:a",
            event_kind="admission_bound",
            occurrence_id="occ-none",
            descending=True,
            limit=1,
        )
        backend.index.select(stream, run_id="RUN-1", event_kind="terminal_egress")
        backend.index.invocation_runs(stream, bodies=None, invocation_ids=("sha256:" + "0" * 64,))

    plans = [
        (sql, plan)
        for sql, plan in _plans(monkeypatch, run)
        if "FROM records" in sql
        or "FROM open_runs" in sql
        or "FROM run_tallies" in sql
        or "FROM invocation_pending" in sql
    ]
    assert len(plans) >= 10
    for sql, plan in plans:
        assert not any(line.startswith("SCAN") for line in plan), (sql, plan)
        if "node_fired" in sql or "attempt_finalized" in sql:
            assert not any(
                "records_kind_position" in line
                and line.startswith("SEARCH f")
                or line.startswith("SEARCH n")
                and "records_kind_position" in line
                for line in plan
            ), (sql, plan)
    counts = [plan for sql, plan in plans if "run_tallies" in sql or "count(*) FROM open" in sql]
    assert counts and all("records" not in " ".join(plan) for plan in counts)
    occurrence = next(plan for sql, plan in plans if "occurrence_id=" in sql)
    assert any("records_occurrence" in line for line in occurrence), occurrence
    # The pending-start catch-up is driven by the pending rows of the stream:
    # empty, it visits nothing else.
    pending = next(plan for sql, plan in plans if "FROM invocation_pending p" in sql)
    assert pending[0].startswith(
        "SEARCH p USING COVERING INDEX invocation_pending_stream (stream=?)"
    ), pending
    assert not any("records_stream_position" in line for line in pending), pending
    running = [plan for sql, plan in plans if "open_runs o" in sql]
    assert len(running) == 2
    assert all(plan[0].startswith("SEARCH o USING COVERING INDEX open_runs") for plan in running)


def test_catching_up_reads_only_the_records_since_the_last_sync(
    tmp_path: Path, monkeypatch
) -> None:
    import sqlite3

    backend = _backend(tmp_path, "journal")
    _activate(backend)
    for index in range(10):
        _append(backend, f"indexed-{index}")
    with monkeypatch.context() as patch:
        # Appends whose post-write index update never ran (another process's
        # append the index has not reached yet).
        patch.setattr(JournalIndex, "sync_appended", lambda *args, **kwargs: None)
        patch.setattr(JournalIndex, "mark_dirty", lambda self, *args: None)
        backend.append_many(
            tuple(_draft(f"unsynced-{index}") for index in range(3)),
            expected_head=backend.read_head(_stream(), "runs-2026-08"),
            fencing_token="writer-a",
        )
    with sqlite3.connect(backend.index.path) as connection:
        indexed = connection.execute("SELECT count(*) FROM records").fetchone()[0]
    missing = 13 - indexed
    assert 1 <= missing < 13
    original = LocalJournalBackend._read_frames
    read: list[int] = []

    def frames(*args, **kwargs):
        for frame in original(*args, **kwargs):
            read.append(1)
            yield frame

    monkeypatch.setattr(LocalJournalBackend, "_read_frames", staticmethod(frames))
    backend.catch_up_index(_stream())
    # A miss costs the records since the last sync, not the journal.
    assert len(read) == missing
    with sqlite3.connect(backend.index.path) as connection:
        assert connection.execute("SELECT count(*) FROM records").fetchone()[0] == 13
    read.clear()
    backend.catch_up_index(_stream())
    assert read == []
