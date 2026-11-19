"""Disposable worker state: a SQLite projection a findings kind can always rebuild.

A findings kind keeps its cursors, queues and findings in one database under
the instance's exhaust directory. Everything in it is derived from the logs the
kind follows, so a database whose schema is not exactly the kind's own -- a
file an earlier version wrote, or one damaged by hand -- is dropped and created
again, and the kind starts over as it would on a new instance. Nothing is
migrated: the logs are the migration.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_LOCKS: dict[Path, threading.Lock] = {}
#: The thread holding each path's lock: opening it again there would wait forever.
_HOLDERS: dict[Path, int] = {}
_LOCKS_GUARD = threading.Lock()


class StateBusyError(RuntimeError):
    """A bounded read's remaining budget elapsed before the state lock was free.

    Raised only when ``open`` is given a ``timeout``; an unbounded open never
    raises this and instead waits for the worker transaction to finish.
    """


def _schema_rows(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


class DisposableState:
    def __init__(self, directory: str, schema: str) -> None:
        self.directory = directory
        self.schema = schema
        with sqlite3.connect(":memory:") as reference:
            reference.executescript(schema)
            self.expected = _schema_rows(reference)

    def path(self, instance: Any) -> Path:
        exhaust = Path(instance.root) / str(instance.descriptor.storage.exhaust)
        return exhaust / self.directory / "state.sqlite3"

    @contextmanager
    def open(
        self, instance: Any, *, create: bool = True, timeout: float | None = None
    ) -> Iterator[sqlite3.Connection | None]:
        """One transaction on the state, rebuilt first if its schema is not this one.

        ``create=False`` is a reader's open: it never creates the database, never
        rebuilds a schema that is not this one (the state is absent to it, None,
        until the worker rebuilds), and never commits: whatever it writes, as a
        read's rolled-back overlay does, is rolled back.

        ``timeout``, when given, bounds both the wait for this path's own lock
        and SQLite's independent busy wait to that many seconds (never less
        than zero): a caller on its own wait budget raises ``StateBusyError``
        rather than blocking past it, with nothing opened or held. ``None``
        (the default) waits for the worker transaction to finish, as every
        write and every unbounded read already did.
        """

        path = self.path(instance)
        if not create and not path.is_file():
            yield None
            return
        # Two first opens may race (orient and next in parallel): never check-then-create.
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = path.resolve()
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(key, threading.Lock())
            if _HOLDERS.get(key) == threading.get_ident():
                # Fail at once rather than deadlock on this thread's own lock.
                raise RuntimeError(f"the {self.directory} state is already open on this thread")
        budget = None if timeout is None else max(0.0, timeout)
        acquired = lock.acquire() if budget is None else lock.acquire(timeout=budget)
        if not acquired:
            raise StateBusyError(
                f"the {self.directory} state lock was still held past the given timeout"
            )
        try:
            with _LOCKS_GUARD:
                _HOLDERS[key] = threading.get_ident()
            connection = sqlite3.connect(path, timeout=30 if budget is None else budget)
            try:
                if _schema_rows(connection) != self.expected:
                    if not create:
                        connection.rollback()
                        yield None
                        return
                    self._rebuild(connection)
                yield connection
                if create:
                    connection.commit()
                else:
                    connection.rollback()
            finally:
                connection.close()
                with _LOCKS_GUARD:
                    _HOLDERS.pop(key, None)
        finally:
            lock.release()

    def reset(self, instance: Any) -> None:
        """Drop every table and create the schema again: an operator's repair."""

        with self.open(instance) as connection:
            assert connection is not None
            self._rebuild(connection)

    def _rebuild(self, connection: sqlite3.Connection) -> None:
        # Dropping a table drops its indexes and triggers. The schema seeds
        # every summary from the empty tables it creates, so the counts are
        # exact the moment the rebuild commits.
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        drops = "".join(f'DROP TABLE IF EXISTS "{name}";' for name in tables)
        connection.executescript(f"BEGIN IMMEDIATE;{drops}{self.schema}COMMIT;")
