"""Disposable record locations and metadata, backed by the retained journal.

Dirty partitions are named before append. Catch-up verifies their suffixes after
an interrupted write; deleting SQLite rebuilds locations without executing work.
No trigger policy or consumer completion is part of this storage index.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, ParamSpec, TypeVar

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillJournalIntegrityError
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.exhaust.records import (
    JournalPartitionHeadV1,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
    journal_genesis_digest,
)

if TYPE_CHECKING:
    from cruxible_core.exhaust.backends import LocalJournalBackend

_LOCK = threading.RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS initialized (singleton INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS partitions (
 stream TEXT NOT NULL, partition_id TEXT NOT NULL, end_offset INTEGER NOT NULL,
 sequence INTEGER NOT NULL, digest TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(stream, partition_id));
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY, stream TEXT NOT NULL, partition_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL,
 digest TEXT NOT NULL, previous TEXT NOT NULL, event_kind TEXT NOT NULL,
 run_id TEXT, occurrence_id TEXT, recorded_at TEXT NOT NULL,
 UNIQUE(stream, partition_id, sequence));
CREATE INDEX IF NOT EXISTS records_run ON records(stream, run_id, sequence);
CREATE INDEX IF NOT EXISTS records_event
 ON records(stream, event_kind, recorded_at, partition_id, sequence);
CREATE INDEX IF NOT EXISTS records_occurrence
 ON records(stream, partition_id, occurrence_id, event_kind);
CREATE TABLE IF NOT EXISTS capture_selectors (
 record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
 contract_digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS capture_contract ON capture_selectors(contract_digest, record_id);
"""


def journal_locked(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _LOCK:
            return function(*args, **kwargs)

    return locked


def _key(stream: JournalStreamIdentityV1) -> str:
    return canonical_bytes(stream.model_dump(mode="json")).decode()


class JournalIndex:
    def __init__(self, backend: LocalJournalBackend) -> None:
        self.backend = backend
        self.path = backend.root / "event-index.sqlite3"
        self.dirty = backend.root / "index-dirty"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            if self.path.is_symlink():
                raise PlaybillJournalIntegrityError("journal index must be a regular file")
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                if not conn.execute("SELECT 1 FROM initialized").fetchone():
                    for identity in self.backend._streams_root.glob("*/*/identity.json"):
                        raw = json.loads(identity.read_bytes())
                        self._sync(
                            conn,
                            JournalStreamIdentityV1.model_validate(raw["stream"]),
                            raw["partition_id"],
                        )
                    conn.execute("INSERT INTO initialized VALUES (1)")
                if self.dirty.exists():
                    for marker in self.dirty.iterdir():
                        raw = json.loads(marker.read_bytes())
                        self._sync(
                            conn,
                            JournalStreamIdentityV1.model_validate(raw["stream"]),
                            raw["partition_id"],
                        )
                        conn.commit()
                        marker.unlink()
                yield conn
                conn.commit()
            finally:
                conn.close()

    def mark_dirty(self, stream: JournalStreamIdentityV1, partition_id: str) -> None:
        from cruxible_core.exhaust.backends import _atomic_write, _fsync_directory

        with _LOCK:
            self.dirty.mkdir(mode=0o700, exist_ok=True)
            value = canonical_bytes(
                {"stream": stream.model_dump(mode="json"), "partition_id": partition_id}
            )
            _atomic_write(self.dirty / hashlib.sha256(value).hexdigest(), value)
            _fsync_directory(self.backend.root)

    def _sync(
        self, conn: sqlite3.Connection, stream: JournalStreamIdentityV1, partition_id: str
    ) -> None:
        directory = self.backend._partition_directory(stream, partition_id, create=False)
        log = directory / "records.log"
        if not log.exists():
            return
        stat = log.stat()
        signature = (
            f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
        )
        prior = conn.execute(
            "SELECT * FROM partitions WHERE stream=? AND partition_id=?",
            (_key(stream), partition_id),
        ).fetchone()
        if prior is not None and prior["signature"] == signature:
            return
        offset, sequence, previous = 0, 0, journal_genesis_digest(stream, partition_id)
        # Only a strict append can reuse the verified prefix. Replacement or an
        # in-place edit rebuilds the partition through the same frame verifier.
        if (
            prior is not None
            and stat.st_size > prior["end_offset"]
            and prior["signature"].split(":")[:2] == signature.split(":")[:2]
        ):
            offset, sequence, previous = prior["end_offset"], prior["sequence"], prior["digest"]
        else:
            conn.execute(
                "DELETE FROM records WHERE stream=? AND partition_id=?",
                (_key(stream), partition_id),
            )
        for start, size, stored in self.backend._read_frames(
            directory,
            stream=stream,
            partition_id=partition_id,
            recover_tail=True,
            offset=offset,
            sequence=sequence,
            previous=previous,
        ):
            record = stored.record
            conn.execute(
                "INSERT INTO records(stream,partition_id,sequence,offset,size,digest,previous,"
                "event_kind,run_id,occurrence_id,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _key(stream),
                    partition_id,
                    record.sequence,
                    start,
                    size,
                    stored.record_digest,
                    record.previous_record_digest,
                    record.event_kind,
                    record.run_id,
                    record.occurrence_id,
                    format_datetime(record.recorded_at),
                ),
            )
            offset, sequence, previous = start + size, record.sequence, stored.record_digest
        stat = log.stat()
        signature = (
            f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}"
        )
        conn.execute(
            "INSERT OR REPLACE INTO partitions VALUES(?,?,?,?,?,?)",
            (_key(stream), partition_id, offset, sequence, previous, signature),
        )

    def sync(self, stream: JournalStreamIdentityV1, partition_id: str) -> None:
        with self.connection() as conn:
            self._sync(conn, stream, partition_id)

    def head(self, stream: JournalStreamIdentityV1, partition_id: str) -> JournalPartitionHeadV1:
        with self.connection() as conn:
            self._sync(conn, stream, partition_id)
            row = conn.execute(
                "SELECT sequence,digest FROM partitions WHERE stream=? AND partition_id=?",
                (_key(stream), partition_id),
            ).fetchone()
            return JournalPartitionHeadV1(
                stream=stream,
                partition_id=partition_id,
                sequence=row[0] if row else 0,
                record_digest=row[1] if row else journal_genesis_digest(stream, partition_id),
            )

    def read_row(self, row: sqlite3.Row) -> StoredProcedureJournalRecordV1:
        stream = JournalStreamIdentityV1.model_validate_json(row["stream"])
        directory = self.backend._partition_directory(stream, row["partition_id"], create=False)
        frames = self.backend._read_frames(
            directory,
            stream=stream,
            partition_id=row["partition_id"],
            recover_tail=False,
            offset=row["offset"],
            sequence=row["sequence"] - 1,
            previous=row["previous"],
        )
        try:
            _, size, stored = next(frames)
            if size != row["size"] or stored.record_digest != row["digest"]:
                raise PlaybillJournalIntegrityError(
                    "indexed journal record differs from retained bytes"
                )
            return stored
        except StopIteration as exc:
            raise PlaybillJournalIntegrityError("indexed journal record is missing") from exc
        finally:
            frames.close()

    def select(
        self,
        stream: JournalStreamIdentityV1,
        *,
        partition_id: str | None = None,
        run_id: str | None = None,
        event_kind: str | None = None,
        occurrence_id: str | None = None,
        first_sequence: int | None = None,
        last_sequence: int | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> tuple[StoredProcedureJournalRecordV1, ...]:
        with self.connection() as conn:
            if partition_id is not None:
                self._sync(conn, stream, partition_id)
            where = ["stream=?"]
            args: list[Any] = [_key(stream)]
            for name, value in (
                ("partition_id", partition_id),
                ("run_id", run_id),
                ("event_kind", event_kind),
                ("occurrence_id", occurrence_id),
            ):
                if value is not None:
                    where.append(f"{name}=?")
                    args.append(value)
            for op, bound in ((">=", first_sequence), ("<=", last_sequence)):
                if bound is not None:
                    where.append(f"sequence{op}?")
                    args.append(bound)
            order = "DESC" if descending else "ASC"
            sql = (
                "SELECT * FROM records WHERE "
                + " AND ".join(where)
                + f" ORDER BY sequence {order}, partition_id {order}"
            )
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            return tuple(self.read_row(row) for row in conn.execute(sql, args).fetchall())

    def captures(
        self,
        stream: JournalStreamIdentityV1,
        *,
        bodies: Any,
        contract_digest: str,
        since: datetime | None,
        until: datetime,
        limit: int,
        cursor: tuple[str, str, int] | None = None,
    ) -> tuple[tuple[StoredProcedureJournalRecordV1, ...], tuple[str, str, int] | None, bool]:
        """Project new capture selectors once, then verify only selected bodies.

        Metadata catch-up is bounded independently of the result page. A caller
        must not report absence while selector coverage is incomplete.
        """
        from cruxible_core.exhaust.records import parse_journal_payload
        from cruxible_core.storage.cas import BodyAccessContext

        access = BodyAccessContext(principal_id="trigger-discovery", can_read_body=True)
        with self.connection() as conn:
            fresh = conn.execute(
                "SELECT r.* FROM records r LEFT JOIN capture_selectors c ON c.record_id=r.id "
                "WHERE r.stream=? AND r.event_kind='produced_capture' AND c.record_id IS NULL "
                "ORDER BY r.id LIMIT 1025",
                (_key(stream),),
            ).fetchall()
            for row in fresh[:1024]:
                stored = self.read_row(row)
                payload = parse_journal_payload(
                    bodies.read(stored.record.payload_digest, access=access)
                )
                if (
                    not isinstance(payload, dict)
                    or payload.get("tag") != "playbill-procedure-produced-capture-v1"
                    or not isinstance(payload.get("capture_contract_digest"), str)
                ):
                    raise PlaybillJournalIntegrityError(
                        "produced Capture payload lacks its typed contract"
                    )
                conn.execute(
                    "INSERT INTO capture_selectors VALUES (?,?)",
                    (row["id"], payload["capture_contract_digest"]),
                )
            if len(fresh) > 1024:
                return (), None, False
            args: list[Any]
            where, args = (
                ["r.stream=?", "c.contract_digest=?", "r.recorded_at<?"],
                [_key(stream), contract_digest, format_datetime(until)],
            )
            if since is not None:
                where.append("r.recorded_at>=?")
                args.append(format_datetime(since))
            if cursor is not None:
                where.append("(r.recorded_at,r.partition_id,r.sequence)<(?,?,?)")
                args.extend(cursor)
            rows = conn.execute(
                "SELECT r.* FROM capture_selectors c JOIN records r ON r.id=c.record_id WHERE "
                + " AND ".join(where)
                + " ORDER BY r.recorded_at DESC,r.partition_id DESC,r.sequence DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
            selected = tuple(self.read_row(row) for row in rows[:limit])
            next_cursor = None
            if len(rows) > limit:
                row = rows[limit - 1]
                next_cursor = (row["recorded_at"], row["partition_id"], row["sequence"])
            return selected, next_cursor, len(fresh) <= 1024
