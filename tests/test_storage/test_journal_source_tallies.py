"""Landings by logical source in the Procedure-run journal index.

Each projected ``produced_capture`` record keeps one ``capture_selectors`` row
(its stream, landing instant and whether its envelope was readable); a landing
whose payload is lost keeps a ``capture_unattributed`` row instead. Triggers
keep ``capture_source_tallies`` -- landings per (stream, source, contract), and
under ``('', '')`` the landings no source can be read for -- on every insert
and delete, cascades from a rebuilt partition included, so the tallies always
equal a recount. Index schema 9 derives them once from an 8 index's own rows.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from cruxible_core.exhaust.journal_index import (
    _SCHEMA,
    _rebuild_schema,
    _statements,
    source_landing_page,
)

STREAM = "stream-a"


def _index() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    _statements(conn, _SCHEMA)
    conn.execute("PRAGMA user_version=9")
    return conn


def _record(conn: sqlite3.Connection, record_id: int, *, stream: str = STREAM, at: str) -> None:
    conn.execute(
        "INSERT INTO records(id,stream,partition_id,sequence,offset,size,digest,previous,"
        "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
        "VALUES (?,?,?,?,0,1,'d','p','produced_capture',?,NULL,?,?)",
        (record_id, stream, f"run:{record_id}", 1, f"RUN-{record_id}", f"payload-{record_id}", at),
    )


def _land(
    conn: sqlite3.Connection,
    record_id: int,
    source: str | None,
    contract: str,
    *,
    at: str,
    stream: str = STREAM,
    lost: bool = False,
) -> None:
    _record(conn, record_id, stream=stream, at=at)
    conn.execute(
        "INSERT INTO capture_selectors(record_id,contract_digest,logical_source,stream,"
        "landed_at,source_lost) VALUES (?,?,?,?,?,?)",
        (record_id, contract, source, stream, at, int(lost)),
    )


def _tallies(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row) for row in conn.execute("SELECT * FROM capture_source_tallies ORDER BY 1, 2, 3")
    ]


def _recount(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT stream, logical_source, contract_digest, count(*) FROM capture_selectors "
            "WHERE logical_source IS NOT NULL GROUP BY 1, 2, 3 "
            "UNION ALL SELECT stream, '', '', count(*) FROM ("
            " SELECT stream FROM capture_selectors WHERE logical_source IS NULL AND source_lost=1"
            " UNION ALL SELECT stream FROM capture_unattributed) GROUP BY 1 "
            "ORDER BY 1, 2, 3"
        )
    ]


def test_tallies_follow_landings_and_partition_rebuilds() -> None:
    conn = _index()
    _land(conn, 1, "cisa.kev.catalog", "c1", at="2026-10-01T00:00:00Z")
    _land(conn, 2, "cisa.kev.catalog", "c1", at="2026-10-02T00:00:00Z")
    _land(conn, 3, "cisa.kev.catalog", "c2", at="2026-10-03T00:00:00Z")
    _land(conn, 4, None, "c1", at="2026-10-04T00:00:00Z")  # emitted: no source, not lost
    _land(conn, 5, None, "c1", at="2026-10-05T00:00:00Z", lost=True)  # envelope lost
    _record(conn, 6, at="2026-10-06T00:00:00Z")
    conn.execute("INSERT INTO capture_unattributed VALUES (6, ?)", (STREAM,))  # payload lost
    assert _tallies(conn) == [
        (STREAM, "", "", 2),
        (STREAM, "cisa.kev.catalog", "c1", 2),
        (STREAM, "cisa.kev.catalog", "c2", 1),
    ]
    # A partition rebuilt after replacement deletes its records; their rows cascade away.
    conn.execute("DELETE FROM records WHERE id IN (1, 3, 6)")
    assert _tallies(conn) == [(STREAM, "", "", 1), (STREAM, "cisa.kev.catalog", "c1", 1)]


def test_tallies_equal_a_recount_after_random_landings_and_rebuilds() -> None:
    rng = random.Random(20261028)
    conn = _index()
    held: set[int] = set()
    for step in range(1, 500):
        if held and rng.random() < 0.3:
            victim = rng.choice(sorted(held))
            conn.execute("DELETE FROM records WHERE id=?", (victim,))
            held.discard(victim)
            continue
        stream = rng.choice((STREAM, "stream-b"))
        at = f"2026-10-01T00:{step // 60:02d}:{step % 60:02d}Z"
        roll = rng.random()
        if roll < 0.1:
            _record(conn, step, stream=stream, at=at)
            conn.execute("INSERT INTO capture_unattributed VALUES (?, ?)", (step, stream))
        else:
            source = None if roll < 0.25 else rng.choice(("kev", "pypi", "osv"))
            _land(
                conn,
                step,
                source,
                rng.choice(("c1", "c2")),
                at=at,
                stream=stream,
                lost=source is None and roll < 0.18,
            )
        held.add(step)
    assert _tallies(conn) == _recount(conn)


def test_a_page_walks_the_tallies_and_seeks_each_latest_landing() -> None:
    conn = _index()
    _land(conn, 1, "cisa.kev.catalog", "c1", at="2026-10-02T00:00:00Z")
    _land(conn, 2, "cisa.kev.catalog", "c2", at="2026-10-01T00:00:00Z")  # older, indexed later
    _land(conn, 3, "pypi.release", "c1", at="2026-10-03T00:00:00Z")
    _land(conn, 4, "pypi.release", "c1", at="2026-10-03T00:00:00Z")  # same instant: later record
    _land(conn, 5, "osv.records", "c1", at="2026-10-04T00:00:00Z", stream="stream-b")
    _land(conn, 6, None, "c1", at="2026-10-05T00:00:00Z", lost=True)

    first = source_landing_page(conn, STREAM, limit=1, after=None)
    assert [(item.source, item.counts) for item in first.sources] == [
        ("cisa.kev.catalog", (("c1", 1), ("c2", 1)))
    ]
    assert first.sources[0].latest.record_id == 1 and first.stop == "cisa.kev.catalog"
    assert first.unattributed == 1
    rest = source_landing_page(conn, STREAM, limit=5, after=first.stop)
    assert [(item.source, item.latest.record_id) for item in rest.sources] == [("pypi.release", 4)]
    assert rest.stop is None

    def plan(sql: str, args: tuple[object, ...]) -> list[str]:
        return [str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args)]

    assert plan(
        "SELECT logical_source, contract_digest, landings FROM capture_source_tallies "
        "WHERE stream=? AND logical_source>? ORDER BY logical_source, contract_digest",
        (STREAM, ""),
    ) == ["SEARCH capture_source_tallies USING PRIMARY KEY (stream=? AND logical_source>?)"]
    (seek, *_join) = plan(
        "SELECT r.id FROM capture_selectors c INDEXED BY capture_source_latest "
        "JOIN records r ON r.id=c.record_id WHERE c.stream=? AND c.logical_source=? "
        "ORDER BY c.landed_at DESC, c.record_id DESC LIMIT 1",
        (STREAM, "pypi.release"),
    )
    assert seek.startswith("SEARCH c USING ") and "capture_source_latest" in seek, seek
    assert "TEMP B-TREE" not in " ".join(_join)


def test_schema_8_gains_the_tallies_from_its_own_rows_and_keeps_its_identity() -> None:
    conn = _index()
    conn.execute("INSERT INTO index_identity VALUES (1, 'kept')")
    # Back to the version 8 shape: no landing objects, no landing columns.
    for statement in (
        "DROP TRIGGER capture_landing_attributed",
        "DROP TRIGGER capture_landing_unattributed",
        "DROP TRIGGER capture_landing_lost",
        "DROP TRIGGER capture_landing_found",
        "DROP INDEX capture_source_latest",
        "DROP TABLE capture_source_tallies",
        "DROP TABLE capture_unattributed",
        "ALTER TABLE capture_selectors DROP COLUMN source_lost",
        "ALTER TABLE capture_selectors DROP COLUMN landed_at",
        "ALTER TABLE capture_selectors DROP COLUMN stream",
        "PRAGMA user_version=8",
    ):
        conn.execute(statement)
    for record_id, source in ((1, "kev"), (2, "kev"), (3, None), (5, "pypi")):
        _record(conn, record_id, at=f"2026-10-0{record_id}T00:00:00Z")
        conn.execute(
            "INSERT INTO capture_selectors(record_id,contract_digest,logical_source) "
            "VALUES (?,?,?)",
            (record_id, "c1", source),
        )
    _record(conn, 4, at="2026-10-04T00:00:00Z")  # skipped by the projection: payload lost
    _record(conn, 6, at="2026-10-06T00:00:00Z")  # not projected yet
    conn.execute("INSERT INTO capture_progress VALUES (?, 5)", (STREAM,))

    _rebuild_schema(conn)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert conn.execute("SELECT identity FROM index_identity").fetchone()[0] == "kept"
    assert _tallies(conn) == [
        (STREAM, "", "", 1),
        (STREAM, "kev", "c1", 2),
        (STREAM, "pypi", "c1", 1),
    ]
    assert [tuple(row) for row in conn.execute("SELECT * FROM capture_unattributed")] == [
        (4, STREAM)
    ]
    page = source_landing_page(conn, STREAM, limit=5, after=None)
    assert [(item.source, item.latest.record_id) for item in page.sources] == [
        ("kev", 2),
        ("pypi", 5),
    ]
    # Every object a fresh index has, the migrated one has too.
    names = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'capture_%'")
    }
    assert {
        "capture_source_tallies",
        "capture_unattributed",
        "capture_source_latest",
        "capture_landing_attributed",
        "capture_landing_unattributed",
        "capture_landing_lost",
        "capture_landing_found",
    } <= names
