"""An unchanged Line run stores what the run itself is, never one more record per item.

``dev.roadmap_item/unchanged-run-cost``: a Line run whose source bytes and
every Claim are unchanged must not grow the instance's stored bytes (CAS,
exhaust, projections, journals and their indexes) with its items, Claims or
fetched responses. This route builds one settle world at two sizes, SMALL and
LARGE (ten times SMALL) live Claims fed by one workspace file, settles them
once, runs the Line once more unchanged (warm-up: the bodies an unchanged run
can repeat land here), and measures a third, unchanged run.

Counted over the measured run, by snapshotting the managed root around it:

- ``cas_files``: new files under ``cas/`` (each a 4 KiB allocation at least);
- ``stored_bytes``: growth of every file under the managed root (CAS objects,
  journal logs, SQLite indexes and their WAL files, projections);
- ``journal_appends``: journal records appended, every journal included;
- ``index_rows``: growth of the row count of every SQLite table under the root.

The rule, for every counter: ``LARGE <= SMALL * RATIO + SLACK``. A counter that
still grows with items is a strict xfail naming why, so the fix that removes
the growth turns it into an XPASS failure that forces the mark off.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts import canonical
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.exhaust.backends import LocalJournalBackend
from tests.test_procedures import test_procedure_settlement_delivery as settle_support
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import SUBJECT_KIND
from tests.test_scale.test_settle_per_item import (
    _advertise_on_the_measuring_thread,
    _create_subjects,
    _item,
    _roomy_selection_budget,
    _subject_id,
)

SMALL, LARGE = 12, 120
RATIO = 1.5
SLACK = {"cas_files": 4, "stored_bytes": 64 * 1024, "journal_appends": 4, "index_rows": 16}
COUNTED = tuple(SLACK)

#: Step 2 of the unchanged-run plan (Capture reuse with the content-keyed
#: observation index) is what removes these; each names what still grows.
STEP_2 = {
    "cas_files": (
        "every unchanged Claim still journals a re-observation payload, and the "
        "terminal's chunks, children and request bodies name the run's new Capture"
    ),
    "stored_bytes": (
        "per-Claim re-observation records and terminal bodies that name the run's new "
        "Capture envelope (a re-fetch mints a new Capture until reuse)"
    ),
    "journal_appends": "one claim_reobservation journal record per unchanged Claim",
    "index_rows": "re-observation journal index and projection rows per unchanged Claim",
}


def _files(root: Path) -> dict[str, int]:
    return {str(path): path.stat().st_size for path in root.rglob("*") if path.is_file()}


def _rows(root: Path) -> int:
    total = 0
    for path in root.rglob("*.sqlite*"):
        if path.suffix in {".sqlite-wal", ".sqlite-shm"} or path.name.endswith(("-wal", "-shm")):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            tables = [
                str(name)
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                total += int(conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
        except sqlite3.DatabaseError:
            continue
        finally:
            conn.close()
    return total


@contextmanager
def _appends_counted() -> Iterator[Counter[str]]:
    counts: Counter[str] = Counter()
    append = LocalJournalBackend.append
    append_many = LocalJournalBackend.append_many

    def one(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts["journal_appends"] += 1
        return append(self, *args, **kwargs)

    def many(self: Any, drafts: Any, **kwargs: Any) -> Any:
        counts["journal_appends"] += len(drafts)
        return append_many(self, drafts, **kwargs)

    LocalJournalBackend.append = one  # type: ignore[method-assign]
    LocalJournalBackend.append_many = many  # type: ignore[method-assign]
    try:
        yield counts
    finally:
        LocalJournalBackend.append = append  # type: ignore[method-assign]
        LocalJournalBackend.append_many = append_many  # type: ignore[method-assign]


def _run(instance: Any, root: Path, line: Any, *, minutes: int) -> Any:
    instance.settled_workspace_advertisement()
    state = settle_support.run_settle(
        instance, root, line, now=fixtures.NOW + timedelta(minutes=minutes)
    )
    assert state.status == "succeeded", state.terminal
    return state


def _measure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int) -> Counter[str]:
    tmp_path.mkdir(parents=True)
    _roomy_selection_budget(monkeypatch)
    _advertise_on_the_measuring_thread(monkeypatch)
    instance, root, line = settle_support.settle_world(
        tmp_path,
        claims_terminal=True,
        claims_within=tuple(subject_path(SUBJECT_KIND, _subject_id(i)) for i in range(count)),
    )
    _create_subjects(instance, settle_support.OWNERS[instance.root], count)
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical.canonical_bytes({"proposals": [_item(index) for index in range(count)]})
    )
    settled = _run(instance, root, line, minutes=10)
    (egress,) = settled.terminal_egress
    assert egress.settle_outcome == "settled" and len(egress.target_paths) == count, egress
    _run(instance, root, line, minutes=20)  # the first unchanged run: warm-up
    head = instance.accepted_coordinate()
    managed = Path(instance.root)
    files, rows = _files(managed), _rows(managed)
    with _appends_counted() as counts:
        state = _run(instance, root, line, minutes=30)
    # Unchanged: no proposal, no generation, every item restated what is live.
    (egress,) = state.terminal_egress
    assert egress.proposal_id is None and len(egress.children) == count, egress
    assert instance.accepted_coordinate() == head
    after = _files(managed)
    counts["cas_files"] = sum(
        1 for path in after if path not in files and "/cas/" in path.replace("\\", "/")
    )
    counts["stored_bytes"] = sum(after.values()) - sum(files.values())
    counts["index_rows"] = _rows(managed) - rows
    return counts


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Counter[str]]:
    monkeypatch = pytest.MonkeyPatch()
    try:
        root = tmp_path_factory.mktemp("unchanged-run")
        return {size: _measure(root / str(size), monkeypatch, size) for size in (SMALL, LARGE)}
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    "counter",
    [
        pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=STEP_2[name]))
        if name in STEP_2
        else name
        for name in COUNTED
    ],
)
def test_an_unchanged_line_run_stores_the_same_at_ten_times_the_items(
    measured: dict[int, Counter[str]], counter: str
) -> None:
    small, large = measured[SMALL][counter], measured[LARGE][counter]
    assert large <= small * RATIO + SLACK[counter], (counter, small, large, measured)
