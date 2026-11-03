"""A citation's proof lookup is a range of its own run, whatever other runs share.

Repeated runs of one Procedure name the same Source occurrence paths, and a
run may hold proof records the index could not project yet. Neither may make
one citation's lookup visit other runs, or the deferred records of its own.
The SQLite VM steps of the lookup's statements are counted at two sizes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from cruxible_core.exhaust.journal_index import (
    _SCHEMA,
    _SCHEMA_VERSION,
    _cited_rows,
    _pending_rows,
    _statements,
)

STREAM = "stream-a"
OCCURRENCE = "occurrence:sources/kev"
_ids = iter(range(1, 1_000_000))


def _index() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _statements(conn, _SCHEMA)
    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    return conn


def _record(conn: sqlite3.Connection, run_id: str, kind: str, *locators: str) -> int:
    record_id = next(_ids)
    conn.execute(
        "INSERT INTO records(id,stream,partition_id,sequence,offset,size,digest,previous,"
        "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
        "VALUES (?,?,?,?,0,1,'d','p',?,?,NULL,?,'2026-11-02T00:00:00Z')",
        (record_id, STREAM, f"part-{run_id}", record_id, kind, run_id, f"payload-{record_id}"),
    )
    conn.execute("DELETE FROM proof_pending WHERE record_id=?", (record_id,))
    for locator in locators:
        conn.execute(
            "INSERT INTO proof_locators VALUES (?,?,?,?)", (record_id, STREAM, run_id, locator)
        )
    return record_id


def _run(conn: sqlite3.Connection, run_id: str) -> None:
    """One run of the same Procedure: its admission and one Provider occurrence."""

    _record(conn, run_id, "admission_bound")
    _record(conn, run_id, "source_request_derived", OCCURRENCE)
    _record(conn, run_id, "source_read", OCCURRENCE)
    _record(conn, run_id, "provider_invocation_completed", f"receipt:{run_id}", OCCURRENCE)
    _record(conn, run_id, "produced_capture", f"capture:{run_id}")


def _steps(conn: sqlite3.Connection, statement: Callable[[], list[sqlite3.Row]]) -> int:
    counted = [0]

    def step() -> int:
        counted[0] += 1
        return 0

    conn.set_progress_handler(step, 1)
    try:
        statement()
    finally:
        conn.set_progress_handler(None, 1)
    return counted[0]


def test_occurrence_expansion_stays_within_the_cited_run() -> None:
    measured = []
    for runs in (50, 500):
        conn = _index()
        for index in range(runs):
            _run(conn, f"RUN-{index}")
        rows = _cited_rows(conn, STREAM, "RUN-7", ("receipt:RUN-7",))
        # The completion, the occurrence's derived request and Source read, and
        # the admission: four records of the cited run, none of another.
        assert [(row["run_id"], row["event_kind"]) for row in rows] == [
            ("RUN-7", "admission_bound"),
            ("RUN-7", "source_request_derived"),
            ("RUN-7", "source_read"),
            ("RUN-7", "provider_invocation_completed"),
        ]
        measured.append(
            _steps(conn, lambda: _cited_rows(conn, STREAM, "RUN-7", ("receipt:RUN-7",)))
        )
    # Ten times as many runs sharing the occurrence path: the same SQL work.
    assert measured[0] == measured[1], measured


def test_records_deferred_under_the_current_head_are_not_visited() -> None:
    measured = []
    for deferred in (10, 100):
        conn = _index()
        _run(conn, "RUN-1")
        for _ in range(deferred):
            record_id = _record(conn, "RUN-1", "produced_capture")
            conn.execute(
                "INSERT INTO proof_pending VALUES (?,?,?,?)", (record_id, STREAM, "RUN-1", "h:4")
            )
        fresh = _record(conn, "RUN-1", "produced_capture")
        conn.execute("INSERT INTO proof_pending VALUES (?,?,?,NULL)", (fresh, STREAM, "RUN-1"))
        assert [row["id"] for row in _pending_rows(conn, STREAM, "RUN-1", head="h:4")] == [fresh]
        measured.append(_steps(conn, lambda: _pending_rows(conn, STREAM, "RUN-1", head="h:4")))
        # Once the head moves, or on a retry, every deferred record is tried again.
        assert len(_pending_rows(conn, STREAM, "RUN-1", head="h:5")) == deferred + 1
        assert len(_pending_rows(conn, STREAM, "RUN-1", head=None)) == deferred + 1
    assert measured[0] == measured[1], measured
