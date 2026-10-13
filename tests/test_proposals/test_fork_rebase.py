"""A stale-base rebase from two deltas equals the whole-tree rebases (perf B8)."""

from __future__ import annotations

import itertools

import pytest

from cruxible_client.contracts.canonical import CanonicalEncodingError, semantic_diff
from cruxible_core.derived.derived_state import SnapshotTree, advance_accepted_tree
from cruxible_core.proposals.proposals import (
    _ForkRebase,
    deterministic_rebase,
    deterministic_rebase_v2,
)

_BASE = {
    "claims/a.json": b"a0",
    "claims/b.json": b"b0",
    "claims/c.json": b"c0",
    "claims/d.json": b"d0",
    "subjects/k/s.json": b"s0",
    "cards/claims/a.json": b"card-a0",
    "changesets/0001.json": b"r1",
}

# (path, head bytes or None) -- what the head changed since the proposed base.
_HEAD_CHANGES = (
    {},
    {"claims/a.json": b"a-head"},
    {"claims/b.json": None, "claims/e.json": b"e-head"},
    {"claims/c.json": b"c-prop", "changesets/0002.json": b"r2"},
    {"claims/a.json": b"a-head", "claims/d.json": None, "cards/claims/a.json": b"card-h"},
)
# What the proposal changed from the base.
_PROPOSALS = (
    {"claims/a.json": b"a-prop"},
    {"claims/c.json": b"c-prop"},
    {"claims/b.json": b"b-prop", "claims/e.json": b"e-prop"},
    {"claims/d.json": None, "subjects/k/t.json": b"t-prop"},
    {"claims/a.json": b"a-prop", "claims/c.json": b"c-prop", "cards/claims/a.json": b"card-p"},
)


def _trees(head_changes, proposal):  # type: ignore[no-untyped-def]
    base = SnapshotTree(dict(_BASE))
    current = advance_accepted_tree(base, head_changes)
    builder = base.fork()
    for path, content in proposal.items():
        if content is None:
            del builder[path]
        else:
            builder[path] = content
    changed = frozenset(path for path in {*base, *current} if base.get(path) != current.get(path))
    return base, current, builder.snapshot(), changed


@pytest.mark.parametrize(
    ("head_changes", "proposal"), list(itertools.product(_HEAD_CHANGES, _PROPOSALS))
)
def test_the_delta_rebases_equal_the_whole_tree_rebases(head_changes, proposal) -> None:  # type: ignore[no-untyped-def]
    base, current, proposed, changed = _trees(head_changes, proposal)
    fork = _ForkRebase.of(
        base_tree=base, current_tree=current, proposed_tree=proposed, current_changed=changed
    )
    assert fork is not None
    assert fork.semantic_scope == semantic_diff(base, proposed)[1]
    whole_v1, conflicts_v1 = deterministic_rebase(
        base_tree=dict(base), current_tree=dict(current), proposed_tree=dict(proposed)
    )
    tree_v1, delta_conflicts_v1 = fork.rebase_v1()
    assert dict(tree_v1) == whole_v1 and delta_conflicts_v1 == conflicts_v1
    whole_v2 = deterministic_rebase_v2(
        old_parent_tree=dict(base), new_parent_tree=dict(current), proposed_tree=dict(proposed)
    )
    tree_v2, delta_conflicts_v2 = fork.rebase_v2()
    assert dict(tree_v2) == whole_v2.tree and delta_conflicts_v2 == whole_v2.conflicts
    # The rebased candidate is a fork of the head: evaluation advances from it.
    assert tree_v2.edits_from(current) is not None


def test_a_colliding_path_union_takes_the_whole_tree_rebase_and_its_refusal() -> None:
    base, current, proposed, changed = _trees(
        {"claims/New.json": b"head"}, {"claims/new.json": b"proposal"}
    )
    assert (
        _ForkRebase.of(
            base_tree=base, current_tree=current, proposed_tree=proposed, current_changed=changed
        )
        is None
    )
    with pytest.raises(CanonicalEncodingError):
        deterministic_rebase_v2(
            old_parent_tree=base, new_parent_tree=current, proposed_tree=proposed
        )


def test_only_a_fork_of_the_exact_base_with_a_known_head_diff_takes_the_delta() -> None:
    base, current, proposed, changed = _trees({"claims/a.json": b"h"}, {"claims/b.json": b"p"})
    for kwargs in (
        {"current_changed": None},
        {"proposed_tree": dict(proposed)},
        {"base_tree": SnapshotTree(dict(_BASE))},
        {"current_tree": dict(current)},
    ):
        arguments = {
            "base_tree": base,
            "current_tree": current,
            "proposed_tree": proposed,
            "current_changed": changed,
            **kwargs,
        }
        assert _ForkRebase.of(**arguments) is None  # type: ignore[arg-type]
