"""CI shards (CRUXIBLE_TEST_SHARD=i/N) split the whole collection by file, once each."""

from __future__ import annotations

import os
import re
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from tests.support.sharding import (
    COLLECTED_NODEIDS,
    SHARD_ENV,
    assign_shards,
    file_of,
    file_weights,
    load_durations,
    parse_shard,
    shard_nodeids,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _collected(config: pytest.Config) -> list[str]:
    return config.stash[COLLECTED_NODEIDS]


@pytest.mark.parametrize("count", [1, 2, 3, 4, 6, 8])
def test_the_shards_partition_this_runs_whole_collection_by_file(
    request: pytest.FixtureRequest, count: int
) -> None:
    # The stash holds every collected node id, before this run's own shard was taken.
    nodeids = _collected(request.config)
    durations = load_durations()
    shards = [shard_nodeids(nodeids, index, count, durations) for index in range(1, count + 1)]

    assert set().union(*shards) == set(nodeids)
    assert sum(len(shard) for shard in shards) == len(set(nodeids))  # disjoint
    shards_of_file: dict[str, set[int]] = defaultdict(set)
    for index, shard in enumerate(shards, start=1):
        for nodeid in shard:
            shards_of_file[file_of(nodeid)].add(index)
    assert all(len(owners) == 1 for owners in shards_of_file.values())
    if len(shards_of_file) >= count:
        assert all(shards)


def test_a_sharded_run_keeps_exactly_its_own_shard(request: pytest.FixtureRequest) -> None:
    value = os.environ.get(SHARD_ENV, "").strip()
    nodeids = _collected(request.config)
    selected = {item.nodeid for item in request.session.items}
    if not value:
        assert selected == set(nodeids)
        return
    index, count = parse_shard(value)
    assert selected == shard_nodeids(nodeids, index, count, load_durations())


@pytest.mark.parametrize("value", ["", "4", "0/4", "5/4", "a/4", "1/0", "1/b"])
def test_a_malformed_shard_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match=SHARD_ENV):
        parse_shard(value)


def test_shard_values_parse_one_based() -> None:
    assert parse_shard("1/4") == (1, 4)
    assert parse_shard(" 4/4 ") == (4, 4)


def test_files_weigh_by_table_seconds_else_by_scaled_test_count() -> None:
    nodeids = [
        "tests/a.py::t1",
        "tests/a.py::t2",
        "tests/b.py::t1",
        "tests/b.py::t2",
        "tests/b.py::t3",
        "tests/b.py::t4",
    ]
    assert file_weights(nodeids, {}) == {"tests/a.py": 2.0, "tests/b.py": 4.0}
    # a.py: 30 s over 2 tests = 15 s per test, so the unknown b.py weighs 4 * 15.
    assert file_weights(nodeids, {"tests/a.py": 30.0}) == {"tests/a.py": 30.0, "tests/b.py": 60.0}


def test_files_go_heaviest_first_onto_the_lightest_shard() -> None:
    weights = {"big": 10.0, "mid": 6.0, "small1": 4.0, "small2": 3.0, "tiny": 1.0}
    assignment = assign_shards(weights, 2)
    # big -> 1; mid -> 2; small1 -> 2 (6 < 10); small2 -> 1 (tie, lower index); tiny -> 2.
    assert assignment == {"big": 1, "mid": 2, "small1": 2, "small2": 1, "tiny": 2}
    assert assign_shards(dict(reversed(weights.items())), 2) == assignment  # order-free


def test_ci_runs_every_shard_of_the_count_it_declares() -> None:
    # A shard list shorter than the declared count would silently drop files.
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    job = workflow["jobs"]["test"]
    declared = re.fullmatch(r"\$\{\{ matrix\.shard \}\}/(\d+)", job["env"][SHARD_ENV])
    assert declared is not None
    count = int(declared.group(1))
    assert job["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    assert f"/{count})" in job["name"]
