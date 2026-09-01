"""Differential proof that incremental tree materialization equals a full read."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillGitError, SettlementIntegrityError
from cruxible_core.playbill.git import GitTreeChange
from cruxible_core.playbill.recovery import _materialize_successor_tree

from ._adoption_fixture import MINIATURE, AdoptionFixtureProfile, build_fixture


def _walk(instance) -> list[str]:  # type: ignore[no-untyped-def]
    return list(instance._ledger.main_history())


@pytest.mark.parametrize("seed", [7, 11, 13])
def test_incremental_materialization_equals_a_full_tree_read(tmp_path: Path, seed: int) -> None:
    """Every step of a randomized multi-generation walk reproduces `read_tree` exactly.

    The walk is randomized in its shape and seeded in its values, so it covers
    additions, revisions and multi-member closures across many generations
    rather than one hand-picked pair of commits.
    """

    profile = AdoptionFixtureProfile(
        name=f"differential-{seed}",
        subjects=4,
        claim_types=3,
        documents=2,
        query_definitions=1,
        seed_claims=4,
        generations=6,
        claims_per_generation=1 + seed % 3,
        seed=f"differential-{seed}",
    )
    fixture = build_fixture(tmp_path, profile)
    ledger = fixture.instance._ledger
    history = _walk(fixture.instance)
    assert len(history) == fixture.head_sequence + 1

    random.Random(seed).shuffle(order := list(range(1, len(history))))
    parent_tree = ledger.read_tree(history[0])
    for index in range(1, len(history)):
        expected = ledger.read_tree(history[index])
        incremental = _materialize_successor_tree(
            ledger,
            parent_oid=history[index - 1],
            oid=history[index],
            parent_tree=parent_tree,
        )
        assert incremental == expected
        # Iteration order matters as much as content: a consumer that reads the
        # mapping without sorting must not see a different tree either way.
        assert list(incremental) == list(expected)
        parent_tree = incremental

    # The same equality must hold for arbitrary already-accepted parents, not
    # only for the order the sliding window happens to walk them in.
    for index in order:
        cold_parent = ledger.read_tree(history[index - 1])
        assert _materialize_successor_tree(
            ledger,
            parent_oid=history[index - 1],
            oid=history[index],
            parent_tree=cold_parent,
        ) == ledger.read_tree(history[index])


def test_changed_entries_reports_only_the_changed_members(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, MINIATURE)
    ledger = fixture.instance._ledger
    history = ledger.main_history()
    changes = ledger.changed_entries(history[-2], history[-1])
    parent_tree = ledger.read_tree(history[-2])
    child_tree = ledger.read_tree(history[-1])
    reported = {change.path for change in changes}
    actually_changed = {
        path
        for path in set(parent_tree) | set(child_tree)
        if parent_tree.get(path) != child_tree.get(path)
    }
    assert reported == actually_changed
    assert all(change.mode == "100644" for change in changes if change.oid is not None)


def test_a_deleted_member_absent_from_the_parent_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path, MINIATURE)
    ledger = fixture.instance._ledger
    history = ledger.main_history()

    def forged(base_oid: str, target_oid: str) -> tuple[GitTreeChange, ...]:
        return (
            GitTreeChange(
                path="claims/ff/CLM-" + "f" * 32 + ".json", status="D", mode="000000", oid=None
            ),
        )

    ledger.changed_entries = forged  # type: ignore[method-assign]
    with pytest.raises(SettlementIntegrityError):
        _materialize_successor_tree(
            ledger,
            parent_oid=history[-2],
            oid=history[-1],
            parent_tree=ledger.read_tree(history[-2]),
        )


def test_a_non_regular_member_is_refused_exactly_as_a_full_read_refuses_it(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path, MINIATURE)
    ledger = fixture.instance._ledger
    history = ledger.main_history()
    real = ledger.changed_entries(history[-2], history[-1])
    smuggled = tuple(
        GitTreeChange(path=change.path, status=change.status, mode="120000", oid=change.oid)
        for change in real
        if change.oid is not None
    )

    def forged(base_oid: str, target_oid: str) -> tuple[GitTreeChange, ...]:
        return smuggled

    ledger.changed_entries = forged  # type: ignore[method-assign]
    with pytest.raises(PlaybillGitError):
        _materialize_successor_tree(
            ledger,
            parent_oid=history[-2],
            oid=history[-1],
            parent_tree=ledger.read_tree(history[-2]),
        )
