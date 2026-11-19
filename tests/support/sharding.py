"""Split the test suite into CI shards by test file.

CI runs the suite as several parallel jobs. Each job sets
``CRUXIBLE_TEST_SHARD=i/N`` (1-based) and keeps only the test files assigned to
shard ``i`` of ``N``; with the variable unset nothing is deselected.

The split is by whole file, as ``--dist loadfile`` runs them: module fixtures
and module state assume a file runs in one process. Each file lands in exactly
one shard. Files are balanced greedily (heaviest first, onto the lightest
shard) by a weight: the file's seconds in the committed duration table
(``shard_durations.json`` beside this module) when it has an entry, otherwise
its collected test count scaled to the table's seconds per test, or the bare
count when there is no table. The assignment depends only on the collected
node ids and the table, so every xdist worker computes the same split.

With ``CRUXIBLE_TEST_DURATIONS_OUT=<path>`` a run writes its per-file seconds
there. CI uploads one file per shard; merging one Python version's files gives
a new table:
``jq -S -s add durations-*.json > tests/support/shard_durations.json``.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

SHARD_ENV = "CRUXIBLE_TEST_SHARD"
DURATIONS_OUT_ENV = "CRUXIBLE_TEST_DURATIONS_OUT"
DURATIONS_TABLE = Path(__file__).resolve().parent / "shard_durations.json"

COLLECTED_NODEIDS = pytest.StashKey[list[str]]()


def parse_shard(value: str) -> tuple[int, int]:
    """Parse ``i/N`` (1 <= i <= N) into ``(i, N)``."""

    index_text, sep, count_text = value.strip().partition("/")
    try:
        if not sep:
            raise ValueError
        index, count = int(index_text), int(count_text)
    except ValueError:
        raise ValueError(f"{SHARD_ENV} must look like i/N, got {value!r}") from None
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"{SHARD_ENV}={value!r}: need 1 <= i <= N")
    return index, count


def file_of(nodeid: str) -> str:
    """The test file a node id belongs to, as pytest spells it (rootdir-relative)."""

    return nodeid.split("::", 1)[0]


def load_durations(path: Path = DURATIONS_TABLE) -> dict[str, float]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(name): float(seconds) for name, seconds in raw.items()}


def file_weights(nodeids: Iterable[str], durations: Mapping[str, float]) -> dict[str, float]:
    counts = Counter(file_of(nodeid) for nodeid in nodeids)
    known = [name for name in counts if name in durations]
    known_tests = sum(counts[name] for name in known)
    per_test = sum(durations[name] for name in known) / known_tests if known_tests else 1.0
    return {
        name: durations[name] if name in durations else count * per_test
        for name, count in counts.items()
    }


def assign_shards(weights: Mapping[str, float], count: int) -> dict[str, int]:
    """Map each file to a 1-based shard: heaviest file first, onto the lightest shard."""

    loads = [0.0] * count
    assignment: dict[str, int] = {}
    for name in sorted(weights, key=lambda name: (-weights[name], name)):
        shard = min(range(count), key=lambda i: (loads[i], i))
        loads[shard] += weights[name]
        assignment[name] = shard + 1
    return assignment


def shard_nodeids(
    nodeids: Iterable[str], index: int, count: int, durations: Mapping[str, float]
) -> set[str]:
    """The node ids of shard ``index`` of ``count``."""

    ids = list(nodeids)
    assignment = assign_shards(file_weights(ids, durations), count)
    return {nodeid for nodeid in ids if assignment[file_of(nodeid)] == index}


class ShardPlugin:
    """Selects this run's shard and, on request, records per-file seconds."""

    def __init__(self, config: pytest.Config) -> None:
        self._config = config
        self._seconds: dict[str, float] = {}

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # Runs after -k/-m deselection. The whole collection stays in the stash
        # so the guard test checks the split on every run, sharded or not.
        nodeids = [item.nodeid for item in items]
        self._config.stash[COLLECTED_NODEIDS] = nodeids
        value = os.environ.get(SHARD_ENV, "").strip()
        if not value:
            return
        index, count = parse_shard(value)
        keep = shard_nodeids(nodeids, index, count, load_durations())
        deselected = [item for item in items if item.nodeid not in keep]
        if deselected:
            self._config.hook.pytest_deselected(items=deselected)
            items[:] = [item for item in items if item.nodeid in keep]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if os.environ.get(DURATIONS_OUT_ENV):
            name = file_of(report.nodeid)
            self._seconds[name] = self._seconds.get(name, 0.0) + report.duration

    def pytest_sessionfinish(self) -> None:
        out = os.environ.get(DURATIONS_OUT_ENV)
        # xdist workers forward their reports to the controller, which writes.
        if not out or hasattr(self._config, "workerinput"):
            return
        table = {name: round(seconds, 2) for name, seconds in sorted(self._seconds.items())}
        Path(out).write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
