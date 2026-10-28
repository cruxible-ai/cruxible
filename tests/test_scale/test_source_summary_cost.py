"""``orient(section="sources")`` costs its sources, and a landing's upkeep is O(1).

Two Procedure-run journal indexes hold the same five logical sources, each with
landings under two contracts, SMALL and LARGE (ten times as many) landings per
source, beside as many landings naming no logical source. Reading one page of
sources (the tally walk, each source's latest-landing seek and the unattributed
tally: every statement the page runs against the index) and projecting one more
landing (its row and the triggers that tally it) run the same SQL work at both
sizes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from cruxible_core.exhaust.journal_index import _SCHEMA, _statements, source_landing_page
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

SOURCES = ("cisa.kev.catalog", "ghsa.advisories", "nvd.cve", "osv.records", "pypi.release")
STREAM = "stream"
SMALL = 20
SIZES = (SMALL, 10 * SMALL)


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _landing(record_id: int, source: str | None, index: int) -> tuple[tuple[object, ...], ...]:
    at = f"2026-10-{1 + index // 1440:02d}T{index // 60 % 24:02d}:{index % 60:02d}:00Z"
    record = (
        record_id,
        STREAM,
        f"run:{record_id}",
        1,
        0,
        1,
        "d",
        "p",
        "produced_capture",
        f"RUN-{record_id}",
        None,
        f"payload-{record_id}",
        at,
    )
    selector = (record_id, f"contract-{index % 2}", source, STREAM, at, 0)
    return record, selector


_RECORD = (
    "INSERT INTO records(id,stream,partition_id,sequence,offset,size,digest,previous,"
    "event_kind,run_id,occurrence_id,payload_digest,recorded_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
_SELECTOR = (
    "INSERT INTO capture_selectors(record_id,contract_digest,logical_source,stream,"
    "landed_at,source_lost) VALUES (?,?,?,?,?,?)"
)


def _index(root: Path, per_source: int) -> sqlite3.Connection:
    conn = sqlite3.connect(root / f"index-{per_source}.sqlite3", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN")
    _statements(conn, _SCHEMA)
    records, selectors = [], []
    record_id = 0
    for source in (*SOURCES, None):
        for index in range(per_source):
            record_id += 1
            record, selector = _landing(record_id, source, index)
            records.append(record)
            selectors.append(selector)
    conn.executemany(_RECORD, records)
    conn.executemany(_SELECTOR, selectors)
    conn.execute("COMMIT")
    return conn


def _flat(steps: list[int]) -> bool:
    small, large = steps
    return small > 0 and large <= small * RATIO + SLACK["sql_vm_steps"]


@pytest.mark.parametrize("after", [None, SOURCES[0]], ids=["first", "continued"])
def test_a_sources_page_is_flat_in_the_landings_per_source(root: Path, after: str | None) -> None:
    steps, statements = [], []
    for per_source in SIZES:
        conn = _index(root, per_source)
        with counting(root) as window:
            page = source_landing_page(conn, STREAM, limit=3, after=after)
        assert window.unexplained == [], window.unexplained
        expected = SOURCES[:3] if after is None else SOURCES[1:4]
        assert [item.source for item in page.sources] == list(expected)
        assert page.stop == expected[-1] and page.unattributed == 0
        assert all(
            item.counts == (("contract-0", per_source // 2), ("contract-1", per_source // 2))
            for item in page.sources
        )
        steps.append(window.counts["sql_vm_steps"])
        statements.append(window.counts["sql_statements"])
        conn.close()
    # One tally walk, one latest-landing seek per source, one unattributed read.
    assert statements == [5, 5]
    assert _flat(steps), (steps, window.attribution[:3])


def test_projecting_one_landing_is_flat_in_the_landings_already_tallied(root: Path) -> None:
    steps = []
    for per_source in SIZES:
        conn = _index(root, per_source)
        record, selector = _landing(10**6, SOURCES[2], 7)
        conn.execute(_RECORD, record)
        with counting(root) as window:
            conn.execute(_SELECTOR, selector)
        assert window.unexplained == [], window.unexplained
        steps.append(window.counts["sql_vm_steps"])
        tallied = conn.execute(
            "SELECT landings FROM capture_source_tallies "
            "WHERE stream=? AND logical_source=? AND contract_digest='contract-1'",
            (STREAM, SOURCES[2]),
        ).fetchone()
        assert tallied == (per_source // 2 + 1,)
        conn.close()
    assert _flat(steps), steps
