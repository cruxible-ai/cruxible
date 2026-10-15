"""One open SQLite connection per derived index file per process, set up once.

A disposable index (the journal's record locator, the Capture envelope index)
was opened, configured, schema-checked and closed on every operation: a connect
and a handful of setup statements per call, which is per record on an append
path. A process now keeps one connection per index file it uses and runs the
setup once, when it opens it. Each operation still ends its own transaction, so
between operations a resident connection holds no lock and no snapshot, and
other connections (other processes, tests) see and make changes as before.

One ``lstat`` per operation proves the connection still has the file the path
names open: a deleted or replaced file is reopened (and set up again), and a
connection inherited across a fork is never used by the child. The oldest idle
connection is closed past the capacity; one in use never is.

Each owner passes the lock that serializes its operations; ``transaction`` holds
it, so a connection is used by one thread at a time.

Tests that count SQL work through a wrapped ``sqlite3.connect`` call
``close_idle_connections`` first, so the next operation connects again, through
the wrapper.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Resident:
    connection: sqlite3.Connection
    #: The file the connection opened: (device, inode).
    identity: tuple[int, int]
    pid: int
    #: Nesting depth of ``transaction`` blocks using it right now.
    depth: int = 0


def _identity(path: Path, not_regular: Callable[[], BaseException]) -> tuple[int, int] | None:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode):
        raise not_regular()
    return (status.st_dev, status.st_ino)


#: Every owner in this process, for ``close_idle_connections``.
_OWNERS: weakref.WeakSet[ResidentConnections] = weakref.WeakSet()


class ResidentConnections:
    """A process's resident connections to index files, most recently used last."""

    def __init__(self, *, lock: threading.RLock, capacity: int = 16) -> None:
        self._entries: OrderedDict[str, _Resident] = OrderedDict()
        self._capacity = capacity
        self._lock = lock
        _OWNERS.add(self)

    def _close(self, entry: _Resident) -> None:
        if entry.pid == os.getpid():
            entry.connection.close()

    def _resident(
        self,
        path: Path,
        open_connection: Callable[[], sqlite3.Connection],
        not_regular: Callable[[], BaseException],
    ) -> _Resident:
        key = str(path)
        entry = self._entries.get(key)
        if entry is not None and entry.depth > 0:
            return entry
        current = _identity(path, not_regular)
        if entry is not None and (
            entry.pid != os.getpid() or current is None or entry.identity != current
        ):
            del self._entries[key]
            self._close(entry)
            entry = None
        if entry is not None:
            self._entries.move_to_end(key)
            return entry
        connection = open_connection()
        try:
            opened = _identity(path, not_regular)
            if opened is None or (current is not None and opened != current):
                raise sqlite3.DatabaseError("index file changed while it was opened")
        except BaseException:
            connection.close()
            raise
        entry = _Resident(connection=connection, identity=opened, pid=os.getpid())
        self._entries[key] = entry
        idle = [name for name, item in self._entries.items() if item.depth == 0 and name != key]
        for old in idle[: max(0, len(self._entries) - self._capacity)]:
            self._close(self._entries.pop(old))
        return entry

    @contextmanager
    def transaction(
        self,
        path: Path,
        open_connection: Callable[[], sqlite3.Connection],
        *,
        not_regular: Callable[[], BaseException],
    ) -> Iterator[tuple[sqlite3.Connection, bool]]:
        """The resident connection and whether this block is the outermost one using it.

        The outermost block commits when it exits and rolls back when it raises;
        a nested block shares its transaction. ``open_connection`` connects and
        runs the one-time setup. The owner's lock is held throughout.
        """

        with self._lock:
            entry = self._resident(path, open_connection, not_regular)
            outermost = entry.depth == 0
            entry.depth += 1
            try:
                yield entry.connection, outermost
                if outermost:
                    entry.connection.commit()
            except BaseException:
                if outermost:
                    entry.connection.rollback()
                raise
            finally:
                entry.depth -= 1

    def close_idle(self) -> None:
        """Close every idle resident connection (shutdown and tests)."""

        with self._lock:
            for key in [name for name, item in self._entries.items() if item.depth == 0]:
                self._close(self._entries.pop(key))


def close_idle_connections() -> None:
    """Close every idle resident index connection and pooled snapshot reader here.

    The next operation on each connects (and sets up) again: tests that count
    SQL work through a wrapped ``sqlite3.connect`` call this before measuring.
    """

    from cruxible_core.indexes.acquisition import close_idle_snapshots

    for owner in list(_OWNERS):
        owner.close_idle()
    close_idle_snapshots()


__all__ = ["ResidentConnections", "close_idle_connections"]
