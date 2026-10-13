"""The sequenced log of body-store landings: the change record for CAS availability.

Every body that lands in the store (bar journal-record payloads, reachable only
through the record that names them) and every erase is logged here twice, under
an increasing sequence: once before the object is written or unlinked, so a
landing is never missing from the log whatever the process does next, and once
after, so a reader that consumed the first row while the write was in flight
sees the digest again once its object is final. A derived answer that consulted
a digest is recomputed from the rows that name it; the log's head is the
availability part of the verdict-input fingerprint.

A row says "this digest may have changed", never what it now holds: readers
consult the store. The log is append-only and carries a random identity minted
with it, so a cursor kept against one log is never read against a replacement.
Writes go to a write-ahead log without a sync per commit, as the token file this
replaces was written: a power loss can drop the newest rows, never reorder them,
and the evidence sweep re-checks every consulted body's identity.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.errors import CasError

LandingKind = Literal["store", "stored", "erase", "erased"]
LOG_NAME = "landings.sqlite3"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS log (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS landings (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, digest TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('store','stored','erase','erased'))
) STRICT;
"""
_LOCK = threading.Lock()


@dataclass(frozen=True)
class LandingHead:
    """Where the log stands: its identity and last sequence (``""`` and 0 before any row)."""

    identity: str
    sequence: int


@dataclass(frozen=True)
class Landing:
    sequence: int
    digest: str
    kind: LandingKind


class LandingLog:
    """The landing log beside one body store's root."""

    def __init__(self, root: Path) -> None:
        self.path = root / LOG_NAME

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            if self.path.is_symlink():
                raise CasError("the CAS landing log must be a regular file")
            connection = sqlite3.connect(self.path, timeout=30)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(_SCHEMA)
                connection.execute("INSERT OR IGNORE INTO log VALUES (1,?)", (uuid.uuid4().hex,))
                yield connection
                connection.commit()
            finally:
                connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection | None]:
        if self.path.is_symlink():
            raise CasError("the CAS landing log must be a regular file")
        if not self.path.is_file():
            yield None
            return
        # An existing log only: reading never creates one. A WAL reader needs the
        # shared-memory sidecar, which a read-only open could not create.
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            yield connection
        finally:
            connection.close()

    def note(self, digest: str, kind: LandingKind) -> int:
        """Append one row and return its sequence."""

        try:
            with self._writer() as connection:
                cursor = connection.execute(
                    "INSERT INTO landings(digest,kind) VALUES (?,?)", (digest, kind)
                )
                assert cursor.lastrowid is not None
                return int(cursor.lastrowid)
        except sqlite3.Error as exc:
            raise CasError("CAS landing could not be logged") from exc

    def head(self) -> LandingHead:
        """The log's identity and newest sequence: one indexed read, no write."""

        with self._reader() as connection:
            if connection is None:
                return LandingHead("", 0)
            try:
                identity = connection.execute("SELECT identity FROM log").fetchone()
                (sequence,) = connection.execute(
                    "SELECT coalesce(max(sequence),0) FROM landings"
                ).fetchone()
            except sqlite3.Error as exc:
                raise CasError("CAS landing log is unreadable") from exc
        return LandingHead("" if identity is None else str(identity[0]), int(sequence))

    def after(self, sequence: int, *, through: int | None = None) -> tuple[Landing, ...]:
        """Every row after ``sequence`` (through ``through``), in order: a range read."""

        with self._reader() as connection:
            if connection is None:
                return ()
            try:
                rows = connection.execute(
                    "SELECT sequence,digest,kind FROM landings WHERE sequence>? AND sequence<=? "
                    "ORDER BY sequence",
                    (sequence, (1 << 62) if through is None else through),
                ).fetchall()
            except sqlite3.Error as exc:
                raise CasError("CAS landing log is unreadable") from exc
        return tuple(Landing(int(row[0]), str(row[1]), row[2]) for row in rows)


__all__ = ["LOG_NAME", "Landing", "LandingHead", "LandingKind", "LandingLog"]
