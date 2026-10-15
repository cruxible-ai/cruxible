"""Resident index connections and pooled WAL snapshot readers.

A derived index keeps one connection per file per process (no connect, pragmas
or setup probes per operation), and a working database hands idle read-only
connections to later snapshots. Each must change only the cost, never what is
read or written.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_core.indexes.acquisition import (
    SnapshotPool,
    WorkingDatabaseChangedError,
    open_working_snapshot,
)
from cruxible_core.indexes.history.history_index import commit_working_write, working_file_stamp
from cruxible_core.indexes.resident import ResidentConnections, close_idle_connections


class _Refused(Exception):
    pass


def _owner() -> tuple[ResidentConnections, list[Path]]:
    opened: list[Path] = []
    lock = threading.RLock()
    return ResidentConnections(lock=lambda: lock, capacity=2), opened


def _opener(path: Path, opened: list[Path]):  # type: ignore[no-untyped-def]
    def open_connection() -> sqlite3.Connection:
        opened.append(path)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.execute("CREATE TABLE IF NOT EXISTS t (v)")
        connection.commit()
        return connection

    return open_connection


def _use(owner: ResidentConnections, path: Path, opened: list[Path], value: object = None) -> int:
    with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (connection, _):
        if value is not None:
            connection.execute("INSERT INTO t VALUES (?)", (value,))
        return int(connection.execute("SELECT count(*) FROM t").fetchone()[0])


def test_one_connect_and_setup_serve_every_operation_and_each_commits(tmp_path: Path) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    for value in range(5):
        _use(owner, path, opened, value)
    assert opened == [path]
    # Each operation committed: another connection sees every row.
    with sqlite3.connect(path) as other:
        assert other.execute("SELECT count(*) FROM t").fetchone()[0] == 5


def test_a_raising_operation_rolls_back_and_a_nested_one_shares_its_transaction(
    tmp_path: Path,
) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    with pytest.raises(RuntimeError):
        with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (outer, top):
            assert top
            outer.execute("INSERT INTO t VALUES (1)")
            with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (
                inner,
                nested_top,
            ):
                assert inner is outer and not nested_top
                inner.execute("INSERT INTO t VALUES (2)")
            # The nested block did not commit the outer block's writes.
            with sqlite3.connect(path) as other:
                assert other.execute("SELECT count(*) FROM t").fetchone()[0] == 0
            raise RuntimeError("abandon")
    assert _use(owner, path, opened) == 0
    assert opened == [path]


def test_a_deleted_or_replaced_file_is_reopened_and_set_up_again(tmp_path: Path) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    assert _use(owner, path, opened, "kept") == 1
    path.unlink()
    assert _use(owner, path, opened) == 0  # a new file, set up again
    replacement = tmp_path / "other.sqlite3"
    with sqlite3.connect(replacement) as other:
        other.execute("CREATE TABLE t (v)")
        other.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    os.replace(replacement, path)
    assert _use(owner, path, opened) == 3
    assert opened == [path, path, path]


def test_a_symlinked_index_is_refused(tmp_path: Path) -> None:
    owner, opened = _owner()
    target = tmp_path / "target.sqlite3"
    sqlite3.connect(target).close()
    path = tmp_path / "index.sqlite3"
    path.symlink_to(target)
    with pytest.raises(_Refused):
        _use(owner, path, opened)
    assert opened == []


def test_past_the_capacity_the_oldest_idle_connection_closes_never_one_in_use(
    tmp_path: Path,
) -> None:
    owner, opened = _owner()
    paths = [tmp_path / f"{name}.sqlite3" for name in "abc"]
    with owner.transaction(paths[0], _opener(paths[0], opened), not_regular=_Refused) as (
        held,
        _,
    ):
        _use(owner, paths[1], opened)
        _use(owner, paths[2], opened)  # a third file: the idle oldest (b) goes
        held.execute("SELECT 1").fetchone()  # the one in use stays open
    _use(owner, paths[0], opened)
    _use(owner, paths[1], opened)
    assert opened == [paths[0], paths[1], paths[2], paths[1]]


def test_a_forked_child_never_uses_its_parents_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    _use(owner, path, opened)
    real = os.getpid()
    monkeypatch.setattr(os, "getpid", lambda: real + 1)
    _use(owner, path, opened)
    assert opened == [path, path]


@pytest.mark.parametrize("keep_mtime", [False, True])
def test_a_resident_connection_never_serves_cached_rows_after_an_in_place_restore(
    tmp_path: Path, keep_mtime: bool
) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    original = _restorable(path, "before", wal=False)
    backup = _restorable(tmp_path / "backup.sqlite3", "after!", wal=False)
    assert original[24:28] == backup[24:28] and len(original) == len(backup)

    def read() -> object:
        with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (c, _):
            return c.execute("SELECT v FROM t").fetchone()

    assert read() == ("before",)
    times = path.stat()
    path.write_bytes(backup)  # a restore copied over the same inode
    if keep_mtime:
        os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
    assert path.stat().st_ino == times.st_ino and _fresh(path) == ("after!",)
    assert read() == ("after!",)
    assert opened == [path, path]


def test_a_capture_envelope_index_sees_an_in_place_restore(tmp_path: Path) -> None:
    from cruxible_core.storage.capture_envelope_index import _SCHEMA, CaptureEnvelopeIndex

    root = tmp_path / "cas"
    root.mkdir()
    path = root / "capture-envelopes.sqlite3"

    def index_file(target: Path, digest: str) -> bytes:
        with sqlite3.connect(target) as connection:
            connection.executescript(_SCHEMA)
            connection.execute("INSERT INTO backfill_complete VALUES (1)")
            connection.execute("INSERT INTO envelopes VALUES ('contract',?)", (digest,))
        connection.close()
        return target.read_bytes()

    before, after = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    original = index_file(path, before)
    backup = index_file(tmp_path / "backup.sqlite3", after)
    assert original[24:28] == backup[24:28]
    index = CaptureEnvelopeIndex(SimpleNamespace(root=root))  # type: ignore[arg-type]
    assert index.captures(("contract",), limit=10) == ((before,), True)
    path.write_bytes(backup)
    assert index.captures(("contract",), limit=10) == ((after,), True)
    close_idle_connections()


def test_a_forked_child_never_nests_into_its_parents_active_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    real = os.getpid()
    with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (parent, _):
        parent.execute("INSERT INTO t VALUES ('parent, uncommitted')")
        with monkeypatch.context() as patch:
            patch.setattr(os, "getpid", lambda: real + 1)
            with owner.transaction(path, _opener(path, opened), not_regular=_Refused) as (
                child,
                outermost,
            ):
                # Its own connection, in its own outermost transaction.
                assert child is not parent and outermost
                assert child.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    assert opened == [path, path]


def test_closing_idle_connections_makes_the_next_operation_connect_again(tmp_path: Path) -> None:
    owner, opened = _owner()
    path = tmp_path / "index.sqlite3"
    _use(owner, path, opened)
    close_idle_connections()
    _use(owner, path, opened)
    assert opened == [path, path]


# -- pooled WAL snapshot readers ------------------------------------------------------


def _wal_database(path: Path, rows: int) -> sqlite3.Connection:
    writer = sqlite3.connect(path, check_same_thread=False)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("CREATE TABLE t (v)")
    writer.executemany("INSERT INTO t VALUES (?)", [(index,) for index in range(rows)])
    writer.commit()
    return writer


def _snapshot(path: Path, pool: SnapshotPool) -> sqlite3.Connection:
    stamp = working_file_stamp(path)
    assert stamp is not None
    return open_working_snapshot(
        path, expected_stamp=stamp, file_stamp=lambda: working_file_stamp(path), pool=pool
    )


def _owner_commit(writer: sqlite3.Connection, path: Path, pool: SnapshotPool, sql: str) -> None:
    """A commit the way an owner makes one: under the writer lock, announced to its pool."""

    writer.execute("BEGIN IMMEDIATE")
    before = working_file_stamp(path)
    writer.execute(sql)
    commit_working_write(writer, lambda: working_file_stamp(path), before, snapshots=pool)
    writer.rollback()


def test_a_released_snapshot_connection_follows_its_owners_commits(tmp_path: Path) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 2)
    pool = SnapshotPool()
    first = _snapshot(path, pool)
    assert first.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    pool.release(first)
    _owner_commit(writer, path, pool, "INSERT INTO t VALUES (99)")
    _owner_commit(writer, path, pool, "INSERT INTO t VALUES (100)")
    second = _snapshot(path, pool)
    # The same connection, in a new snapshot that sees the announced rows.
    assert second is first
    assert second.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    with pytest.raises(sqlite3.OperationalError):
        second.execute("INSERT INTO t VALUES (1)")  # still read-only
    pool.release(second)
    pool.close()
    writer.close()


def test_an_unannounced_commit_closes_the_idle_connection_instead_of_reusing_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 2)
    pool = SnapshotPool()
    first = _snapshot(path, pool)
    pool.release(first)
    writer.execute("INSERT INTO t VALUES (99)")  # another writer: the pool was not told
    writer.commit()
    second = _snapshot(path, pool)
    assert second is not first
    assert second.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")
    pool.release(second)
    pool.close()
    writer.close()


def _restorable(path: Path, value: str, *, wal: bool) -> bytes:
    """A one-row database whose header (change counter included) any other one matches."""

    with sqlite3.connect(path) as connection:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.execute("INSERT INTO t VALUES (?)", (value,))
    connection.close()
    return path.read_bytes()


def _fresh(path: Path) -> object:
    oracle = sqlite3.connect(path)
    try:
        return oracle.execute("SELECT v FROM t").fetchone()
    finally:
        oracle.close()


@pytest.mark.parametrize("wal", [False, True])
@pytest.mark.parametrize("keep_mtime", [False, True])
def test_a_pooled_reader_never_serves_cached_rows_after_an_in_place_restore(
    tmp_path: Path, wal: bool, keep_mtime: bool
) -> None:
    path = tmp_path / "working.sqlite3"
    original = _restorable(path, "before", wal=wal)
    backup = _restorable(tmp_path / "backup.sqlite3", "after!", wal=wal)
    assert original[24:28] == backup[24:28] and len(original) == len(backup)
    pool = SnapshotPool()
    first = _snapshot(path, pool)
    assert first.execute("SELECT v FROM t").fetchone() == ("before",)
    pool.release(first)
    inode, times = path.stat().st_ino, path.stat()
    path.write_bytes(backup)  # a restore copied over the same inode
    if keep_mtime:
        os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))  # as `cp -p` would
    assert path.stat().st_ino == inode and _fresh(path) == ("after!",)
    second = _snapshot(path, pool)
    try:
        assert second is not first
        assert second.execute("SELECT v FROM t").fetchone() == ("after!",)
    finally:
        pool.release(second)
        pool.close()


def test_a_forked_child_never_takes_its_parents_pooled_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 1)
    pool = SnapshotPool()
    parent = _snapshot(path, pool)
    held = _snapshot(path, pool)  # checked out in the parent when it forks
    pool.release(parent)
    real = os.getpid()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: real + 1)
        child = _snapshot(path, pool)
        assert child is not parent and child is not held
        pool.release(child)
        pool.release(held)  # the parent's: kept, never closed or rolled back here
        assert held.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    assert parent.execute("SELECT count(*) FROM t").fetchone()[0] == 1  # never closed
    pool.close()
    writer.close()


def test_a_real_fork_opens_a_fresh_snapshot_connection_in_the_child(tmp_path: Path) -> None:
    script = """
import os, sqlite3, sys
from pathlib import Path
from cruxible_core.indexes.acquisition import SnapshotPool, open_working_snapshot
from cruxible_core.indexes.history.history_index import working_file_stamp
path = Path(sys.argv[1])
with sqlite3.connect(path) as connection:
    connection.execute("CREATE TABLE t(v)")
pool = SnapshotPool()
def snapshot():
    return open_working_snapshot(
        path, expected_stamp=working_file_stamp(path),
        file_stamp=lambda: working_file_stamp(path), pool=pool,
    )
first = snapshot()
pool.release(first)
read, write = os.pipe()
pid = os.fork()
if pid == 0:
    os.close(read)
    try:
        second = snapshot()
        os.write(write, b"inherited" if second is first else b"fresh")
    finally:
        os._exit(0)
os.close(write)
result = os.read(read, 100)
os.waitpid(pid, 0)
assert first.execute("SELECT count(*) FROM t").fetchone() == (0,)
pool.close()
print(result.decode())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "fork.sqlite3")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip() == "fresh", result


def test_a_pooled_connection_is_never_reused_for_a_replaced_file(tmp_path: Path) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 2)
    pool = SnapshotPool()
    first = _snapshot(path, pool)
    pool.release(first)
    forged = tmp_path / "forged.sqlite3"
    _wal_database(forged, 7).close()
    writer.close()
    os.replace(forged, path)
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    second = _snapshot(path, pool)
    assert second is not first
    assert second.execute("SELECT count(*) FROM t").fetchone()[0] == 7
    # The stale idle connection was closed, not kept.
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        first.execute("SELECT 1")
    pool.release(second)
    pool.close()


def test_a_pooled_snapshot_whose_stamp_moves_refuses_and_stays_reusable(tmp_path: Path) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 1)
    pool = SnapshotPool()
    pool.release(_snapshot(path, pool))
    stamp = working_file_stamp(path)
    assert stamp is not None
    moved = iter((stamp,))
    with pytest.raises(WorkingDatabaseChangedError):
        open_working_snapshot(
            path,
            expected_stamp=stamp,
            file_stamp=lambda: next(moved, (*stamp[:-1], -1)),
            pool=pool,
        )
    again = _snapshot(path, pool)
    assert again.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    pool.release(again)
    pool.close()
    writer.close()


def test_a_closed_pool_keeps_nothing(tmp_path: Path) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 1)
    pool = SnapshotPool()
    pool.close()
    connection = _snapshot(path, pool)
    pool.release(connection)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    writer.close()
