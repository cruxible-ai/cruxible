"""Accepted generations by acceptance time: a small rebuildable index for the activity feed.

A generation's acceptance time is its accepted candidate's timestamp, which is
checked for spelling, not for order: generation 7 may say it was accepted
before generation 6. The feed lists the ledger newest first beside the
operational journals, so it needs the generations in an order that is both
time-like and a pure function of the history below each one. ``placed`` is
that order: a generation's acceptance time raised to the latest one at or
below it in sequence (a running maximum). It never decreases with sequence,
so newest-placed first is newest sequence first, and it depends only on the
generation and those accepted before it -- a generation accepted later never
moves one placed earlier.

The index lives beside the operational journals and caches what accepted
history already says: each accepted generation is read into it once (one
generation record's facts, memoized per process), as the first activity read
after its acceptance meets it, a chunk per transaction. A page then reads one
index range. A history whose generation at the index's top no longer matches
(recovered or replaced) rebuilds it from genesis.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.temporal import format_datetime
from cruxible_core.indexes.history.history_index import HistoryReader
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.generation_rows import generation_accepted_at

_LOCK = threading.RLock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_times (
 sequence INTEGER PRIMARY KEY, git_oid TEXT NOT NULL, at TEXT NOT NULL, placed TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS generation_times_placed ON generation_times(placed, sequence);
"""
#: Generations read into the index per transaction.
_CHUNK = 512


@dataclass(frozen=True)
class GenerationTime:
    """One generation's place in the feed: its true acceptance time and its ``placed`` key."""

    position: int
    accepted: str
    placed: str


def ledger_times_path(instance: PlaybillInstance) -> Path:
    return instance.root / instance.descriptor.storage.exhaust / "activity" / "ledger-times.sqlite3"


def _connect(instance: PlaybillInstance) -> sqlite3.Connection:
    path = ledger_times_path(instance)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("the ledger time index must be a regular file")
    conn = sqlite3.connect(path, timeout=30)
    conn.executescript(_SCHEMA)
    return conn


def _catch_up(instance: PlaybillInstance, history: HistoryReader, conn: sqlite3.Connection) -> None:
    """Read every generation up to the reader's head that the index lacks, once."""

    head = history.sequence
    records = instance.retained_record_reader()
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            top = conn.execute(
                "SELECT sequence,placed FROM generation_times ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            if top is not None:
                probe = min(int(top[0]), head)
                stored = conn.execute(
                    "SELECT git_oid FROM generation_times WHERE sequence=?", (probe,)
                ).fetchone()
                if stored is None or stored[0] != history.generation(probe).git_oid:
                    conn.execute("DELETE FROM generation_times")
                    top = None
            start = 0 if top is None else int(top[0]) + 1
            if start > head:
                conn.commit()
                return
            placed = None if top is None else str(top[1])
            rows = []
            for sequence in range(start, min(head, start + _CHUNK - 1) + 1):
                location = history.generation(sequence)
                at = str(
                    format_datetime(generation_accepted_at(instance, history, records, location))
                )
                placed = at if placed is None or at > placed else placed
                rows.append((sequence, location.git_oid, at, placed))
            conn.executemany("INSERT INTO generation_times VALUES(?,?,?,?)", rows)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def newest_generations(
    instance: PlaybillInstance,
    history: HistoryReader,
    *,
    limit: int,
    before: tuple[str, int] | None,
) -> tuple[GenerationTime, ...]:
    """Generations up to the reader's head, newest ``placed`` first, strictly ``before`` a key.

    One range of ``generation_times_placed`` starting below the reader's own
    head key (``placed`` never decreases with sequence, so every generation at
    or below the head sorts at or below it), after reading in the generations
    accepted since the last read: rows another reader indexed past this head
    are never visited.
    """

    with _LOCK:
        conn = _connect(instance)
        try:
            _catch_up(instance, history, conn)
            # The range starts at this reader's own head: another reader may
            # have indexed generations past it, and a page never walks them.
            head = conn.execute(
                "SELECT placed FROM generation_times WHERE sequence=?", (history.sequence,)
            ).fetchone()
            if head is None:
                return ()
            bound = (str(head[0]), history.sequence + 1)
            if before is not None and before < bound:
                bound = before
            rows = conn.execute(
                "SELECT sequence,at,placed FROM generation_times "
                "INDEXED BY generation_times_placed "
                "WHERE (placed,sequence)<(?,?) AND sequence<=? "
                "ORDER BY placed DESC,sequence DESC LIMIT ?",
                (*bound, history.sequence, limit),
            ).fetchall()
        finally:
            conn.close()
    return tuple(
        GenerationTime(position=int(row[0]), accepted=str(row[1]), placed=str(row[2]))
        for row in rows
    )


__all__ = ["GenerationTime", "ledger_times_path", "newest_generations"]
