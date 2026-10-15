"""Short namespace proofs while SQLite acquires files by pathname."""

from __future__ import annotations

import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from cruxible_client.contracts.errors import ProjectionIntegrityError


class DatabasePathChangedError(ProjectionIntegrityError):
    """Acquisition raced with a namespace change; retry without changing source state."""


class WorkingDatabaseChangedError(ProjectionIntegrityError):
    """The working database's physical stamp moved while a snapshot was acquired.

    Under the writer lock that is an integrity failure. A lock-free reader races
    ordinary writers, so it falls back to its locked path instead.
    """


# An unrelated entry change in any ancestor refuses an attempt exactly as a swap
# does, and a shared system temporary directory changes many times a second. A
# bounded retry with a short growing pause outlasts that churn instead of
# spending every attempt inside one burst; a swap repeated on every attempt still
# exhausts the bound and refuses. The pause is never taken after the last attempt.
ACQUISITION_ATTEMPTS = 8
_ACQUISITION_PAUSE_CAP_SECONDS = 0.05


def pause_before_acquisition_retry(attempt: int) -> None:
    """Wait before retrying after `attempt` (zero-based) raised DatabasePathChangedError."""

    time.sleep(min(0.001 * (2**attempt), _ACQUISITION_PAUSE_CAP_SECONDS))


def _ancestor_stamp(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    lexical = path.absolute().parent
    resolved = lexical.resolve(strict=True)
    parents = {lexical, *lexical.parents, resolved, *resolved.parents}
    result = []
    for parent in sorted(parents):
        value = parent.lstat()
        result.append((str(parent), value.st_dev, value.st_ino, value.st_ctime_ns))
    return tuple(result)


@contextmanager
def guard_database_path(path: Path) -> Iterator[None]:
    """Detect ancestor replacement, including a directory swapped back after open.

    Main-file stamps alone miss an ancestor rename: the file's inode and ctime
    stay unchanged. Renaming the ancestor changes its own ctime. Both lexical
    and resolved ancestors cover symlink spellings as well as their targets.
    Unrelated directory-entry changes can conservatively refuse this short
    acquisition; they do not establish that any source or database row changed.
    """

    try:
        before = _ancestor_stamp(path)
    except OSError as exc:
        raise DatabasePathChangedError("database namespace is unavailable; retry required") from exc
    yield
    try:
        after = _ancestor_stamp(path)
    except OSError as exc:
        raise DatabasePathChangedError(
            "database namespace changed during acquisition; retry required"
        ) from exc
    if after != before:
        raise DatabasePathChangedError(
            "database namespace changed during acquisition; retry required"
        )


def _snapshot_identity(stamp: tuple[int, ...]) -> tuple[int, ...]:
    """The files a stamp names: the database's and, when it has one, the WAL's device and inode."""

    return (*stamp[:2], *stamp[5:7])


class SnapshotPool:
    """Idle read-only connections to one working database, reused for later WAL snapshots.

    A pooled snapshot is the same proof as a fresh one. Its connection was opened
    by pathname under ``guard_database_path`` while the stamp it was acquired at
    named exactly the database (and WAL) files it opened; it is handed out again
    only for a stamp naming those same files (device and inode), and the caller's
    expected stamp is checked before and after the new snapshot is pinned, as on
    a fresh connection. A file replaced since is never read through an old
    connection: its idle connections are closed instead.

    Released connections end their read transaction, so an idle one pins no WAL
    frames and holds no lock a writer or a checkpoint waits on. The owner closes
    the pool before its own shutdown checkpoint.
    """

    def __init__(self, capacity: int = 4) -> None:
        self._lock = threading.Lock()
        self._idle: list[tuple[sqlite3.Connection, tuple[int, ...]]] = []
        self._out: dict[int, tuple[int, ...]] = {}
        self._capacity = capacity
        self._closed = False
        _POOLS.add(self)

    def _take(self, stamp: tuple[int, ...]) -> sqlite3.Connection | None:
        identity = _snapshot_identity(stamp)
        stale: list[sqlite3.Connection] = []
        found = None
        with self._lock:
            while self._idle and found is None:
                connection, opened = self._idle.pop()
                if opened == identity:
                    found = connection
                    self._out[id(connection)] = opened
                else:
                    stale.append(connection)
        for connection in stale:
            connection.close()
        return found

    def _adopt(self, connection: sqlite3.Connection, stamp: tuple[int, ...]) -> None:
        with self._lock:
            self._out[id(connection)] = _snapshot_identity(stamp)

    def release(self, connection: sqlite3.Connection) -> None:
        """End the connection's snapshot; keep it for the next one, or close it."""

        with self._lock:
            opened = self._out.pop(id(connection), None)
        if opened is not None:
            try:
                connection.rollback()
                connection.row_factory = None
            except sqlite3.Error:
                opened = None
        if opened is not None:
            with self._lock:
                if not self._closed and len(self._idle) < self._capacity:
                    self._idle.append((connection, opened))
                    return
        connection.close()

    def discard(self, connection: sqlite3.Connection) -> None:
        """Close a connection whose snapshot failed: it is never reused."""

        with self._lock:
            self._out.pop(id(connection), None)
        connection.close()

    def clear(self) -> None:
        """Close the idle connections; later snapshots connect, and are pooled, again."""

        with self._lock:
            idle, self._idle = self._idle, []
        for connection, _opened in idle:
            connection.close()

    def close(self) -> None:
        """Close the idle connections and pool no more (the owner is shutting down)."""

        with self._lock:
            self._closed = True
        self.clear()


#: Every pool in this process, for ``close_idle_snapshots``.
_POOLS: weakref.WeakSet[SnapshotPool] = weakref.WeakSet()


def close_idle_snapshots() -> None:
    """Close every pool's idle connections (``resident.close_idle_connections``)."""

    for pool in list(_POOLS):
        pool.clear()


def _pin_pooled_snapshot(
    pool: SnapshotPool,
    *,
    expected_stamp: tuple[int, ...],
    file_stamp: Callable[[], tuple[int, ...] | None],
) -> sqlite3.Connection | None:
    connection = pool._take(expected_stamp)
    if connection is None:
        return None
    try:
        if file_stamp() != expected_stamp:
            raise WorkingDatabaseChangedError(
                "working database changed before snapshot acquisition"
            )
        connection.execute("BEGIN DEFERRED")
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        if file_stamp() != expected_stamp:
            raise WorkingDatabaseChangedError(
                "working database changed during snapshot acquisition"
            )
    except WorkingDatabaseChangedError:
        pool.release(connection)
        raise
    except BaseException:
        pool.discard(connection)
        raise
    return connection


def open_working_snapshot(
    path: Path,
    *,
    expected_stamp: tuple[int, ...],
    file_stamp: Callable[[], tuple[int, ...] | None],
    pool: SnapshotPool | None = None,
) -> sqlite3.Connection:
    """Pin a WAL snapshot only while its verified physical proof remains unchanged.

    With ``pool``, an idle connection to the same files is reused (no connect)
    and the returned connection goes back through ``pool.release``.
    """

    if pool is not None:
        pooled = _pin_pooled_snapshot(pool, expected_stamp=expected_stamp, file_stamp=file_stamp)
        if pooled is not None:
            return pooled
    for attempt in range(ACQUISITION_ATTEMPTS):
        connection = None
        try:
            with guard_database_path(path):
                if file_stamp() != expected_stamp:
                    raise WorkingDatabaseChangedError(
                        "working database changed before snapshot acquisition"
                    )
                # A pooled connection may serve a later snapshot on another thread;
                # it is only ever used by one thread at a time.
                connection = sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True, check_same_thread=False
                )
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN DEFERRED")
                # BEGIN does not acquire the WAL snapshot. Read while the path
                # proof and the exact physical stamp still protect acquisition.
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                if file_stamp() != expected_stamp:
                    raise WorkingDatabaseChangedError(
                        "working database changed during snapshot acquisition"
                    )
            if pool is not None:
                pool._adopt(connection, expected_stamp)
            return connection
        except DatabasePathChangedError:
            if connection is not None:
                connection.close()
            if attempt == ACQUISITION_ATTEMPTS - 1:
                raise
            pause_before_acquisition_retry(attempt)
        except BaseException:
            if connection is not None:
                connection.close()
            raise
    raise AssertionError("snapshot acquisition retry bound is unreachable")
