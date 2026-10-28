"""``orient(section="sources")`` costs its sources, and a landing's upkeep is O(1).

Two Procedure-run journal indexes hold the same five logical sources, each with
landings under two contracts, SMALL and LARGE (ten times as many) landings per
source, beside as many landings naming no logical source. Reading one page of
sources (the tally walk, each source's latest-landing seek and the unattributed
tally: every statement the page runs against the index) and projecting one more
landing (its row and the triggers that tally it) run the same SQL work at both
sizes.

The served read is measured whole too: ``service_playbill_orient`` for one page
on two instances, the LARGE one with ten times the landings (of the listed
source and of other sources) and ten times the evidence worker's unrelated
findings (200 against 2,000), the page's latest Capture missing its body without an erasure so its
availability consults the findings. Every counter (SQL statements and VM
steps, file opens, CAS reads, projection binds, parses and digests) stays
within the scale guard's rule: contract resolution, the landing's admission,
payload and envelope reads and the findings read cost the page.
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


def _served_world(root: Path, extra: int) -> object:
    """The sources world with ``extra`` more landings and ``10 * extra`` unrelated findings."""

    from cruxible_core.consumers.next.evidence import _STATE
    from tests.test_service.test_orient_sources import KEV, SELECTOR, SourcesWorld

    root.mkdir()
    world = SourcesWorld(root)
    for index in range(extra):
        source = KEV if index % 2 else f"zz.other-{index:04d}"
        world.land(source, SELECTOR.capture_contract_digest, 10 + index)
    latest = world.land(KEV, SELECTOR.capture_contract_digest, 10 + extra)
    from cruxible_core.exhaust.payloads import read_payload
    from cruxible_core.storage.capture_metadata import capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    access = BodyAccessContext(principal_id="cost", can_read_body=True)
    payload = read_payload(world.instance.body_store(), latest.record.payload_digest, access=access)
    envelope = capture_envelope(
        world.instance.body_store(), payload["capture_digest"], access=access
    )
    world.instance.body_store().erase(envelope.commitment.digest)  # missing, never erased
    with _STATE.open(world.instance) as connection:
        assert connection is not None
        connection.executemany(
            "INSERT INTO findings VALUES (?,?,?,?,?)",
            [
                (
                    f"sha256:{index:064x}",
                    "body",
                    f"sha256:{index:064x}",
                    "missing",
                    "2026-10-28T00:00:00.000000Z",
                )
                for index in range(10 * extra)
            ],
        )
    return world


def test_a_served_sources_page_is_flat_in_landings_and_unrelated_findings(root: Path) -> None:
    from cruxible_core.service.discovery.orient import service_playbill_orient
    from tests.test_scale._counters import COUNTERS

    counts = []
    for extra in SIZES:
        world = _served_world(root / f"served-{extra}", extra)
        instance = world.instance  # type: ignore[attr-defined]
        warm = service_playbill_orient(instance, section="sources", limit=1)
        assert warm.sources is not None
        with counting(root) as window:
            answer = service_playbill_orient(instance, section="sources", limit=1)
        assert window.unexplained == [], window.unexplained
        assert answer.sources is not None and len(answer.sources) == 1
        (row,) = answer.sources
        assert row.logical_source == "cisa.kev.catalog" and row.landings == 3 + extra // 2 + 1
        assert row.latest.status == "unavailable" and row.latest.erased is None
        counts.append(dict(window.counts))
    small, large = counts
    grown = {
        name: (small[name], large[name])
        for name in COUNTERS
        if large[name] > small[name] * RATIO + SLACK[name]
    }
    assert not grown, grown
