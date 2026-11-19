"""Pair index upkeep grows with the change, never with unrelated pairs or a slot's history.

Three worlds, each at 100 and 1,000 (``SIZES``), accept the same one-Claim
change through the projection's accept path (``replace_rows``) and count its
SQL work:

- ``unrelated``: 100 vs 1,000 other pairs, each holding a live fact; the change
  is a first fact on one more pair (a 0 -> 1 slot: its units inserted);
- ``shells``: one end with 10 current pairs and 100 vs 1,000 historical shells
  (pairs whose fact was retired); the change is a first fact on one shell;
- ``history``: one pair slot with 100 vs 1,000 retired versions and one live
  holder; the change retires that last holder (a 1 -> 0 slot: its units
  deleted by full key, its ends read from ``pair_ends``) [F8].

Rule (the scale guard's): ``LARGE <= SMALL * 1.25 + SLACK``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.core_support._pair_support import (
    COMPONENT,
    EXPOSURE,
    SCOPE,
    VULN,
    claim,
    definitions,
    end,
    exposure,
    literal,
    pair_rows,
    path_of,
    projection,
    publish,
    put,
    retired_claim,
)
from tests.test_scale._counters import counting, trace_sqlite
from tests.test_scale.test_scale_guard import RATIO, SLACK

SIZES = (100, 1000)
COUNTED = ("sql_statements", "sql_vm_steps")


@pytest.fixture
def root(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def _base() -> dict[str, bytes]:
    tree = definitions()
    put(tree, *end(COMPONENT, "target", 1), *end(VULN, "v0", 2))
    put(tree, *exposure("target", "v0", 3))
    return tree


TARGET = path_of(EXPOSURE, "target--v0")


def _unrelated(size: int) -> tuple[dict[str, bytes], tuple[Any, ...]]:
    tree = _base()
    for index in range(size):
        number = 100_000 + 10 * index
        put(tree, *end(COMPONENT, f"o{index}", number), *exposure(f"o{index}", "v0", number + 1))
        pair = path_of(EXPOSURE, f"o{index}--v0")
        put(tree, claim(number + 3, SCOPE, literal("required"), subject=pair))
    return tree, (claim(50, SCOPE, literal("required"), subject=TARGET),)


def _shells(size: int) -> tuple[dict[str, bytes], tuple[Any, ...]]:
    tree = _base()
    for index in range(10 + size):
        number = 100_000 + 10 * index
        put(tree, *end(VULN, f"w{index}", number), *exposure("target", f"w{index}", number + 1))
        pair = path_of(EXPOSURE, f"target--w{index}")
        fact = claim(number + 3, SCOPE, literal("required"), subject=pair)
        put(tree, fact if index < 10 else retired_claim(fact))
    shell = path_of(EXPOSURE, f"target--w{10 + size - 1}")
    return tree, (claim(50, SCOPE, literal("optional"), subject=shell),)


def _history(size: int) -> tuple[dict[str, bytes], tuple[Any, ...]]:
    tree = _base()
    for index in range(size):
        put(
            tree, retired_claim(claim(100_000 + index, SCOPE, literal(f"r{index}"), subject=TARGET))
        )
    holder = claim(50, SCOPE, literal("required"), subject=TARGET)
    put(tree, holder)
    return tree, (retired_claim(holder),)


@pytest.mark.parametrize("world", [_unrelated, _shells, _history], ids=lambda f: f.__name__[1:])
def test_one_claim_accept_is_flat_in_pairs_shells_and_history(root: Path, world: Any) -> None:
    measured: list[dict[str, int]] = []
    for size in SIZES:
        tree, change = world(size)
        connection = projection(tree, path=root / f"{world.__name__}-{size}.sqlite3")
        changes = put({}, *change)
        before = pair_rows(connection)
        with counting(root) as window:
            publish(connection, changes, changed=changes)
        assert window.unexplained == [], window.unexplained
        assert pair_rows(connection) != before
        measured.append({counter: window.counts[counter] for counter in COUNTED})
        connection.close()
    small, large = measured
    for counter in COUNTED:
        assert small[counter] > 0
        assert large[counter] <= small[counter] * RATIO + SLACK[counter], (
            counter,
            measured,
            window.attribution[:3],
        )


# -- the candidate boundary on a routine write: flat in the keyed vocabulary (review F3) --


def _vocabulary(size: int) -> dict[str, bytes]:
    """The base world plus ``size`` retired and ``size`` live keyed SubjectKinds, unrelated."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.subject_kinds import (
        render_subject_kind,
        subject_kind_digest,
        subject_kind_path,
    )
    from tests.core_support._pair_support import keyed, successor_kind

    tree = _base()
    for index in range(size):
        retired = keyed(f"pt.r{index}", f"pt.r{index}.slug")
        tree[subject_kind_path(retired.subject_kind)] = render_subject_kind(
            successor_kind(
                retired,
                lifecycle=ArtifactLifecycle(
                    state="retired", predecessor_digest=subject_kind_digest(retired).tagged
                ),
            )
        )
        live = keyed(f"pt.l{index}", f"pt.l{index}.slug")
        tree[subject_kind_path(live.subject_kind)] = render_subject_kind(live)
    return tree


def test_a_fact_write_is_judged_flat_in_the_keyed_vocabulary(root: Path) -> None:
    from cruxible_core.indexes.subject_identity import candidate_identity_issues
    from tests.core_support._pair_support import HeldTree

    measured: list[dict[str, int]] = []
    for size in SIZES:
        tree = _vocabulary(size)
        connection = projection(tree, path=root / f"vocabulary-{size}.sqlite3")
        parent = HeldTree(tree, connection)
        changes = put({}, claim(50, SCOPE, literal("required"), subject=TARGET))
        with counting(root) as window:
            issues = candidate_identity_issues(
                current_tree=parent,
                candidate_tree={**tree, **changes},
                scope=tuple(sorted(changes)),
            )
        assert issues == ()
        assert window.unexplained == [], window.unexplained
        measured.append({counter: window.counts[counter] for counter in COUNTED})
        connection.close()
    small, large = measured
    for counter in COUNTED:
        assert small[counter] > 0
        assert large[counter] <= small[counter] * RATIO + SLACK[counter], (
            counter,
            measured,
            window.attribution[:3],
        )
