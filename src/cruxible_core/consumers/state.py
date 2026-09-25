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
_LOCKS_GUARD = threading.Lock()


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
    def open(self, instance: Any, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        """One transaction on the state, rebuilt first if its schema is not this one."""

        path = self.path(instance)
        if not path.parent.exists():
            if not create:
                yield None
                return
            path.parent.mkdir(mode=0o700, parents=True)
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(path.resolve(), threading.Lock())
        with lock:
            connection = sqlite3.connect(path, timeout=30)
            try:
                if _schema_rows(connection) != self.expected:
                    self._rebuild(connection)
                yield connection
                connection.commit()
            finally:
                connection.close()

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
