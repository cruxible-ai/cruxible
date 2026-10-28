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
from pathlib import Path
from typing import Any

import pytest

from cruxible_core.exhaust.journal_index import (
    _SCHEMA,
    _TABLES,
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


def _selectors_to_version_8(conn: sqlite3.Connection) -> None:
    """Strip capture_selectors back to its version 8 shape: no landing columns or objects."""

    for (trigger,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='capture_selectors'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP INDEX capture_source_latest")
    for column in ("source_lost", "landed_at", "stream"):
        conn.execute(f"ALTER TABLE capture_selectors DROP COLUMN {column}")
    conn.execute("PRAGMA user_version=8")


def _version_8_rows(conn: sqlite3.Connection) -> None:
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


def _assert_migrated(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
    assert (
        _tallies(conn)
        == _recount(conn)
        == [
            (STREAM, "", "", 1),
            (STREAM, "kev", "c1", 2),
            (STREAM, "pypi", "c1", 1),
        ]
    )
    assert [tuple(row) for row in conn.execute("SELECT * FROM capture_unattributed")] == [
        (4, STREAM)
    ]
    page = source_landing_page(conn, STREAM, limit=5, after=None)
    assert [(item.source, item.latest.record_id) for item in page.sources] == [
        ("kev", 2),
        ("pypi", 5),
    ]
    # Every landing object a fresh index has, the migrated one has too.
    fresh = _index()
    expected = {
        row[0]
        for row in fresh.execute(
            "SELECT type || ':' || name FROM sqlite_master WHERE name LIKE 'capture_%'"
        )
    }
    assert expected == {
        row[0]
        for row in conn.execute(
            "SELECT type || ':' || name FROM sqlite_master WHERE name LIKE 'capture_%'"
        )
    }


def test_schema_8_gains_the_tallies_from_its_own_rows_and_keeps_its_identity() -> None:
    conn = _index()
    conn.execute("INSERT INTO index_identity VALUES (1, 'kept')")
    conn.execute("DROP TABLE capture_source_tallies")
    conn.execute("DROP TABLE capture_unattributed")
    _selectors_to_version_8(conn)
    _version_8_rows(conn)

    _rebuild_schema(conn)

    assert conn.execute("SELECT identity FROM index_identity").fetchone()[0] == "kept"
    _assert_migrated(conn)


def test_a_9_to_8_to_9_round_trip_rederives_the_stale_landing_objects() -> None:
    """An older binary rebuilds a version 9 file as 8, dropping only the tables it knows.

    The landing tables survive it, tallying rows that are gone; migrating to 9
    again recreates them from the rows, instead of colliding with them.
    """

    conn = _index()
    _land(conn, 1, "kev", "c1", at="2026-10-01T00:00:00Z")
    _land(conn, 7, "kev", "c1", at="2026-10-07T00:00:00Z")
    _record(conn, 8, at="2026-10-08T00:00:00Z")
    conn.execute("INSERT INTO capture_unattributed VALUES (8, ?)", (STREAM,))
    # The version 8 binary's rebuild: its own tables dropped and recreated.
    for table in _TABLES:
        if table not in {"capture_source_tallies", "capture_unattributed"}:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    _statements(conn, _SCHEMA)
    _selectors_to_version_8(conn)
    assert _tallies(conn), "the stale tallies survive the older rebuild"
    _version_8_rows(conn)

    _rebuild_schema(conn)

    _assert_migrated(conn)


def _assert_guarded(conn: sqlite3.Connection) -> None:
    """Every write that could move a row away from its tally is refused."""

    _land(conn, 1, "kev", "c1", at="2026-10-01T00:00:00Z")
    _land(conn, 3, "pypi", "c1", at="2026-10-03T00:00:00Z")
    _record(conn, 2, at="2026-10-02T00:00:00Z")
    conn.execute("INSERT INTO capture_unattributed VALUES (2, ?)", (STREAM,))
    for update in (
        "UPDATE capture_selectors SET logical_source='pypi' WHERE record_id=1",
        "UPDATE capture_selectors SET contract_digest='c2' WHERE record_id=1",
        "UPDATE capture_selectors SET stream='stream-b' WHERE record_id=1",
        "UPDATE capture_selectors SET source_lost=1 WHERE record_id=1",
        # Moving a row onto another's key would delete that one with no trigger.
        "UPDATE OR REPLACE capture_selectors SET record_id=3 WHERE record_id=1",
        "UPDATE capture_selectors SET record_id=9 WHERE record_id=1",
        # The INTEGER PRIMARY KEY's aliases, which an UPDATE OF column list never sees.
        "UPDATE OR REPLACE capture_selectors SET rowid=3 WHERE record_id=1",
        "UPDATE OR REPLACE capture_selectors SET _rowid_=3 WHERE record_id=1",
        "UPDATE OR REPLACE capture_selectors SET oid=3 WHERE record_id=1",
        "UPDATE OR REPLACE capture_unattributed SET rowid=1",
        "UPDATE OR REPLACE capture_unattributed SET _rowid_=1",
        "UPDATE OR REPLACE capture_unattributed SET oid=1",
        "UPDATE capture_unattributed SET stream='stream-b'",
        "UPDATE OR REPLACE capture_unattributed SET record_id=1",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="keeps its"):
            conn.execute(update)
    for replace in (
        "INSERT OR REPLACE INTO capture_selectors(record_id,contract_digest,logical_source,"
        "stream,landed_at,source_lost) VALUES (1,'c1','kev','stream-a','2026-10-01T00:00:00Z',0)",
        "INSERT OR REPLACE INTO capture_selectors(record_id,contract_digest,logical_source,"
        "stream,landed_at,source_lost) VALUES (1,'c1','pypi','stream-a','2026-10-01T00:00:00Z',0)",
        "INSERT OR REPLACE INTO capture_unattributed VALUES (2, 'stream-a')",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            conn.execute(replace)
    # What the projection does write: the Trigger-name column, and nothing tallied moves.
    conn.execute("UPDATE capture_selectors SET contract_identity='CaptureContract:x'")
    assert (
        _tallies(conn)
        == _recount(conn)
        == [
            (STREAM, "", "", 1),
            (STREAM, "kev", "c1", 1),
            (STREAM, "pypi", "c1", 1),
        ]
    )


def test_a_projected_landing_is_never_rewritten_or_replaced() -> None:
    _assert_guarded(_index())


def _guard_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN "
            "('capture_landing_once','capture_landing_fixed','capture_landing_frozen',"
            "'capture_landing_sealed',"
            "'capture_unattributed_once','capture_unattributed_fixed')"
        )
    }


@pytest.mark.parametrize("written_by", ["d9af311c3", "72a3d6880", "576f84444"])
def test_opening_an_index_written_before_its_guards_installs_them(
    tmp_path: Path, written_by: str
) -> None:
    """A version 9 file from before the guards, or with an older guard, gains the current ones."""

    from types import SimpleNamespace

    from cruxible_core.exhaust.journal_index import JournalIndex
    from cruxible_core.indexes.resident import close_idle_connections

    path = tmp_path / "event-index.sqlite3"
    conn = sqlite3.connect(path, isolation_level=None)
    _statements(conn, _SCHEMA)
    for name in _guard_names(conn):
        conn.execute(f"DROP TRIGGER {name}")
    if written_by == "72a3d6880":
        # That commit's selector guard did not freeze record_id.
        conn.execute(
            "CREATE TRIGGER capture_landing_fixed BEFORE UPDATE OF logical_source ON "
            "capture_selectors BEGIN SELECT RAISE(ABORT, 'keeps its'); END"
        )
    if written_by == "576f84444":
        # That commit's froze record_id through a column list the rowid aliases bypass.
        conn.execute(
            "CREATE TRIGGER capture_landing_frozen BEFORE UPDATE OF record_id ON "
            "capture_selectors BEGIN SELECT RAISE(ABORT, 'keeps its'); END"
        )
    conn.execute("INSERT INTO initialized VALUES (1)")
    conn.execute("PRAGMA user_version=9")
    conn.close()

    backend = SimpleNamespace(root=tmp_path, _streams_root=tmp_path / "streams")
    reopened = JournalIndex(backend)._open()  # type: ignore[arg-type]
    try:
        assert _guard_names(reopened) == {
            "capture_landing_once",
            "capture_landing_sealed",
            "capture_unattributed_once",
            "capture_unattributed_fixed",
        }
        version = reopened.execute("PRAGMA schema_version").fetchone()[0]
    finally:
        reopened.close()
    # Opening again finds them present and changes nothing.
    again = JournalIndex(backend)._open()  # type: ignore[arg-type]
    try:
        assert again.execute("PRAGMA schema_version").fetchone()[0] == version
    finally:
        again.close()
    close_idle_connections()
    guarded = sqlite3.connect(path, isolation_level=None)
    guarded.execute("PRAGMA foreign_keys=ON")
    _assert_guarded(guarded)
    guarded.close()
