"""The scale guard's counters count what they claim: focused checks on scratch databases.

No world is built here. Each check opens a window over a scratch root and runs
one shape of work the guard must see (or must refuse to guess about).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_scale import _counters
from tests.test_scale._counters import Window, counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

ROWS = 50


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _claims(root: Path, *, rows: int = ROWS, name: str = "state.sqlite3") -> sqlite3.Connection:
    connection = sqlite3.connect(root / name, isolation_level=None)
    connection.executescript(
        """
        CREATE TABLE claims(id INTEGER PRIMARY KEY, claim_id TEXT, value TEXT, other INT);
        CREATE INDEX claim_id ON claims(claim_id);
        """
    )
    connection.executemany(
        "INSERT INTO claims(claim_id, value, other) VALUES (?,?,?)",
        [(f"CLM-{index:04d}", f"value {index}", index % 7) for index in range(rows)],
    )
    return connection


def _scan(root: Path, connection: sqlite3.Connection, *sql: str) -> Window:
    with counting(root) as window:
        for statement in sql:
            connection.execute(statement).fetchall()
    return window


def _rows(window: Window) -> int:
    assert window.unexplained == [], window.unexplained
    return window.counts["sql_scan_rows"]


def test_a_scan_weighs_its_table_however_the_table_is_named(root: Path) -> None:
    connection = _claims(root)
    assert _rows(_scan(root, connection, "SELECT * FROM claims WHERE value LIKE '%9'")) == ROWS
    # SQLite plans an aliased table as `SCAN c`.
    assert _rows(_scan(root, connection, "SELECT * FROM claims AS c WHERE c.other = 3")) == ROWS
    assert _rows(_scan(root, connection, "SELECT * FROM claims c WHERE c.other = 3")) == ROWS
    assert _rows(_scan(root, connection, "SELECT * FROM main.claims")) == ROWS
    assert _rows(_scan(root, connection, 'SELECT * FROM "claims" AS "c"')) == ROWS


def test_a_self_join_weighs_each_walk(root: Path) -> None:
    connection = _claims(root)
    # One side is scanned, the other gets an automatic index: both walk the table.
    window = _scan(
        root, connection, "SELECT count(*) FROM claims a, claims b WHERE a.other = b.other"
    )
    assert _rows(window) == 2 * ROWS


def test_attached_and_temporary_tables_are_weighed(root: Path) -> None:
    connection = _claims(root)
    connection.execute("ATTACH DATABASE ? AS aux", (str(root / "aux.sqlite3"),))
    connection.execute("CREATE TABLE aux.items(k)")
    connection.executemany("INSERT INTO aux.items VALUES (?)", [(index,) for index in range(30)])
    connection.execute("CREATE TEMP TABLE pending(k)")
    connection.executemany("INSERT INTO pending VALUES (?)", [(index,) for index in range(20)])
    assert _rows(_scan(root, connection, "SELECT * FROM aux.items AS i WHERE i.k > 3")) == 30
    assert _rows(_scan(root, connection, "SELECT * FROM pending p WHERE p.k > 3")) == 20
    # A temporary table created inside the window, after the schema was first read.
    with counting(root) as window:
        connection.execute("SELECT * FROM claims WHERE other = 1").fetchall()
        connection.execute("CREATE TEMP TABLE delta AS SELECT id FROM claims WHERE other = 2")
        connection.execute("SELECT * FROM temp.delta").fetchall()
    delta = connection.execute("SELECT count(*) FROM temp.delta").fetchone()[0]
    assert _rows(window) == ROWS + ROWS + delta
    # The schema table resolves (it is no unknown target) and weighs nothing.
    window = _scan(root, connection, "SELECT name FROM main.sqlite_master WHERE type = 'x';")
    assert (window.counts["sql_statements"], _rows(window)) == (1, 0)


def test_ctes_views_and_subqueries_weigh_only_their_base_scans(root: Path) -> None:
    connection = _claims(root)
    connection.execute("CREATE VIEW sevens AS SELECT * FROM claims WHERE other = 0")
    assert _rows(_scan(root, connection, "SELECT * FROM sevens AS s")) == ROWS
    window = _scan(
        root,
        connection,
        "WITH w AS MATERIALIZED (SELECT id FROM claims WHERE other = 1) SELECT * FROM w AS ww",
    )
    assert _rows(window) == ROWS
    window = _scan(root, connection, "SELECT * FROM (SELECT id FROM claims WHERE other = 1) sub")
    assert _rows(window) == ROWS


def test_a_limited_walk_is_bounded_only_when_nothing_filters_or_sorts_it(root: Path) -> None:
    connection = _claims(root)
    assert _rows(_scan(root, connection, "SELECT id FROM claims ORDER BY id LIMIT 3")) == 3
    # An offset is walked too.
    assert _rows(_scan(root, connection, "SELECT id FROM claims ORDER BY id LIMIT 3 OFFSET 5")) == 8
    # A residual predicate no row matches walks every row of the ordered index.
    filtered = (
        "SELECT claim_id FROM claims INDEXED BY claim_id WHERE value = 'absent' "
        "ORDER BY claim_id LIMIT 1"
    )
    assert _rows(_scan(root, connection, filtered)) == ROWS
    # A sort reads everything before the limit applies.
    assert _rows(_scan(root, connection, "SELECT id FROM claims ORDER BY value LIMIT 1")) == ROWS


def test_a_planned_foreign_key_check_weighs_the_child_table(root: Path) -> None:
    connection = _claims(root)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE notes(claim INTEGER REFERENCES claims(id), text TEXT)")
    connection.executemany("INSERT INTO notes VALUES (?, 'n')", [(1,)] * 30)
    # SQLite would walk notes for each deleted claim; none is deleted, so this over-counts.
    assert _rows(_scan(root, connection, "DELETE FROM claims WHERE id = 999")) == 30


def test_whole_table_statements_weigh_their_table(root: Path) -> None:
    connection = _claims(root)
    assert _rows(_scan(root, connection, "UPDATE claims SET value = 'y' WHERE other = 9")) == ROWS
    assert _rows(_scan(root, connection, "CREATE INDEX claims_by_other ON claims(other)")) == ROWS
    assert _rows(_scan(root, connection, "DELETE FROM claims")) == ROWS


def test_a_trigger_body_scan_is_weighed(root: Path) -> None:
    connection = _claims(root)
    connection.executescript(
        """
        CREATE TABLE tally(n INT);
        CREATE TRIGGER count_claims AFTER INSERT ON claims
        BEGIN UPDATE tally SET n = n + 1 WHERE n = NEW.other; END;
        """
    )
    connection.executemany("INSERT INTO tally VALUES (?)", [(index,) for index in range(40)])
    window = _scan(root, connection, "INSERT INTO claims(claim_id, other) VALUES ('CLM-x', 3)")
    # The insert, then its trigger program and the program's one statement, each
    # traced (by Python) as a repeat of the insert; each weighs the body's scan.
    assert window.counts["sql_statements"] == 3
    assert _rows(window) == 2 * 40
    # The same insert on a table with no trigger is weighed once, as itself.
    connection.execute("DROP TRIGGER count_claims")
    window = _scan(root, connection, "INSERT INTO claims(claim_id, other) VALUES ('CLM-y', 3)")
    assert (window.counts["sql_statements"], _rows(window)) == (1, 0)


def test_a_database_renamed_before_the_window_closes_is_still_weighed(root: Path) -> None:
    connection = _claims(root, name="build.sqlite3")
    with counting(root) as window:
        connection.execute("SELECT * FROM claims AS c WHERE c.other = 2").fetchall()
        connection.close()
        os.replace(root / "build.sqlite3", root / "published.sqlite3")
    assert _rows(window) == ROWS


def test_work_the_window_cannot_resolve_is_reported_never_zero(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _claims(root)
    window = _scan(root, connection, "ANALYZE")
    assert window.unexplained and "ANALYZE" in window.unexplained[0]
    # A plan step naming nothing the statement or schema defines.
    monkeypatch.setattr(_counters, "_objects", lambda connection, version: {"main": {}})
    window = _scan(root, connection, "SELECT * FROM claims WHERE value = 'x'")
    assert window.unexplained and "names no table" in window.unexplained[0]


def test_the_growth_rule_rejects_a_scan_that_grows_with_the_table(root: Path) -> None:
    small = _claims(root, rows=20, name="small.sqlite3")
    large = _claims(root, rows=200, name="large.sqlite3")
    statement = "SELECT c.id FROM claims AS c WHERE c.value = 'absent' ORDER BY c.claim_id LIMIT 1"
    counted = [_rows(_scan(root, connection, statement)) for connection in (small, large)]
    assert counted == [20, 200]
    assert counted[1] > counted[0] * RATIO + SLACK["sql_scan_rows"]


def test_function_counters_follow_names_bound_by_import_and_are_removed(root: Path) -> None:
    from cruxible_client.contracts import claims
    from cruxible_client.contracts.claims import ClaimFormatError
    from cruxible_core.service.discovery import query

    original = claims.parse_claim
    assert query.parse_claim is original
    with counting(root) as window:
        with pytest.raises(ClaimFormatError):
            query.parse_claim(b"not json", path="claims/00/CLM-x.json")
    assert window.counts["claim_parses"] == 1
    assert query.parse_claim is original and claims.parse_claim is original


def test_opens_under_the_root_and_spawns_are_counted(root: Path, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    inside = root / "inside.txt"
    outside = tmp_path_factory.mktemp("outside") / "outside.txt"
    for path in (inside, outside):
        path.write_text("x")
    with counting(root) as window:
        inside.read_text()
        outside.read_text()
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert window.counts["file_opens"] == 1
    assert (window.counts["git_spawns"], window.counts["other_spawns"]) == (1, 1)
