"""The scale guard's counters count what they claim: focused checks on scratch databases.

No world is built here. Each check opens a window over a scratch root and runs
one shape of work the guard must see (or must refuse to guess about). The SQL
checks carry every reproduction from the counter's reviews (F-001, F-003 to
F-008): the window must weigh at least the table the work walks, or fail.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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


# -- reads: however the table is named (F-001, F-008) ---------------------------------


def test_a_scan_weighs_its_table_however_the_table_is_named(root: Path) -> None:
    connection = _claims(root)
    for statement in (
        "SELECT * FROM claims WHERE value LIKE '%9'",
        # SQLite plans an aliased table as `SCAN c`; the authorizer names claims.
        "SELECT * FROM claims AS c WHERE c.other = 3",
        "SELECT * FROM claims c WHERE c.other = 3",
        "SELECT * FROM main.claims",
        'SELECT * FROM "claims" AS "c"',
        "SELECT * FROM CLAIMS WHERE other = 3",
        "SELECT * FROM 'claims' WHERE other = 3",
        # No column is read: only the program's cursor names the table.
        "SELECT count(*) FROM claims c",
        "SELECT count(*) FROM claims",
    ):
        assert _rows(_scan(root, connection, statement)) == ROWS, statement


def test_a_seek_on_a_whole_unique_key_weighs_nothing(root: Path) -> None:
    connection = _claims(root)
    connection.execute("CREATE UNIQUE INDEX claims_unique ON claims(value)")
    for statement in (
        "SELECT * FROM claims WHERE id = 3",
        "SELECT * FROM main.claims WHERE id = 3",
        "SELECT * FROM claims AS c WHERE c.id = 3",
        "SELECT EXISTS(SELECT 1 FROM claims WHERE id = 3)",
        "SELECT * FROM CLAIMS WHERE value = 'value 3'",
        "SELECT * FROM claims WHERE id IN (3, 4, 5)",
    ):
        assert _rows(_scan(root, connection, statement)) == 0, statement


def test_a_seek_then_step_over_a_range_or_a_prefix_weighs_its_table(root: Path) -> None:
    # The ruling's range rule: a seek followed by Next/Prev walks a range that can
    # grow with the table, unless the seek is equality on a whole unique key.
    connection = _claims(root)
    connection.execute("CREATE INDEX by_other ON claims(other)")
    for statement in (
        # A range on an index.
        "SELECT * FROM claims WHERE other > 2",
        "SELECT * FROM claims WHERE id > 3",
        # Equality on a non-unique index: a prefix every equal entry shares.
        "SELECT * FROM claims WHERE claim_id = 'CLM-0003'",
        "SELECT * FROM claims WHERE other = 3",
    ):
        assert _rows(_scan(root, connection, statement)) == ROWS, statement


def test_a_self_join_weighs_its_table(root: Path) -> None:
    connection = _claims(root)
    # One side is scanned, the other gets an automatic index: claims is walked.
    statement = "SELECT count(*) FROM claims a, claims b WHERE a.other = b.other"
    assert _rows(_scan(root, connection, statement)) == ROWS


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
    # The schema table weighs nothing.
    window = _scan(root, connection, "SELECT name FROM main.sqlite_master WHERE type = 'x';")
    assert (window.counts["sql_statements"], _rows(window)) == (1, 0)


def test_views_ctes_and_subqueries_weigh_their_base_tables(root: Path) -> None:
    connection = _claims(root)
    connection.execute("CREATE VIEW sevens AS SELECT * FROM claims WHERE other = 0")
    for statement in (
        "SELECT * FROM sevens AS s",
        "WITH w AS MATERIALIZED (SELECT id FROM claims WHERE other = 1) SELECT * FROM w AS ww",
        "SELECT * FROM (SELECT id FROM claims WHERE other = 1) sub",
    ):
        assert _rows(_scan(root, connection, statement)) == ROWS, statement


def test_a_database_renamed_before_the_window_closes_is_still_weighed(root: Path) -> None:
    connection = _claims(root, name="build.sqlite3")
    with counting(root) as window:
        connection.execute("SELECT * FROM claims AS c WHERE c.other = 2").fetchall()
        connection.close()
        os.replace(root / "build.sqlite3", root / "published.sqlite3")
    assert _rows(window) == ROWS


# -- bounded walks (F-001, F-005, F-006) -----------------------------------------------


def test_a_limited_walk_is_bounded_only_for_a_plain_row_preserving_select(root: Path) -> None:
    connection = _claims(root)
    assert _rows(_scan(root, connection, "SELECT id, value FROM claims LIMIT 3")) == 3
    # An offset is walked too.
    assert _rows(_scan(root, connection, "SELECT id FROM claims LIMIT 3 OFFSET 5")) == 8
    assert _rows(_scan(root, connection, "SELECT * FROM claims AS c LIMIT 3")) == 3
    # Everything else reads the table in full, however few rows it returns.
    for statement in (
        # A residual predicate no row matches walks every row of the ordered index.
        "SELECT claim_id FROM claims INDEXED BY claim_id WHERE value = 'absent' "
        "ORDER BY claim_id LIMIT 1",
        "SELECT id FROM claims ORDER BY id LIMIT 3",
        "SELECT id FROM claims ORDER BY value LIMIT 1",
        "SELECT count(*) FROM claims LIMIT 1",
        "SELECT coalesce(value, '') FROM claims LIMIT 1",
        "SELECT id FROM claims UNION ALL SELECT id FROM claims LIMIT 1",
    ):
        assert _rows(_scan(root, connection, statement)) == ROWS, statement


def test_a_distinct_walk_is_read_in_full_whatever_its_limit(root: Path) -> None:
    # F-005: every value equal, an index on it; DISTINCT reads every entry to
    # return one row, in one plan step.
    connection = sqlite3.connect(root / "distinct.sqlite3", isolation_level=None)
    connection.executescript("CREATE TABLE claims(id INTEGER PRIMARY KEY, value TEXT);")
    connection.executemany("INSERT INTO claims(value) VALUES ('same')", [()] * ROWS)
    connection.execute("CREATE INDEX values_idx ON claims(value)")
    statement = "SELECT DISTINCT value FROM claims ORDER BY value LIMIT 2"
    plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement)]
    assert plan == ["SCAN claims USING COVERING INDEX values_idx"]
    assert _rows(_scan(root, connection, statement)) == ROWS


def test_a_literal_that_changes_the_plan_is_planned_afresh(root: Path) -> None:
    # F-006: a partial unique index serves other=1 and not other=2, on one schema.
    connection = _claims(root)
    connection.execute("CREATE UNIQUE INDEX partial_match ON claims(value) WHERE other = 1")
    served = "SELECT * FROM claims WHERE value = 'value 1' AND other = 1"
    scanned = "SELECT * FROM claims WHERE value = 'value 2' AND other = 2"
    assert _rows(_scan(root, connection, served, scanned)) == ROWS
    assert _rows(_scan(root, connection, served)) == 0
    assert _rows(_scan(root, connection, scanned)) == ROWS


def test_a_table_rebuilt_under_its_name_is_weighed_at_its_new_size(root: Path) -> None:
    # F-004: read the empty table, drop it, rebuild it with every Claim by
    # CREATE TABLE AS (which moves neither total_changes nor data_version), read it.
    connection = _claims(root)
    connection.execute("ATTACH DATABASE ? AS aux", (str(root / "aux.sqlite3"),))
    for schema in ("main", "aux"):
        connection.execute(f"CREATE TABLE {schema}.rebuilt(id INTEGER)")
        read = f"SELECT * FROM {schema}.rebuilt"
        assert _rows(_scan(root, connection, read)) == 0
        connection.execute(f"DROP TABLE {schema}.rebuilt")
        connection.execute(f"CREATE TABLE {schema}.rebuilt AS SELECT id FROM main.claims")
        assert _rows(_scan(root, connection, read)) == ROWS, schema


# -- writes and the triggers they fire (F-003, F-007, F-008) ---------------------------


def test_whole_table_statements_weigh_their_table_and_point_writes_nothing(root: Path) -> None:
    connection = _claims(root)
    assert _rows(_scan(root, connection, "UPDATE claims SET value = 'y' WHERE other = 9")) == ROWS
    # Inserting and updating by key touch only their rows.
    for statement in (
        "INSERT INTO claims(claim_id) VALUES ('CLM-new')",
        "UPDATE claims SET value = 'z' WHERE id = 4",
        "DELETE FROM claims WHERE id = 5",
    ):
        assert _rows(_scan(root, connection, statement)) == 0, statement
    assert _rows(_scan(root, connection, "CREATE INDEX claims_by_other ON claims(other)")) == ROWS
    # Emptied whole.
    assert _rows(_scan(root, connection, "DELETE FROM claims")) == ROWS


def test_a_planned_foreign_key_check_weighs_the_child_table(root: Path) -> None:
    connection = _claims(root)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE notes(claim INTEGER REFERENCES claims(id), text TEXT)")
    connection.executemany("INSERT INTO notes VALUES (?, 'n')", [(1,)] * 30)
    # The claims lookup is proved by its plan; the check on notes walks it. SQLite
    # would walk notes for each deleted claim; none is deleted: an over-count.
    assert _rows(_scan(root, connection, "DELETE FROM claims WHERE id = 999")) == 30


def test_a_trigger_body_is_weighed_on_top_of_its_statement(root: Path) -> None:
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
    # Three traces (the insert, its trigger program, the program's statement),
    # each weighing tally, which the trigger program walks; the insert itself
    # touches only its row.
    assert window.counts["sql_statements"] == 3
    assert _rows(window) == 3 * 40
    connection.execute("DROP TRIGGER count_claims")
    window = _scan(root, connection, "INSERT INTO claims(claim_id, other) VALUES ('CLM-y', 3)")
    assert (window.counts["sql_statements"], _rows(window)) == (1, 0)


def test_a_repeated_statement_on_a_table_with_triggers_keeps_its_own_scan(root: Path) -> None:
    # F-003: the same UPDATE twice, matching no row, so no trigger fires; each
    # execution walks claims, in one window and across windows.
    connection = _claims(root)
    connection.execute("CREATE TRIGGER touched AFTER UPDATE ON claims BEGIN SELECT 1; END")
    statement = "UPDATE claims SET value = 'same' WHERE other = 9"
    assert _rows(_scan(root, connection, statement, statement)) == 2 * ROWS
    assert _rows(_scan(root, connection, statement)) == ROWS
    assert _rows(_scan(root, connection, statement)) == ROWS


def _guarded_events(root: Path) -> sqlite3.Connection:
    """``events`` and ``events_view``, whose every change makes a trigger read all of claims."""

    connection = _claims(root)
    connection.executescript(
        """
        CREATE TABLE events(n INT);
        INSERT INTO events VALUES (1), (2), (3);
        CREATE TRIGGER events_in AFTER INSERT ON events BEGIN SELECT sum(other) FROM claims; END;
        CREATE TRIGGER events_up AFTER UPDATE ON events BEGIN SELECT sum(other) FROM claims; END;
        CREATE TRIGGER events_out AFTER DELETE ON events BEGIN SELECT sum(other) FROM claims; END;
        CREATE VIEW events_view AS SELECT * FROM events;
        CREATE TRIGGER events_view_in INSTEAD OF INSERT ON events_view
        BEGIN SELECT sum(other) FROM claims; END;
        """
    )
    return connection


def test_every_spelling_of_a_write_weighs_the_triggers_it_fires(root: Path) -> None:
    # F-007 and F-008: CTE prefixes, case, quoting and a view's INSTEAD OF
    # trigger; each statement fires one trigger (three traces), and every trace
    # weighs claims, which the trigger reads.
    connection = _guarded_events(root)
    for statement in (
        "INSERT INTO events VALUES (4)",
        "INSERT INTO EVENTS VALUES (4)",
        "WITH v AS (SELECT 5) INSERT INTO events SELECT * FROM v",
        "WITH v AS (SELECT 3) INSERT INTO EVENTS SELECT * FROM v",
        "WITH v AS (SELECT 7) INSERT INTO 'events' SELECT * FROM v",
        "WITH v AS (SELECT 5) INSERT INTO events_view SELECT * FROM v",
        "INSERT INTO events_view VALUES (6)",
        "WITH v AS (SELECT 1) UPDATE events SET n = n + 10 WHERE n IN (SELECT * FROM v)",
        "WITH v AS (SELECT 2) DELETE FROM events WHERE n IN (SELECT * FROM v)",
    ):
        window = _scan(root, connection, statement)
        assert window.counts["sql_statements"] == 3, statement
        assert _rows(window) >= 3 * ROWS, statement


def test_a_trigger_reached_through_another_trigger_is_weighed(root: Path) -> None:
    connection = _claims(root)
    connection.executescript(
        """
        CREATE TABLE a(n INT);
        CREATE TABLE b(n INT);
        CREATE TRIGGER a_in AFTER INSERT ON a BEGIN INSERT INTO b VALUES (NEW.n); END;
        CREATE TRIGGER b_in AFTER INSERT ON b BEGIN SELECT sum(other) FROM claims; END;
        """
    )
    window = _scan(root, connection, "INSERT INTO a VALUES (1)")
    assert window.counts["sql_statements"] >= 3
    assert _rows(window) >= window.counts["sql_statements"] * ROWS


def test_blobs_and_created_tables_do_not_break_planning(root: Path) -> None:
    connection = _claims(root)
    connection.execute("CREATE TABLE sums(name TEXT, value BLOB)")
    window = _scan(root, connection, "INSERT INTO sums VALUES ('a', x'ff00fe1b80')")
    assert _rows(window) == 0
    window = _scan(
        root, connection, "CREATE TABLE fresh(id INTEGER PRIMARY KEY REFERENCES claims(id))"
    )
    assert _rows(window) == 0


def test_a_guard_trigger_that_raises_does_not_break_planning(root: Path) -> None:
    connection = _claims(root)
    connection.executescript(
        """
        CREATE TABLE events(n INT);
        CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only; (really)'); END;
        """
    )
    window = _scan(root, connection, "INSERT INTO events VALUES (1)")
    assert (window.counts["sql_statements"], _rows(window)) == (1, 0)


def test_statements_that_may_walk_everything_weigh_every_table(root: Path) -> None:
    connection = _claims(root)
    for statement in ("PRAGMA integrity_check", "ANALYZE"):
        assert _rows(_scan(root, connection, statement)) >= ROWS, statement
    assert _rows(_scan(root, connection, "PRAGMA journal_mode")) == 0


# -- what the window cannot establish fails it -----------------------------------------


def test_work_the_window_cannot_establish_is_reported_never_zero(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _claims(root)

    def refuse(self: Any, callback: Any) -> None:
        raise sqlite3.ProgrammingError("no authorizer here")

    with monkeypatch.context() as patch:
        patch.setattr(_counters.TracedConnection, "set_authorizer", refuse)
        window = _scan(root, connection, "SELECT * FROM claims")
    assert window.unexplained and "authorizer cannot be installed" in window.unexplained[0]
    with monkeypatch.context() as patch:
        patch.setattr(_counters, "_touched", lambda events: {})
        window = _scan(root, connection, "INSERT INTO claims(claim_id) VALUES ('CLM-z')")
    assert window.unexplained and "no write" in window.unexplained[0]
    with monkeypatch.context() as patch:
        patch.setattr(_counters, "_object_at", lambda connection, schema, page: None)
        window = _scan(root, connection, "SELECT * FROM claims WHERE value = 'x'")
    assert window.unexplained and "names no object" in window.unexplained[0]


def test_the_growth_rule_rejects_a_scan_that_grows_with_the_table(root: Path) -> None:
    small = _claims(root, rows=20, name="small.sqlite3")
    large = _claims(root, rows=200, name="large.sqlite3")
    statement = "SELECT c.id FROM claims AS c WHERE c.value = 'absent' ORDER BY c.claim_id LIMIT 1"
    counted = [_rows(_scan(root, connection, statement)) for connection in (small, large)]
    assert counted == [20, 200]
    assert counted[1] > counted[0] * RATIO + SLACK["sql_scan_rows"]


# -- functions, opens and spawns --------------------------------------------------------


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
