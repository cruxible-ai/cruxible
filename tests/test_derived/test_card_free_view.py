"""Card-free views and fork comparisons equal their whole-tree oracles."""

from __future__ import annotations

import random

import pytest

from cruxible_core.derived.derived_state import (
    BlobRef,
    SnapshotTree,
    advance_accepted_tree,
    card_free_edits,
    card_free_view,
    changed_paths,
    without_cards,
)


def _oracle_without_cards(tree) -> dict[str, bytes]:
    return {path: tree[path] for path in tree if not path.startswith("cards/")}


def _root(generator: random.Random) -> SnapshotTree:
    rows: dict[str, bytes | BlobRef] = {}
    for index in range(generator.randint(0, 60)):
        namespace = generator.choice(["claims", "cards/claims", "subjects", "cards", "changesets"])
        rows[f"{namespace}/{index:03d}.json"] = f"{index}".encode() * generator.randint(1, 3)
    return SnapshotTree(rows)


def _edit(generator: random.Random, tree) -> None:
    for _ in range(generator.randint(0, 8)):
        paths = list(tree)
        if paths and generator.random() < 0.4:
            del tree[generator.choice(paths)]
        else:
            namespace = generator.choice(["claims", "cards/claims", "subjects"])
            tree[f"{namespace}/{generator.randint(0, 80):03d}.json"] = generator.randbytes(3)


def _weights(tree: SnapshotTree) -> tuple[int, int, int, int]:
    oracle = SnapshotTree(dict(tree.items()))
    assert (tree._input_bytes, tree._resident_bytes) == (
        oracle._input_bytes,
        oracle._resident_bytes,
    )
    return (
        tree._input_bytes,
        tree._resident_bytes,
        tree._semantic_bytes,
        tree._semantic_members,
    )


@pytest.mark.parametrize("seed", range(25))
def test_views_forks_and_carried_views_equal_the_oracle(seed: int) -> None:
    generator = random.Random(seed)
    root = _root(generator)
    view = card_free_view(root)
    assert dict(view.items()) == _oracle_without_cards(root)
    _weights(view)
    assert card_free_view(root) is view

    fork = root.fork()
    _edit(generator, fork)
    stripped = without_cards(fork.snapshot())
    assert dict(stripped.items()) == _oracle_without_cards(fork)
    assert card_free_edits(stripped, root) is not None
    assert card_free_edits(fork, root) is None

    # An accepted successor carries its view forward by the same delta.
    successor = advance_accepted_tree(
        root, {path: fork._rows.get(path) for path in changed_paths(root, fork)}
    )
    assert successor._card_free is not None
    assert dict(successor._card_free.items()) == _oracle_without_cards(successor)
    _weights(successor._card_free)


@pytest.mark.parametrize("seed", range(25))
def test_fork_equality_and_changed_paths_equal_the_whole_tree_oracle(seed: int) -> None:
    generator = random.Random(seed)
    root = _root(generator)
    left, right = root.fork(), root.fork()
    _edit(generator, left)
    if generator.random() < 0.5:
        for path in list(right):
            if path not in left:
                del right[path]
        for path in left:
            right[path] = left[path]
    else:
        _edit(generator, right)
    oracle_left, oracle_right = dict(left.items()), dict(right.items())
    assert (left.snapshot() == right.snapshot()) == (oracle_left == oracle_right)
    assert (root == left) == (dict(root.items()) == oracle_left)
    expected = {
        path
        for path in oracle_left.keys() | oracle_right.keys()
        if oracle_left.get(path) != oracle_right.get(path)
    }
    assert set(changed_paths(left, right)) == expected
    assert set(changed_paths(left, dict(oracle_right))) == expected
