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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, ParamSpec, TypeVar
from uuid import uuid4

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.content_memo import ValueMemo
from cruxible_client.contracts.errors import JournalIntegrityError
from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.exhaust.records import (
    JournalPartitionHeadV1,
    JournalStreamIdentityV1,
    StoredProcedureJournalRecordV1,
    journal_genesis_digest,
    journal_stream_key,
    journal_stream_strings,
)

if TYPE_CHECKING:
    from cruxible_core.exhaust.backends import LocalJournalBackend

_LOCK = threading.RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")
#: Bumped whenever a table or index changes shape: an index file written under
#: another version is dropped and rebuilt from the retained journal, which is
#: the authority (this file is a disposable cache).
_SCHEMA_VERSION = 5
_TABLES = (
    "capture_selectors",
    "capture_progress",
    "invocation_locators",
    "invocation_pending",
    "open_runs",
    "run_tallies",
    "records",
    "partitions",
    "index_identity",
    "initialized",
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS initialized (singleton INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS index_identity (singleton INTEGER PRIMARY KEY, identity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS partitions (
 stream TEXT NOT NULL, partition_id TEXT NOT NULL, end_offset INTEGER NOT NULL,
 sequence INTEGER NOT NULL, digest TEXT NOT NULL, signature TEXT NOT NULL,
 PRIMARY KEY(stream, partition_id));
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, stream TEXT NOT NULL, partition_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, offset INTEGER NOT NULL, size INTEGER NOT NULL,
 digest TEXT NOT NULL, previous TEXT NOT NULL, event_kind TEXT NOT NULL,
 run_id TEXT, occurrence_id TEXT, payload_digest TEXT NOT NULL, recorded_at TEXT NOT NULL,
 UNIQUE(stream, partition_id, sequence));
CREATE INDEX IF NOT EXISTS records_payload ON records(stream,event_kind,payload_digest);
CREATE INDEX IF NOT EXISTS records_run ON records(stream, run_id, sequence);
CREATE INDEX IF NOT EXISTS records_run_kind ON records(stream, run_id, event_kind, sequence);
CREATE INDEX IF NOT EXISTS records_event
 ON records(stream, event_kind, recorded_at, partition_id, sequence);
CREATE INDEX IF NOT EXISTS records_occurrence
 ON records(stream, partition_id, occurrence_id, event_kind, sequence);
CREATE TABLE IF NOT EXISTS capture_selectors (
 record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
 contract_digest TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS capture_contract ON capture_selectors(contract_digest, record_id);
CREATE INDEX IF NOT EXISTS records_kind_position ON records(stream,event_kind,id);
CREATE INDEX IF NOT EXISTS records_stream_position ON records(stream,id);
CREATE INDEX IF NOT EXISTS records_partition_position
 ON records(stream,partition_id,event_kind,id);
CREATE TABLE IF NOT EXISTS capture_progress(stream TEXT PRIMARY KEY, record_id INTEGER NOT NULL);
-- The Provider invocation each provider_invocation_started record names. The
-- writer that appends a start records its locator (note_invocation); a start
-- indexed without one (a rebuilt index, a crash between append and note) waits
-- in invocation_pending until a lookup projects it from its payload.
CREATE TABLE IF NOT EXISTS invocation_locators (
 record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
 invocation_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS invocation_by_id ON invocation_locators(invocation_id, record_id);
CREATE TABLE IF NOT EXISTS invocation_pending (
 record_id INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
 stream TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS invocation_pending_stream ON invocation_pending(stream, record_id);
CREATE TRIGGER IF NOT EXISTS records_invocation_started AFTER INSERT ON records
 WHEN new.event_kind='provider_invocation_started' BEGIN
 INSERT INTO invocation_pending VALUES(new.id, new.stream);
END;
-- Admissions whose run has no attempt_finalized record yet, keyed by the
-- admission row: running runs are read without visiting finished ones.
CREATE TABLE IF NOT EXISTS open_runs (
 row_id INTEGER PRIMARY KEY, stream TEXT NOT NULL, partition_id TEXT NOT NULL,
 run_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS open_runs_run ON open_runs(stream, run_id);
CREATE INDEX IF NOT EXISTS open_runs_stream ON open_runs(stream, row_id);
CREATE INDEX IF NOT EXISTS open_runs_partition ON open_runs(stream, partition_id, row_id);
-- Admission counts per partition and, under the partition id '' (never a valid
-- partition id), per stream: a count never visits the admissions it counts.
CREATE TABLE IF NOT EXISTS run_tallies (
 stream TEXT NOT NULL, partition_id TEXT NOT NULL, admitted INTEGER NOT NULL,
 PRIMARY KEY(stream, partition_id));
-- Triggers keep both current on every insert and delete of an indexed record,
-- including a partition rebuilt after replacement.
CREATE TRIGGER IF NOT EXISTS records_admitted AFTER INSERT ON records
 WHEN new.event_kind='admission_bound' AND new.run_id IS NOT NULL BEGIN
 INSERT INTO run_tallies VALUES(new.stream,new.partition_id,1)
  ON CONFLICT(stream,partition_id) DO UPDATE SET admitted=admitted+1;
 INSERT INTO run_tallies VALUES(new.stream,'',1)
  ON CONFLICT(stream,partition_id) DO UPDATE SET admitted=admitted+1;
 INSERT INTO open_runs SELECT new.id,new.stream,new.partition_id,new.run_id
  WHERE NOT EXISTS(SELECT 1 FROM records f WHERE f.stream=new.stream
  AND f.run_id=new.run_id AND f.event_kind='attempt_finalized');
END;
CREATE TRIGGER IF NOT EXISTS records_finalized AFTER INSERT ON records
 WHEN new.event_kind='attempt_finalized' AND new.run_id IS NOT NULL BEGIN
 DELETE FROM open_runs WHERE stream=new.stream AND run_id=new.run_id;
END;
CREATE TRIGGER IF NOT EXISTS records_unadmitted AFTER DELETE ON records
 WHEN old.event_kind='admission_bound' AND old.run_id IS NOT NULL BEGIN
 UPDATE run_tallies SET admitted=admitted-1
  WHERE stream=old.stream AND partition_id IN (old.partition_id,'');
 DELETE FROM open_runs WHERE row_id=old.id;
END;
CREATE TRIGGER IF NOT EXISTS records_unfinalized AFTER DELETE ON records
 WHEN old.event_kind='attempt_finalized' AND old.run_id IS NOT NULL BEGIN
 INSERT OR IGNORE INTO open_runs SELECT a.id,a.stream,a.partition_id,a.run_id
  FROM records a WHERE a.stream=old.stream AND a.run_id=old.run_id
  AND a.event_kind='admission_bound' AND NOT EXISTS(SELECT 1 FROM records f
  WHERE f.stream=old.stream AND f.run_id=old.run_id AND f.event_kind='attempt_finalized');
END;
"""


def journal_locked(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def locked(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _LOCK:
            return function(*args, **kwargs)

    return locked


def _rebuild_schema(conn: sqlite3.Connection) -> None:
    """Create the index, dropping one written under another schema version.

    The retained journal refills it. One immediate transaction re-checks the
    version, so concurrent openers drop it once. The new identity invalidates
    every run page key and live position minted against the old rows, exactly
    as deleting the file would.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            for table in _TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            statement = ""
            for line in _SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    conn.execute(statement)
                    statement = ""
            conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _key(stream: JournalStreamIdentityV1) -> str:
    return journal_stream_key(stream)


#: A partition's dirty marker (file name, bytes), by the identity's strings.
_DIRTY_MARKERS: ValueMemo[tuple[str, bytes]] = ValueMemo(capacity=4096)


def _dirty_marker(stream: JournalStreamIdentityV1, partition_id: str) -> tuple[str, bytes]:
    def derive() -> tuple[str, bytes]:
        value = canonical_bytes(
            {"stream": stream.model_dump(mode="json"), "partition_id": partition_id}
        )
        return hashlib.sha256(value).hexdigest(), value

    return _DIRTY_MARKERS.derive((*journal_stream_strings(stream), partition_id), derive)


#: Where a run page stopped: (the index's identity, the admission row's position).
RunLocatorKey = tuple[str, int]


class RunPageInvalidated(Exception):
    """A run page key minted before the index was rebuilt: continue from page one."""


@dataclass(frozen=True)
class RunLocator:
    """One admitted run as the index locates it, without reading any payload."""

    run_id: str
    admitted_at: str
    partition_id: str
    admission_payload_digest: str
    #: The ``attempt_finalized`` payload; ``None`` while the run is running.
    final_payload_digest: str | None
    nodes_done: int
    index_identity: str
    #: The admission record's position in the index: immutable once written.
    row_id: int

    @property
    def finished(self) -> bool:
        return self.final_payload_digest is not None

    @property
    def key(self) -> RunLocatorKey:
        return (self.index_identity, self.row_id)


class JournalIndex:
    def __init__(self, backend: LocalJournalBackend) -> None:
        self.backend = backend
        self.path = backend.root / "event-index.sqlite3"
        self.dirty = backend.root / "index-dirty"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with _LOCK:
            if self.path.is_symlink():
                raise JournalIntegrityError("journal index must be a regular file")
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                if conn.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    _rebuild_schema(conn)
                conn.execute("INSERT OR IGNORE INTO index_identity VALUES(1,?)", (uuid4().hex,))
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
            name, value = _dirty_marker(stream, partition_id)
            _atomic_write(self.dirty / name, value)
            _fsync_directory(self.backend.root)

    def _sync(
        self,
        conn: sqlite3.Connection,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        recover_tail: bool = False,
    ) -> None:
        """Index complete frames; only a locked writer may discard a crash tail."""

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
        if (
            prior is not None
            and prior["signature"] == signature
            and (not recover_tail or prior["end_offset"] == stat.st_size)
        ):
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
            recover_tail=recover_tail,
            tolerate_tail=not recover_tail,
            offset=offset,
            sequence=sequence,
            previous=previous,
        ):
            record = stored.record
            conn.execute(
                "INSERT INTO records(stream,partition_id,sequence,offset,size,digest,previous,"
                "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    record.payload_digest,
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

    def sync(
        self,
        stream: JournalStreamIdentityV1,
        partition_id: str,
        *,
        recover_tail: bool = False,
    ) -> None:
        with self.connection() as conn:
            self._sync(conn, stream, partition_id, recover_tail=recover_tail)

    def catch_up(self, stream: JournalStreamIdentityV1, partition_ids: Iterable[str]) -> None:
        """Index every record appended to these partitions since their last sync.

        Each partition's sync compares the log's signature first and, on a strict
        append, verifies and indexes only the frames past its last indexed
        offset: an unchanged partition costs one stat, a grown one its new
        records, never the whole journal.
        """

        with self.connection() as conn:
            for partition_id in partition_ids:
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
                raise JournalIntegrityError("indexed journal record differs from retained bytes")
            return stored
        except StopIteration as exc:
            raise JournalIntegrityError("indexed journal record is missing") from exc
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
        payload_digest: str | None = None,
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
                ("payload_digest", payload_digest),
            ):
                if value is not None:
                    where.append(f"{name}=?")
                    args.append(value)
            for op, bound in ((">=", first_sequence), ("<=", last_sequence)):
                if bound is not None:
                    where.append(f"sequence{op}?")
                    args.append(bound)
            order = "DESC" if descending else "ASC"
            # An exact occurrence or run is one short index range; without the
            # hint the planner may prefer an index that only serves the order.
            hint = (
                " INDEXED BY records_occurrence"
                if occurrence_id is not None and partition_id is not None
                else " INDEXED BY records_run_kind"
                if run_id is not None and event_kind is not None
                else ""
            )
            sql = (
                f"SELECT * FROM records{hint} WHERE "
                + " AND ".join(where)
                + f" ORDER BY sequence {order}, partition_id {order}"
            )
            if limit is not None:
                sql += " LIMIT ?"
                args.append(limit)
            return tuple(self.read_row(row) for row in conn.execute(sql, args).fetchall())

    def run_locators(
        self,
        stream: JournalStreamIdentityV1,
        *,
        limit: int,
        partition_id: str | None = None,
        run_id: str | None = None,
        running_only: bool = False,
        after: RunLocatorKey | None = None,
    ) -> tuple[tuple[RunLocator, ...], bool]:
        """Admitted runs, newest admission first: a locator read.

        Index rows only: no payload is read. The order is the admission
        record's position in this index, which never changes once written, so
        a page continues by key and a run that finishes between pages is never
        repeated or skipped. A run's status is a displayed field, never part of
        the order; ``running_only`` filters to runs whose ``attempt_finalized``
        record has not landed. A key minted before the index was rebuilt names
        positions that no longer exist, so it refuses rather than guess.
        """

        where = ["a.stream=?", "a.event_kind='admission_bound'", "a.run_id IS NOT NULL"]
        args: list[Any] = [_key(stream)]
        if partition_id is not None:
            where.append("a.partition_id=?")
            args.append(partition_id)
        if run_id is not None:
            where.append("a.run_id=?")
            args.append(run_id)
        # Each shape names the index whose range holds exactly its candidates.
        source = "records a INDEXED BY " + (
            "records_run_kind"
            if run_id is not None
            else "records_partition_position"
            if partition_id is not None
            else "records_kind_position"
        )
        if running_only:
            # Only open admissions are visited: finished history is never scanned.
            source = (
                "open_runs o INDEXED BY "
                + ("open_runs_partition" if partition_id is not None else "open_runs_stream")
                + " JOIN records a ON a.id=o.row_id"
            )
            where.append("o.stream=?")
            args.append(_key(stream))
            if partition_id is not None:
                where.append("o.partition_id=?")
                args.append(partition_id)
        final = (
            "(SELECT f.payload_digest FROM records f INDEXED BY records_run_kind "
            "WHERE f.stream=a.stream AND f.run_id=a.run_id AND f.event_kind='attempt_finalized' "
            "ORDER BY f.sequence DESC LIMIT 1)"
        )
        with self.connection() as conn:
            identity = conn.execute(
                "SELECT identity FROM index_identity WHERE singleton=1"
            ).fetchone()[0]
            if after is not None:
                if after[0] != identity:
                    raise RunPageInvalidated(
                        "the run index was rebuilt since this page's first read"
                    )
            order = "o.row_id" if running_only else "a.id"
            if after is not None:
                where.append(f"{order}<?")
                args.append(after[1])
            rows = conn.execute(
                f"SELECT a.id AS row_id, a.run_id AS run_id, a.recorded_at AS admitted_at, "
                f"a.partition_id AS partition_id, a.payload_digest AS payload_digest, "
                f"{final} AS final_digest, "
                "(SELECT count(*) FROM records n INDEXED BY records_run_kind "
                "WHERE n.stream=a.stream AND n.run_id=a.run_id AND n.event_kind='node_fired') "
                f"AS nodes FROM {source} WHERE "
                + " AND ".join(where)
                + f" ORDER BY {order} DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
        return (
            tuple(
                RunLocator(
                    run_id=row["run_id"],
                    admitted_at=row["admitted_at"],
                    partition_id=row["partition_id"],
                    admission_payload_digest=row["payload_digest"],
                    final_payload_digest=row["final_digest"],
                    nodes_done=int(row["nodes"]),
                    index_identity=str(identity),
                    row_id=int(row["row_id"]),
                )
                for row in rows[:limit]
            ),
            len(rows) > limit,
        )

    def running(
        self, stream: JournalStreamIdentityV1, *, page_size: int = 256
    ) -> Iterator[RunLocator]:
        """Every run still running, newest admission first, paged from the open-run set.

        A run that finishes while the caller works through a page (recovery
        finalizing it) never moves the keyset, so nothing repeats or is skipped.
        """

        after: RunLocatorKey | None = None
        while True:
            page, more = self.run_locators(stream, limit=page_size, running_only=True, after=after)
            yield from page
            if not more or not page:
                return
            after = page[-1].key

    def run_ids_with_prefix(
        self, stream: JournalStreamIdentityV1, prefix: str, *, limit: int
    ) -> tuple[str, ...]:
        """Admitted run ids starting with ``prefix``, in id order: a locator read."""

        with self.connection() as conn:
            return tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM records WHERE stream=? "
                    "AND event_kind='admission_bound' AND run_id>=? AND run_id<? "
                    "ORDER BY run_id LIMIT ?",
                    (_key(stream), prefix, prefix + "\uffff", limit),
                )
            )

    def run_counts(
        self, stream: JournalStreamIdentityV1, *, partition_id: str | None = None
    ) -> tuple[int, int]:
        """How many runs were admitted, and how many of them are still running.

        Read from the admission tallies and the open-run set the index keeps as
        it indexes records, so the cost follows the runs still running, never
        the runs ever admitted.
        """

        scope = partition_id if partition_id is not None else ""
        with self.connection() as conn:
            admitted = conn.execute(
                "SELECT admitted FROM run_tallies WHERE stream=? AND partition_id=?",
                (_key(stream), scope),
            ).fetchone()
            running = (
                conn.execute(
                    "SELECT count(*) FROM open_runs WHERE stream=? AND partition_id=?",
                    (_key(stream), partition_id),
                )
                if partition_id is not None
                else conn.execute("SELECT count(*) FROM open_runs WHERE stream=?", (_key(stream),))
            ).fetchone()
        row = (0 if admitted is None else admitted[0], running[0])
        return int(row[0]), int(row[1])

    def payload_digests(
        self,
        stream: JournalStreamIdentityV1,
        *,
        event_kind: str,
        after: int,
        through: int,
    ) -> tuple[str, ...]:
        """The payload digests of one kind's records in one live-reader range, by position."""

        with self.connection() as conn:
            return tuple(
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT payload_digest FROM records "
                    "INDEXED BY records_kind_position "
                    "WHERE stream=? AND event_kind=? AND id>? AND id<=?",
                    (_key(stream), event_kind, after, through),
                )
            )

    def kind_after(
        self,
        stream: JournalStreamIdentityV1,
        *,
        event_kind: str,
        after: int,
        limit: int,
    ) -> tuple[str, tuple[tuple[int, StoredProcedureJournalRecordV1], ...]]:
        """One kind's records after a cache position, in append order: a keyed page.

        Returns the index identity with the page. A position is only meaningful
        under the identity it was read with: after a rebuild the caller starts
        again from zero (its own work must be idempotent per record).
        """

        with self.connection() as conn:
            identity = str(
                conn.execute("SELECT identity FROM index_identity WHERE singleton=1").fetchone()[0]
            )
            rows = conn.execute(
                "SELECT * FROM records INDEXED BY records_kind_position "
                "WHERE stream=? AND event_kind=? AND id>? ORDER BY id LIMIT ?",
                (_key(stream), event_kind, after, limit),
            ).fetchall()
            return identity, tuple((int(row["id"]), self.read_row(row)) for row in rows)

    def positions(
        self, stream: JournalStreamIdentityV1, *, event_kind: str | None = None
    ) -> dict[str, Any]:
        """A disposable live-reader position, never a work-completion authority.

        A new cache identity invalidates live positions after a rebuild. Pending
        work still rebuilds from retained transitions; listeners open a new
        forward range instead of guessing which old cache ordinal was observed.
        With an event kind, the position is that kind's latest record, so a
        reader of one kind does not move for every other record.
        """
        with self.connection() as conn:
            identity = conn.execute(
                "SELECT identity FROM index_identity WHERE singleton=1"
            ).fetchone()[0]
            ordinal = (
                conn.execute(
                    "SELECT coalesce(max(id),0) FROM records WHERE stream=?", (_key(stream),)
                )
                if event_kind is None
                else conn.execute(
                    "SELECT coalesce(max(id),0) FROM records WHERE stream=? AND event_kind=?",
                    (_key(stream), event_kind),
                )
            ).fetchone()[0]
            return {"generation": identity, "ordinal": ordinal}

    def appended_partitions(
        self,
        stream: JournalStreamIdentityV1,
        *,
        after: dict[str, Any],
        through: dict[str, Any],
        limit: int,
    ) -> tuple[tuple[str, ...], dict[str, Any] | None]:
        """The partitions that gained a record in one live-reader range, in append order.

        A locator read only: the caller re-reads each partition through its
        verified log. Returns where the page stopped, or None once the range is
        exhausted.
        """
        with self.connection() as conn:
            generation = conn.execute(
                "SELECT identity FROM index_identity WHERE singleton=1"
            ).fetchone()[0]
            if after["generation"] != generation or through["generation"] != generation:
                raise JournalIntegrityError(
                    "live event reader position was invalidated by an index rebuild"
                )
            rows = conn.execute(
                "SELECT id,partition_id FROM records WHERE stream=? AND id>? AND id<=? "
                "ORDER BY id LIMIT ?",
                (_key(stream), after["ordinal"], through["ordinal"], limit + 1),
            ).fetchall()
        partitions = tuple(dict.fromkeys(row["partition_id"] for row in rows[:limit]))
        if len(rows) > limit:
            return partitions, {"generation": generation, "ordinal": rows[limit - 1]["id"]}
        return partitions, None

    def note_invocation(
        self,
        stream: JournalStreamIdentityV1,
        *,
        partition_id: str,
        sequence: int,
        invocation_id: str,
    ) -> None:
        """Record the invocation a just-appended start names: the writer's own locator.

        The writer holds the payload it appended, so no body is read. A start
        never noted stays pending, and a lookup projects it from its payload.
        """

        with self.connection() as conn:
            self._sync(conn, stream, partition_id)
            row = conn.execute(
                "SELECT id,event_kind FROM records WHERE stream=? AND partition_id=? "
                "AND sequence=?",
                (_key(stream), partition_id, sequence),
            ).fetchone()
            if row is None or row["event_kind"] != "provider_invocation_started":
                raise JournalIntegrityError("noted invocation start is not indexed")
            conn.execute(
                "INSERT OR REPLACE INTO invocation_locators VALUES (?,?)",
                (row["id"], invocation_id),
            )
            conn.execute("DELETE FROM invocation_pending WHERE record_id=?", (row["id"],))

    def invocation_runs(
        self,
        stream: JournalStreamIdentityV1,
        *,
        bodies: Any,
        invocation_ids: tuple[str, ...],
    ) -> dict[str, tuple[str, str]]:
        """invocation id -> (run id, partition) of the run that started it, for the named ids.

        Writers note each start as they append it, so a lookup is one index
        range per id. Only starts indexed without a note (an index rebuilt from
        the journal, or a crash between an append and its note) are projected
        here, once each, from their payloads. An id no retained start names is
        absent from the answer.
        """
        from cruxible_core.exhaust.records import parse_journal_payload
        from cruxible_core.storage.cas import BodyAccessContext

        access = BodyAccessContext(principal_id="provider-recovery", can_read_body=True)
        with self.connection() as conn:
            while True:
                pending = conn.execute(
                    # Driven by the pending rows of this stream: when none are
                    # pending, nothing else is visited.
                    "SELECT r.* FROM invocation_pending p INDEXED BY invocation_pending_stream "
                    "JOIN records r ON r.id=p.record_id "
                    "WHERE p.stream=? ORDER BY p.record_id LIMIT 1024",
                    (_key(stream),),
                ).fetchall()
                if not pending:
                    break
                for row in pending:
                    stored = self.read_row(row)
                    payload = parse_journal_payload(
                        bodies.read(stored.record.payload_digest, access=access)
                    )
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("invocation_id"), str
                    ):
                        raise JournalIntegrityError(
                            "Provider invocation start payload names no invocation"
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO invocation_locators VALUES (?,?)",
                        (row["id"], payload["invocation_id"]),
                    )
                    conn.execute("DELETE FROM invocation_pending WHERE record_id=?", (row["id"],))
            found: dict[str, tuple[str, str]] = {}
            for invocation_id in invocation_ids:
                row = conn.execute(
                    "SELECT r.run_id,r.partition_id FROM invocation_locators l "
                    "JOIN records r ON r.id=l.record_id "
                    "WHERE l.invocation_id=? AND r.stream=? ORDER BY l.record_id LIMIT 1",
                    (invocation_id, _key(stream)),
                ).fetchone()
                if row is not None and row[0] is not None:
                    found[invocation_id] = (str(row[0]), str(row[1]))
            return found

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
        after: dict[str, Any] | None = None,
        through: dict[str, Any] | None = None,
    ) -> tuple[tuple[StoredProcedureJournalRecordV1, ...], tuple[str, str, int] | None, bool]:
        """Project new capture selectors once, then verify only selected bodies.

        Metadata catch-up is bounded independently of the result page. A caller
        must not report absence while selector coverage is incomplete.
        """
        from cruxible_core.exhaust.records import parse_journal_payload
        from cruxible_core.storage.cas import BodyAccessContext

        access = BodyAccessContext(principal_id="trigger-discovery", can_read_body=True)
        with self.connection() as conn:
            progress = conn.execute(
                "SELECT record_id FROM capture_progress WHERE stream=?", (_key(stream),)
            ).fetchone()
            fresh = conn.execute(
                "SELECT * FROM records WHERE stream=? AND event_kind='produced_capture' "
                "AND id>? ORDER BY id LIMIT 1025",
                (_key(stream), progress[0] if progress else 0),
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
                    raise JournalIntegrityError("produced Capture payload lacks its typed contract")
                conn.execute(
                    "INSERT INTO capture_selectors VALUES (?,?)",
                    (row["id"], payload["capture_contract_digest"]),
                )
            if fresh:
                conn.execute(
                    "INSERT OR REPLACE INTO capture_progress VALUES(?,?)",
                    (_key(stream), fresh[min(len(fresh), 1024) - 1]["id"]),
                )
            if len(fresh) > 1024:
                return (), None, False
            args: list[Any]
            where, args = (
                ["r.stream=?", "c.contract_digest=?"],
                [_key(stream), contract_digest],
            )
            if through is not None:
                generation = conn.execute(
                    "SELECT identity FROM index_identity WHERE singleton=1"
                ).fetchone()[0]
                if (
                    through["generation"] != generation
                    or after is None
                    or after["generation"] != generation
                ):
                    raise JournalIntegrityError(
                        "live event reader position was invalidated by an index rebuild"
                    )
                where.extend(("r.id>?", "r.id<=?"))
                args.extend((after["ordinal"], through["ordinal"]))
            if through is None:
                where.append("r.recorded_at<?")
                args.append(format_datetime(until))
            if since is not None and through is None:
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
