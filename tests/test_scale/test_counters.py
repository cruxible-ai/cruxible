"""The scale guard's counters count what they claim: focused checks on scratch databases.

No world is built here. Each check opens a window over a scratch root and runs
one shape of work the guard must see (or must refuse to guess about). SQL work
is measured, not inferred: ``sql_vm_steps`` is the VM instructions SQLite ran on
the measuring thread, so a walk over a growing table grows with it, while a seek
(unique or not) that returns a fixed handful of rows stays flat at any size. The
growth checks hold each shape to the guard's own rule and slack, SMALL against a
table ten times its size, as the guard's worlds are.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.test_scale import _counters
from tests.test_scale._counters import Window, counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

ROWS = 50
#: The guard's LARGE world holds about ten times SMALL's rows.
SIZES = (ROWS, 10 * ROWS)
SCAN = "SELECT * FROM claims WHERE value LIKE '%9'"


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _claims(root: Path, *, rows: int = ROWS, name: str = "state.sqlite3") -> sqlite3.Connection:
    """``rows`` Claims, five to a group: ``grp`` is a non-unique key with a fixed fan-out."""

    connection = sqlite3.connect(root / name, isolation_level=None)
    connection.executescript(
        """
        CREATE TABLE claims(
            id INTEGER PRIMARY KEY, claim_id TEXT UNIQUE, value TEXT, other INT, grp TEXT
        );
        CREATE INDEX claims_grp ON claims(grp);
        """
    )
    connection.executemany(
        "INSERT INTO claims(claim_id, value, other, grp) VALUES (?,?,?,?)",
        [(f"CLM-{n:05d}", f"value {n}", n % 7, f"g{n // 5}") for n in range(rows)],
    )
    return connection


def _run(root: Path, connection: sqlite3.Connection, *sql: str) -> Window:
    with counting(root) as window:
        for statement in sql:
            connection.execute(statement).fetchall()
    return window


def _steps(window: Window) -> int:
    assert window.unexplained == [], window.unexplained
    return window.counts["sql_vm_steps"]


def _sizes(root: Path, setup: str | None = None) -> list[sqlite3.Connection]:
    connections = []
    for rows in SIZES:
        connection = _claims(root, rows=rows, name=f"claims-{rows}.sqlite3")
        if setup is not None:
            connection.executescript(setup)
        connections.append(connection)
    return connections


def _grows(small: int, large: int) -> bool:
    return large > small * RATIO + SLACK["sql_vm_steps"]


def _measured(root: Path, connections: list[sqlite3.Connection], *sql: str) -> list[int]:
    return [_steps(_run(root, connection, *sql)) for connection in connections]


# -- what the rule must reject ---------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        SCAN,
        "SELECT * FROM claims AS c WHERE c.other = 3",
        "SELECT id FROM claims WHERE claim_id > 'CLM-00002'",
        # A sort over a column no index orders: every row is fed to the sorter.
        "SELECT id FROM claims ORDER BY value LIMIT 1",
        "SELECT c.id FROM claims c WHERE c.value = 'absent' ORDER BY c.claim_id LIMIT 1",
        "SELECT count(DISTINCT other) FROM claims",
        "WITH recent AS (SELECT * FROM claims WHERE id > 1) SELECT max(value) FROM recent",
    ],
)
def test_a_scan_or_range_over_a_growing_table_fails_the_rule(root: Path, statement: str) -> None:
    small, large = _measured(root, _sizes(root), statement)
    assert _grows(small, large), (statement, small, large)


def test_a_write_that_walks_a_growing_table_fails_the_rule(root: Path) -> None:
    update = "UPDATE claims SET other = other + 1 WHERE other = 3"
    small, large = _measured(root, _sizes(root), update)
    assert _grows(small, large), (small, large)


def test_a_foreign_key_check_that_walks_the_child_table_fails_the_rule(root: Path) -> None:
    setup = """
        PRAGMA foreign_keys = ON;
        CREATE TABLE groups(name TEXT PRIMARY KEY);
        INSERT INTO groups VALUES ('x'), ('y');
        CREATE TABLE members(claim INT, grp TEXT REFERENCES groups(name));
        INSERT INTO members SELECT id, 'y' FROM claims;
    """
    # members.grp has no index: deleting a parent row walks every member.
    small, large = _measured(root, _sizes(root, setup), "DELETE FROM groups WHERE name = 'x'")
    assert _grows(small, large), (small, large)


# -- what the rule must let through ----------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM claims WHERE claim_id = 'CLM-00007'",
        "SELECT * FROM claims WHERE id = 7",
        "SELECT value FROM claims WHERE claim_id IN ('CLM-00001', 'CLM-00009', 'CLM-00011')",
        "SELECT max(claim_id) FROM claims",
        # An ordered walk that stops after a fixed number of rows is bounded however long.
        "SELECT id FROM claims WHERE claim_id LIKE 'CLM-%' ORDER BY claim_id DESC LIMIT 1 "
        "OFFSET 40",
        "UPDATE claims SET value = 'w' WHERE id = 3",
        "INSERT INTO claims(claim_id, value, other, grp) VALUES ('CLM-new', 'v', 1, 'gx')",
        "DELETE FROM claims WHERE claim_id = 'CLM-00004'",
    ],
)
def test_a_seek_point_write_or_fixed_walk_passes_and_is_flat(root: Path, statement: str) -> None:
    small, large = _measured(root, _sizes(root), statement)
    assert small == large and not _grows(small, large), (statement, small, large)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM claims WHERE grp = 'g3'",
        "SELECT id FROM claims WHERE grp = 'g3' ORDER BY claim_id",
        "SELECT count(*) FROM claims WHERE grp = 'g3'",
    ],
)
def test_a_non_unique_equality_over_a_fixed_handful_passes_and_is_flat(
    root: Path, statement: str
) -> None:
    small, large = _measured(root, _sizes(root), statement)
    assert small == large and not _grows(small, large), (statement, small, large)


def test_the_slack_is_sixteen_bounded_statements_and_no_walk(root: Path) -> None:
    """``SLACK["sql_vm_steps"]`` is ``SLACK["sql_statements"]`` bounded statements at 64
    steps each: the dearest bounded statement here (a five-row equality) stays under 64,
    and a walk over the LARGE table alone exceeds the slack."""

    assert SLACK["sql_vm_steps"] == SLACK["sql_statements"] * 64
    connection = _claims(root, rows=SIZES[1])
    assert _measured(root, [connection], "SELECT * FROM claims WHERE grp = 'g3'")[0] <= 64
    assert _measured(root, [connection], SCAN)[0] > SLACK["sql_vm_steps"]


# -- what counts -----------------------------------------------------------------------


def test_trigger_work_counts_with_its_statement(root: Path) -> None:
    setup = """
        CREATE TABLE events(n INT);
        CREATE TRIGGER claims_events AFTER UPDATE OF value ON claims
        BEGIN INSERT INTO events SELECT count(*) FROM claims WHERE other = NEW.other; END;
    """
    plain, triggered = _claims(root, name="plain.sqlite3"), _claims(root, name="trig.sqlite3")
    triggered.executescript(setup)
    update = "UPDATE claims SET value = 'w' WHERE id = 3"
    assert _steps(_run(root, triggered, update)) > _steps(_run(root, plain, update)) + ROWS
    # The trigger's walk grows with the table, so its statement fails the rule.
    small, large = _measured(root, _sizes(root, setup), update)
    assert _grows(small, large), (small, large)


def test_a_trigger_reached_through_another_trigger_counts(root: Path) -> None:
    setup = """
        CREATE TABLE a(n INT); CREATE TABLE b(n INT); CREATE TABLE c(n INT);
        CREATE TRIGGER a_b AFTER INSERT ON a BEGIN INSERT INTO b VALUES (NEW.n); END;
        CREATE TRIGGER b_c AFTER INSERT ON b
        BEGIN INSERT INTO c SELECT count(*) FROM claims WHERE other = NEW.n; END;
    """
    small, large = _measured(root, _sizes(root, setup), "INSERT INTO a VALUES (1)")
    assert _grows(small, large), (small, large)


def test_a_statement_started_before_the_window_counts_its_steps_inside(root: Path) -> None:
    connection = _claims(root, rows=SIZES[1])
    cursor = connection.execute(SCAN)
    with counting(root) as window:
        cursor.fetchall()
    assert _steps(window) > SIZES[1]
    assert set(window.statements) == {_counters.BEFORE_ANY}


def test_a_connection_opened_inside_the_window_is_counted(root: Path) -> None:
    _claims(root).close()
    with counting(root) as window:
        connection = sqlite3.connect(root / "state.sqlite3")
        connection.execute(SCAN).fetchall()
    assert _steps(window) > ROWS
    connection.close()


def test_the_same_work_gives_the_same_count(root: Path) -> None:
    connection = _claims(root)
    sql = (
        "SELECT * FROM claims WHERE grp = 'g3'",
        SCAN,
        "UPDATE claims SET value = value WHERE id = 4",
    )
    first, second = _run(root, connection, *sql), _run(root, connection, *sql)
    assert first.counts == second.counts and first.statements == second.statements


def test_work_outside_the_root_is_not_counted(root: Path, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    outside = _claims(tmp_path_factory.mktemp("outside"))
    window = _run(root, outside, SCAN)
    assert window.counts["sql_vm_steps"] == window.counts["sql_statements"] == 0


# -- the measuring thread only ---------------------------------------------------------


def test_sql_on_other_threads_is_excluded(root: Path) -> None:
    _claims(root).close()
    shared = sqlite3.connect(root / "state.sqlite3", check_same_thread=False)

    def elsewhere() -> None:
        own = sqlite3.connect(root / "state.sqlite3")
        own.execute(SCAN).fetchall()
        own.close()
        shared.execute(SCAN).fetchall()

    with counting(root) as window:
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join()
        # The shared connection still counts on the measuring thread.
        shared.execute("SELECT * FROM claims WHERE id = 7").fetchall()
    assert window.counts["sql_statements"] == 1
    assert 0 < _steps(window) < ROWS
    shared.close()


def test_other_threads_do_not_count_opens_spawns_or_calls(root: Path) -> None:
    from cruxible_client.contracts import claims
    from cruxible_client.contracts.claims import ClaimFormatError

    (root / "inside.txt").write_text("x")
    raised = []

    def elsewhere() -> None:
        (root / "inside.txt").read_text()
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        try:
            claims.parse_claim(b"not json", path="claims/00/CLM-x.json")
        except ClaimFormatError:
            raised.append(True)

    with counting(root) as window:
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join()
    assert raised == [True]
    assert sum(window.counts.values()) == 0, window.counts


# -- what the window cannot count fails it ---------------------------------------------


def test_sql_on_a_connection_without_a_step_counter_is_reported_never_zero(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _claims(root)

    def refuse(self: Any, handler: Any, n: int) -> None:
        raise sqlite3.ProgrammingError("no progress handler here")

    with monkeypatch.context() as patch:
        patch.setattr(_counters.TracedConnection, "set_progress_handler", refuse)
        window = _run(root, connection, "SELECT * FROM claims")
    assert window.unexplained and "no step counter" in window.unexplained[0]


# -- attribution names the work, and never decides a count ------------------------------


def test_attribution_names_the_heaviest_statement_its_tables_and_cursors(root: Path) -> None:
    connection = _claims(root)
    window = _run(
        root,
        connection,
        "SELECT * FROM claims WHERE claim_id = 'CLM-00007'",
        "SELECT id FROM claims WHERE claim_id > 'CLM-00002'",
    )
    top = window.attribution[0]
    assert top["statement"] == "SELECT id FROM claims WHERE claim_id > ?"
    assert top["vm_steps"] == max(window.statements.values())
    assert top["tables"] == ["main.claims"]
    assert any("Next" in cursor for cursor in top["cursors"]), top["cursors"]
    assert top["runs"] == 1 and sum(steps for _, steps, _ in top["paths"]) == top["vm_steps"]
    assert sum(window.statements.values()) == window.counts["sql_vm_steps"]


def test_attribution_reads_a_closed_connection_through_its_file(root: Path) -> None:
    connection = _claims(root)
    with counting(root) as window:
        connection.execute(SCAN).fetchall()
        connection.close()
    top = window.attribution[0]
    assert "unavailable" not in top and top["tables"] == ["main.claims"], top


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
