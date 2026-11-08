"""D-R48 fix round, F1 and F2: two orient/get reads flat in population size.

F1: ``orient``'s Runbook count read a maintained singleton
(``runbook_live_count``), moved by one AFTER INSERT trigger per accepted live
Runbook, instead of ``count(*) FROM runbooks``. Runbooks are append-only (a
retirement is its own new row, never an update of an old one), so only the
insert ever moves it.

F2: a Line or Runbook card's aimed-Trigger total and schedule kinds come from
the maintained ``trigger_aim_counts`` counters, never a GROUP BY or a window
function over the target's retained Trigger population; its first page of
named Triggers is a direct ``triggers_by_target`` index seek capped at the
page limit. Both tables are populated by the same schema every other owner
table uses (``schema_sql()``), so a cold build and an incremental accept see
the same counters.

Three sizes (10, 100, 1,000 live Runbooks / Triggers aimed at one Runbook)
run the same SQL work: a route whose cost grew with the population would show
it moving between these points, not staying still.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.operational_reads import OPERATIONAL_CARD_LIST_LIMIT
from cruxible_core.indexes.typed_state import RUNBOOK_LIVE_COUNT_SQL, schema_sql
from cruxible_core.service.discovery.operational import aimed_trigger_page
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

SIZES = (10, 100, 1000)

_RUNBOOK_COLUMNS = (
    "identity,path,format_tag,artifact_digest,predecessor_digest,revision,lifecycle,step_count"
)
_TRIGGER_COLUMNS = (
    "identity,path,format_tag,artifact_digest,predecessor_digest,revision,lifecycle,"
    "target_kind,target,schedule_kind,interval_seconds"
)


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _runbook_row(identity: str, index: int) -> tuple[object, ...]:
    return (
        identity,
        f"runbooks/{index}.json",
        "runbook-v1",
        f"sha256:{index:064x}",
        None,
        1,
        "live",
        1,
    )


def _trigger_row(identity: str, index: int, target: str, schedule_kind: str) -> tuple[object, ...]:
    return (
        identity,
        f"triggers/{index}.json",
        "trigger-v1",
        f"sha256:{index:064x}",
        None,
        1,
        "live",
        "runbook",
        target,
        schedule_kind,
        None,
    )


def _runbook_world(root: Path, live: int) -> sqlite3.Connection:
    """``live`` accepted Runbooks, nothing else."""

    connection = sqlite3.connect(root / f"runbooks-{live}.sqlite3", isolation_level=None)
    connection.executescript(schema_sql())
    connection.execute("BEGIN")
    connection.executemany(
        f"INSERT INTO runbooks ({_RUNBOOK_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
        [_runbook_row(f"Runbook:r{i}", i) for i in range(live)],
    )
    connection.execute("COMMIT")
    return connection


def _trigger_world(root: Path, aimed: int) -> sqlite3.Connection:
    """Two Runbooks: ``target`` with ``aimed`` live Triggers, ``other`` with three unrelated."""

    connection = sqlite3.connect(root / f"triggers-{aimed}.sqlite3", isolation_level=None)
    connection.executescript(schema_sql())
    connection.execute("BEGIN")
    connection.executemany(
        f"INSERT INTO runbooks ({_RUNBOOK_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
        [_runbook_row("Runbook:target", 0), _runbook_row("Runbook:other", 1)],
    )
    rows = [
        _trigger_row(f"Trigger:target-{i}", i, "Runbook:target", "cadence" if i % 2 else "cron")
        for i in range(aimed)
    ]
    rows.extend(
        _trigger_row(f"Trigger:other-{i}", 10_000 + i, "Runbook:other", "cron") for i in range(3)
    )
    connection.executemany(
        f"INSERT INTO triggers ({_TRIGGER_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    connection.execute("COMMIT")
    return connection


def _flat(first: int, last: int) -> bool:
    return last <= first * RATIO + SLACK["sql_vm_steps"]


def test_the_runbook_count_is_flat_in_the_number_of_runbooks(root: Path) -> None:
    steps, statements = [], []
    for live in SIZES:
        connection = _runbook_world(root, live)
        with counting(root) as window:
            (amount,) = connection.execute(RUNBOOK_LIVE_COUNT_SQL).fetchone()
        assert window.unexplained == [], window.unexplained
        assert amount == live
        statements.append(window.counts["sql_statements"])
        steps.append(window.counts["sql_vm_steps"])
        connection.close()
    # One primary-key seek of the maintained singleton, whatever the population.
    assert statements == [1, 1, 1], statements
    assert _flat(steps[0], steps[-1]), steps


def test_a_runbook_cards_aimed_triggers_are_flat_in_how_many_are_aimed(root: Path) -> None:
    steps, statements = [], []
    for aimed in SIZES:
        connection = _trigger_world(root, aimed)
        projection = SimpleNamespace(typed=SimpleNamespace(connection=connection))
        with counting(root) as window:
            page = aimed_trigger_page(
                projection,
                ("Runbook:target",),
                limit=OPERATIONAL_CARD_LIST_LIMIT,
                target_kind="runbook",
            )["Runbook:target"]
        assert window.unexplained == [], window.unexplained
        assert page.total == aimed
        assert page.schedule_kinds == ("cadence", "cron")
        assert len(page.identities) == min(aimed, OPERATIONAL_CARD_LIST_LIMIT)
        statements.append(window.counts["sql_statements"])
        steps.append(window.counts["sql_vm_steps"])
        connection.close()
    # One batched seek of the maintained counters, one capped index seek of the named page.
    assert statements == [2, 2, 2], statements
    assert _flat(steps[0], steps[-1]), steps
