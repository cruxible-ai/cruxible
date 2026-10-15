"""Every stored Capture envelope, by the contract it names: an index, never a CAS walk.

A canonical Capture envelope sorts its keys, so its bytes open with the contract
digest it names. The body store records that pair here before it writes the
object, so a stored envelope is never missing from the index; objects stored
before the index existed are added once, shard by shard, and that pass is never
repeated. A row is a candidate, not evidence: the object may be any bytes that
open the same way, or be gone, and citation verifies whatever a caller keeps.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from cruxible_client.contracts.errors import CasError
from cruxible_core.indexes.resident import ResidentConnections

if TYPE_CHECKING:
    from cruxible_core.storage.cas import ContentAddressedBodyStore

ENVELOPE_HEAD = b'{"capture_contract_digest":"'
_DIGEST = re.compile(rb"sha256:[0-9a-f]{64}")
#: The bytes that name an envelope's contract: the head, the digest, its quote.
ENVELOPE_HEAD_LENGTH = len(ENVELOPE_HEAD) + len("sha256:") + 64 + 1
_SHARD_NAME = re.compile(r"[0-9a-f]{2}")
_OBJECT_NAME = re.compile(r"[0-9a-f]{64}")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
 contract_digest TEXT NOT NULL, capture_digest TEXT NOT NULL,
 PRIMARY KEY(contract_digest, capture_digest)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS backfilled (shard TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS backfill_complete (singleton INTEGER PRIMARY KEY);
"""
_LOCK = threading.RLock()
#: One connection per index file per process, its schema created once, used under ``_LOCK``.
_RESIDENT = ResidentConnections(lock=lambda: _LOCK)


def _after_fork_in_child() -> None:
    # A lock held by another thread at fork time is never released in the child.
    global _LOCK
    _LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def envelope_contract(head: bytes) -> str | None:
    """The contract digest an object's leading bytes name, if they open an envelope."""

    if not head.startswith(ENVELOPE_HEAD) or len(head) < ENVELOPE_HEAD_LENGTH:
        return None
    named = head[len(ENVELOPE_HEAD) : ENVELOPE_HEAD_LENGTH - 1]
    if (
        not _DIGEST.fullmatch(named)
        or head[ENVELOPE_HEAD_LENGTH - 1 : ENVELOPE_HEAD_LENGTH] != b'"'
    ):
        return None
    return named.decode("ascii")


class CaptureEnvelopeIndex:
    """The contract-to-envelope index beside one body store."""

    def __init__(self, store: ContentAddressedBodyStore) -> None:
        self._store = store
        self.path = store.root / "capture-envelopes.sqlite3"

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        try:
            connection.executescript(_SCHEMA)
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """This process's resident connection to the index, in one transaction per call."""

        with _RESIDENT.transaction(
            self.path,
            self._open,
            not_regular=lambda: CasError("the Capture envelope index must be a regular file"),
        ) as (connection, _outermost):
            yield connection

    def note(self, digest: str, content: bytes) -> None:
        """Record a stored object that opens as an envelope; called before it is written."""

        contract = envelope_contract(content[:ENVELOPE_HEAD_LENGTH])
        if contract is None:
            return
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO envelopes VALUES (?,?)", (contract, digest))

    def _shards(self) -> list[str]:
        return sorted(
            name for name in os.listdir(self._store._root_fd) if _SHARD_NAME.fullmatch(name)
        )

    def _shard_envelopes(self, shard: str) -> list[tuple[str, str]]:
        """The (contract, digest) pairs of one shard's objects that open as envelopes."""

        names: list[str] = []
        descriptor = self._store._shard(shard)
        if descriptor is not None:
            try:
                with os.scandir(descriptor) as entries:
                    names = [
                        entry.name
                        for entry in entries
                        if _OBJECT_NAME.fullmatch(entry.name) and entry.name.startswith(shard)
                    ]
            finally:
                os.close(descriptor)
        rows: list[tuple[str, str]] = []
        for name in names:
            digest = "sha256:" + name
            contract = envelope_contract(self._store.peek(digest, ENVELOPE_HEAD_LENGTH))
            if contract is not None:
                rows.append((contract, digest))
        return rows

    def _backfill(self, connection: sqlite3.Connection) -> None:
        """Add the objects stored before the index existed, once, shard by shard."""

        if connection.execute("SELECT 1 FROM backfill_complete").fetchone():
            return
        done = {row[0] for row in connection.execute("SELECT shard FROM backfilled")}
        for shard in self._shards():
            if shard in done:
                continue
            connection.executemany(
                "INSERT OR IGNORE INTO envelopes VALUES (?,?)", self._shard_envelopes(shard)
            )
            connection.execute("INSERT OR IGNORE INTO backfilled VALUES (?)", (shard,))
            connection.commit()
        connection.execute("INSERT OR IGNORE INTO backfill_complete VALUES (1)")

    def captures(
        self, contract_digests: Collection[str], *, limit: int
    ) -> tuple[tuple[str, ...], bool]:
        """Up to ``limit`` envelope digests under these contracts, and whether that is all."""

        from cruxible_core.storage.cas import dry_run_held_bodies

        versions = tuple(dict.fromkeys(contract_digests))
        if not versions:
            return (), True
        if dry_run_held_bodies() is not None:
            return self._captures_without_writing(versions, limit=limit)
        marks = ",".join("?" for _ in versions)
        with self._connection() as connection:
            self._backfill(connection)
            rows = connection.execute(
                f"SELECT capture_digest FROM envelopes WHERE contract_digest IN ({marks}) "
                "ORDER BY capture_digest LIMIT ?",
                (*versions, limit + 1),
            ).fetchall()
        digests = tuple(str(row[0]) for row in rows)
        return digests[:limit], len(digests) <= limit

    def _captures_without_writing(
        self, versions: tuple[str, ...], *, limit: int
    ) -> tuple[tuple[str, ...], bool]:
        """A dry run's answer: the index as it stands, plus the shards it has not added yet."""

        found: set[str] = set()
        done: set[str] = set()
        complete = False
        if self.path.is_file() and not self.path.is_symlink():
            marks = ",".join("?" for _ in versions)
            with _LOCK:
                connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
                try:
                    found.update(
                        str(row[0])
                        for row in connection.execute(
                            f"SELECT capture_digest FROM envelopes "
                            f"WHERE contract_digest IN ({marks}) LIMIT ?",
                            (*versions, limit + 1),
                        )
                    )
                    done = {row[0] for row in connection.execute("SELECT shard FROM backfilled")}
                    complete = bool(
                        connection.execute("SELECT 1 FROM backfill_complete").fetchone()
                    )
                except sqlite3.DatabaseError:
                    found, done, complete = set(), set(), False
                finally:
                    connection.close()
        if not complete:
            wanted = set(versions)
            for shard in self._shards():
                if shard not in done:
                    found.update(
                        digest
                        for contract, digest in self._shard_envelopes(shard)
                        if contract in wanted
                    )
        digests = tuple(sorted(found))
        return digests[:limit], len(digests) <= limit


__all__ = [
    "ENVELOPE_HEAD",
    "ENVELOPE_HEAD_LENGTH",
    "CaptureEnvelopeIndex",
    "envelope_contract",
]
