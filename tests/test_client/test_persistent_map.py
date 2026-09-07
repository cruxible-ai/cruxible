"""Persistent root isolation, AVL depth, and mapping behavior."""

from __future__ import annotations

import math
import random
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from cruxible_client._persistent import MapMutation, PersistentMap, _Node


def _assert_tree(node: _Node[int] | None) -> tuple[int, int, list[str]]:
    if node is None:
        return 0, 0, []
    left_height, left_size, left_keys = _assert_tree(node.left)
    right_height, right_size, right_keys = _assert_tree(node.right)
    assert abs(left_height - right_height) <= 1
    assert node.height == 1 + max(left_height, right_height)
    assert node.size == left_size + right_size + 1
    assert all(key < node.key for key in left_keys)
    assert all(key > node.key for key in right_keys)
    return node.height, node.size, [*left_keys, node.key, *right_keys]


def _node_ids(node: _Node[int] | None) -> set[int]:
    if node is None:
        return set()
    return {id(node)} | _node_ids(node.left) | _node_ids(node.right)


def test_random_updates_and_deletions_preserve_all_snapshots_and_avl_invariants() -> None:
    rng = random.Random(2817)
    reference: dict[str, int] = {}
    root: PersistentMap[int] = PersistentMap()
    snapshots = []
    for step in range(1000):
        key = f"key-{rng.randrange(160):04}"
        if rng.random() < 0.4:
            reference.pop(key, None)
            root = root.delete(key)
        else:
            reference[key] = step
            root = root.set(key, step)
        assert dict(root.items()) == reference
        assert list(root) == sorted(reference)
        height, size, keys = _assert_tree(root._root)
        assert size == len(root)
        assert keys == list(root)
        assert height <= 1.45 * math.log2(size + 2)
        if step % 100 == 0:
            snapshots.append((root, dict(reference)))
    for snapshot, expected in snapshots:
        assert snapshot == expected
        _assert_tree(snapshot._root)


def test_one_update_copies_only_a_bounded_path_and_shares_rest() -> None:
    before = PersistentMap({f"{i:05}": i for i in range(4096)})
    after = before.set("02000", -1)
    before_ids, after_ids = _node_ids(before._root), _node_ids(after._root)
    assert len(after_ids - before_ids) <= before._root.height
    assert before["02000"] == 2000
    assert after["02000"] == -1
    assert PersistentMap(before)._root is before._root
    assert before.delete("absent") is before
    assert before.set("02000", before["02000"]) is before
    with pytest.raises(FrozenInstanceError):
        before._root = None  # type: ignore[misc]


def test_sorted_insertions_and_deletions_never_form_overlay_chains() -> None:
    root: PersistentMap[int] = PersistentMap()
    for i in range(1024):
        root = root.set(f"{i:04}", i)
    _assert_tree(root._root)
    for i in range(1024):
        root = root.delete(f"{i:04}")
        if i % 100 == 0:
            _assert_tree(root._root)
    assert len(root) == 0
    assert root._root is None


def test_mutation_finish_is_a_snapshot_and_mapping_methods_work() -> None:
    original = PersistentMap({"a": 1, "b": 2})
    mutation = MapMutation(original)
    assert mutation.finish()._root is original._root
    mutation["a"] = 3
    first = mutation.finish()
    mutation.update({"c": 4})
    assert mutation.pop("b") == 2
    with pytest.raises(KeyError):
        del mutation["missing"]
    assert dict(first) == {"a": 3, "b": 2}
    assert dict(original) == {"a": 1, "b": 2}
    assert dict(mutation.finish()) == {"a": 3, "c": 4}
    assert dict(first.evolve({"b": 9}, removed=["a", "b"])) == {"b": 9}


def test_unicode_order_duplicates_and_missing_keys() -> None:
    keys = ["😀", "é", "中", "a", "", "\ue000", "𐀀"]
    root = PersistentMap((key, i) for i, key in enumerate(keys + ["a"]))
    assert list(root) == sorted(set(keys), key=lambda key: key.encode("utf-8"))
    assert list(root.items()) == [(key, root[key]) for key in root]
    assert list(root.values()) == [root[key] for key in root]
    assert root["a"] == len(keys)
    with pytest.raises(KeyError):
        root["absent"]
    with pytest.raises(TypeError):
        PersistentMap({1: "bad"})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        root.set(1, 2)  # type: ignore[arg-type]


def test_deepcopy_copies_mutable_values_and_preserves_aliases_and_cycles() -> None:
    shared: list[object] = []
    original = PersistentMap({"a": shared, "b": shared})
    shared.append(original)
    copied = deepcopy(original)
    assert copied is not original
    assert copied["a"] is copied["b"]
    assert copied["a"] is not shared
    assert copied["a"][0] is copied
    copied["a"].append("new")
    assert len(shared) == 1
