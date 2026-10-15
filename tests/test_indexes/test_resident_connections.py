"""Resident index connections and pooled WAL snapshot readers.

A derived index keeps one connection per file per process (no connect, pragmas
or setup probes per operation), and a working database hands idle read-only
connections to later snapshots. Each must change only the cost, never what is
read or written.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from cruxible_core.indexes.acquisition import (
    SnapshotPool,
    WorkingDatabaseChangedError,
    open_working_snapshot,
)
from cruxible_core.indexes.history.history_index import working_file_stamp
from cruxible_core.indexes.resident import ResidentConnections, close_idle_connections


class _Refused(Exception):
    pass


def _owner() -> tuple[ResidentConnections, list[Path]]:
    opened: list[Path] = []
    return ResidentConnections(lock=threading.RLock(), capacity=2), opened


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


def test_a_released_snapshot_connection_serves_the_next_snapshot_at_a_new_stamp(
    tmp_path: Path,
) -> None:
    path = tmp_path / "working.sqlite3"
    writer = _wal_database(path, 2)
    pool = SnapshotPool()
    first = _snapshot(path, pool)
    assert first.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    pool.release(first)
    writer.execute("INSERT INTO t VALUES (99)")
    writer.commit()
    second = _snapshot(path, pool)
    # The same connection, in a new snapshot that sees the new row.
    assert second is first
    assert second.execute("SELECT count(*) FROM t").fetchone()[0] == 3
    with pytest.raises(sqlite3.OperationalError):
        second.execute("INSERT INTO t VALUES (1)")  # still read-only
    pool.release(second)
    pool.close()
    writer.close()


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
