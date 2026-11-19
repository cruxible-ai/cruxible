"""One open SQLite connection per derived index file per process, set up once.

A disposable index (the journal's record locator, the Capture envelope index)
was opened, configured, schema-checked and closed on every operation: a connect
and a handful of setup statements per call, which is per record on an append
path. A process now keeps one connection per index file it uses and runs the
setup once, when it opens it. Each operation still ends its own transaction, so
between operations a resident connection holds no lock and no snapshot, and
other connections (other processes, tests) see and make changes as before.

Reuse is valid only within the owning process, and only while the file holds
what this connection last left in it. After each operation the file's content
stamp is recorded: its device, inode, size, modification and change times, and
the change counter and version fields of its SQLite header (read through a
descriptor held on the same inode, so no open per operation). The next operation
compares one ``lstat`` and one ``pread`` against it. Any unannounced drift (a
file deleted, replaced, or restored in place over the same inode, or a commit by
another connection) reopens the connection and runs its setup again before
SQLite's cached pages could be used; nothing is ever scanned to decide. A
connection, descriptor or lock inherited across a fork is never used by the
child: it gets fresh ones (and never closes the parent's).

The oldest idle connection is closed past the capacity; one in use never is.
Each owner names the lock that serializes its operations; ``transaction`` holds
it, so a connection is used by one thread at a time. Tests that count SQL work
through a wrapped ``sqlite3.connect`` call ``close_idle_connections`` first, so
the next operation connects again, through the wrapper.
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

#: The SQLite header fields that move with the file's content: the file change
#: counter (bytes 24-27) and the version-valid-for number (92-95).
_HEADER_BYTES = 100

#: A file's content stamp: its stat identity and times, and its header fields.
ContentStamp = tuple[int, int, int, int, int, bytes]


@dataclass
class _Resident:
    connection: sqlite3.Connection
    #: A read-only descriptor on the inode the connection opened (for the header).
    descriptor: int
    pid: int
    #: The content stamp after this process's last operation on the file.
    stamp: ContentStamp | None
    #: Nesting depth of ``transaction`` blocks using it right now.
    depth: int = 0


#: Connections and descriptors a forked child inherited: kept referenced and never
#: closed there (closing them could touch the parent's files or locks).
_INHERITED: list[object] = []


def _content_stamp(path: Path, descriptor: int) -> ContentStamp | None:
    """The file's stamp now, or None when the path no longer names the held inode."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    held = os.fstat(descriptor)
    if (status.st_dev, status.st_ino) != (held.st_dev, held.st_ino):
        return None
    header = os.pread(descriptor, _HEADER_BYTES, 0)
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        header[24:28] + header[92:96],
    )


def _regular(path: Path, not_regular: Callable[[], BaseException]) -> bool:
    """Whether the path names a regular file now; refuses anything else that exists."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(status.st_mode):
        raise not_regular()
    return True


#: Every owner in this process, for ``close_idle_connections`` and the fork reset.
_OWNERS: weakref.WeakSet[ResidentConnections] = weakref.WeakSet()


class ResidentConnections:
    """A process's resident connections to index files, most recently used last."""

    def __init__(self, *, lock: Callable[[], threading.RLock], capacity: int = 16) -> None:
        self._entries: OrderedDict[str, _Resident] = OrderedDict()
        self._capacity = capacity
        #: The owner's current lock (an owner replaces its lock in a forked child).
        self._lock = lock
        self._pid = os.getpid()
        _OWNERS.add(self)

    def _close(self, entry: _Resident) -> None:
        if entry.pid == os.getpid():
            entry.connection.close()
            os.close(entry.descriptor)
        else:
            _INHERITED.append(entry)

    def _forget_parent(self) -> None:
        """In a forked child: never reuse (or close) what the parent opened."""

        if self._pid != os.getpid():
            _INHERITED.extend(self._entries.values())
            self._entries = OrderedDict()
            self._pid = os.getpid()

    def _open(
        self,
        path: Path,
        open_connection: Callable[[], sqlite3.Connection],
        not_regular: Callable[[], BaseException],
    ) -> _Resident:
        connection = open_connection()
        try:
            if not _regular(path, not_regular):
                raise sqlite3.DatabaseError("index file vanished while it was opened")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except BaseException:
            connection.close()
            raise
        # The stamp is taken after the setup committed: what the next operation
        # must find unchanged.
        return _Resident(
            connection=connection,
            descriptor=descriptor,
            pid=os.getpid(),
            stamp=_content_stamp(path, descriptor),
        )

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
        regular = _regular(path, not_regular)
        if entry is not None and (
            not regular
            or entry.stamp is None
            or _content_stamp(path, entry.descriptor) != entry.stamp
        ):
            del self._entries[key]
            self._close(entry)
            entry = None
        if entry is not None:
            self._entries.move_to_end(key)
            return entry
        entry = self._open(path, open_connection, not_regular)
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

        The outermost block commits when it exits and rolls back when it raises,
        then records the file's content stamp; a nested block shares its
        transaction. ``open_connection`` connects and runs the one-time setup.
        The owner's lock is held throughout.
        """

        # Process identity first: a child never nests into, or reuses, a block
        # or connection its parent had.
        self._forget_parent()
        with self._lock():
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
                if outermost:
                    entry.stamp = _content_stamp(path, entry.descriptor)

    def close_idle(self) -> None:
        """Close every idle resident connection (shutdown and tests)."""

        self._forget_parent()
        with self._lock():
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


def _after_fork_in_child() -> None:
    for owner in list(_OWNERS):
        owner._forget_parent()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


__all__ = ["ContentStamp", "ResidentConnections", "close_idle_connections"]
