"""Private ordered persistent maps for rebuildable state.

AVL path copying bounds lookup and update depth by O(log n), independent of
how many generations have been retained. Values are shared, not frozen: callers
must supply immutable values when they need immutable snapshots. String order
matches UTF-8 byte order for valid Unicode strings.
"""

from __future__ import annotations

from collections.abc import ItemsView, Iterable, Iterator, Mapping, MutableMapping, ValuesView
from copy import deepcopy
from dataclasses import dataclass
from typing import Generic, TypeVar

V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class _Node(Generic[V]):
    key: str
    value: V
    left: _Node[V] | None = None
    right: _Node[V] | None = None
    height: int = 1
    size: int = 1


def _height(node: _Node[V] | None) -> int:
    return node.height if node is not None else 0


def _size(node: _Node[V] | None) -> int:
    return node.size if node is not None else 0


def _node(key: str, value: V, left: _Node[V] | None, right: _Node[V] | None) -> _Node[V]:
    return _Node(
        key,
        value,
        left,
        right,
        1 + max(_height(left), _height(right)),
        1 + _size(left) + _size(right),
    )


def _rotate_left(node: _Node[V]) -> _Node[V]:
    right = node.right
    assert right is not None
    return _node(
        right.key, right.value, _node(node.key, node.value, node.left, right.left), right.right
    )


def _rotate_right(node: _Node[V]) -> _Node[V]:
    left = node.left
    assert left is not None
    return _node(
        left.key, left.value, left.left, _node(node.key, node.value, left.right, node.right)
    )


def _balance(node: _Node[V]) -> _Node[V]:
    skew = _height(node.left) - _height(node.right)
    if skew > 1:
        left = node.left
        assert left is not None
        if _height(left.left) < _height(left.right):
            node = _node(node.key, node.value, _rotate_left(left), node.right)
        return _rotate_right(node)
    if skew < -1:
        right = node.right
        assert right is not None
        if _height(right.right) < _height(right.left):
            node = _node(node.key, node.value, node.left, _rotate_right(right))
        return _rotate_left(node)
    return node


def _set(root: _Node[V] | None, key: str, value: V) -> _Node[V]:
    if root is None:
        return _Node(key, value)
    if key == root.key:
        return root if value is root.value else _node(key, value, root.left, root.right)
    if key < root.key:
        left = _set(root.left, key, value)
        return (
            root if left is root.left else _balance(_node(root.key, root.value, left, root.right))
        )
    right = _set(root.right, key, value)
    return root if right is root.right else _balance(_node(root.key, root.value, root.left, right))


def _delete(root: _Node[V] | None, key: str) -> _Node[V] | None:
    if root is None:
        return None
    if key < root.key:
        left = _delete(root.left, key)
        return (
            root if left is root.left else _balance(_node(root.key, root.value, left, root.right))
        )
    if key > root.key:
        right = _delete(root.right, key)
        return (
            root if right is root.right else _balance(_node(root.key, root.value, root.left, right))
        )
    if root.left is None:
        return root.right
    if root.right is None:
        return root.left
    successor = root.right
    while successor.left is not None:
        successor = successor.left
    return _balance(
        _node(successor.key, successor.value, root.left, _delete(root.right, successor.key))
    )


def _nodes(root: _Node[V] | None) -> Iterator[_Node[V]]:
    stack: list[_Node[V]] = []
    while stack or root is not None:
        while root is not None:
            stack.append(root)
            root = root.left
        root = stack.pop()
        yield root
        root = root.right


def _build(items: list[tuple[str, V]], start: int, end: int) -> _Node[V] | None:
    if start == end:
        return None
    middle = (start + end) // 2
    key, value = items[middle]
    return _node(key, value, _build(items, start, middle), _build(items, middle + 1, end))


@dataclass(frozen=True, slots=True, init=False, eq=False)
class PersistentMap(Mapping[str, V]):
    """Immutable map structure with O(log n) path-copying updates.

    Constructing from another PersistentMap shares its root in O(1). Other
    inputs are materialized once and sorted to build a balanced tree. Duplicate
    input keys take the final value, as with dict. Deepcopy copies values and
    preserves aliases; it does not assume generic values are immutable.
    """

    _root: _Node[V] | None

    def __init__(self, values: Mapping[str, V] | Iterable[tuple[str, V]] = ()) -> None:
        if isinstance(values, PersistentMap):
            root = values._root
        else:
            items = dict(values)
            if any(not isinstance(key, str) for key in items):
                raise TypeError("PersistentMap keys must be strings")
            ordered = sorted(items.items())
            root = _build(ordered, 0, len(ordered))
        object.__setattr__(self, "_root", root)

    @classmethod
    def _from_root(cls, root: _Node[V] | None) -> PersistentMap[V]:
        result = cls.__new__(cls)
        object.__setattr__(result, "_root", root)
        return result

    def __getitem__(self, key: str) -> V:
        if not isinstance(key, str):
            raise KeyError(key)
        node = self._root
        while node is not None:
            if key == node.key:
                return node.value
            node = node.left if key < node.key else node.right
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (node.key for node in _nodes(self._root))

    def __len__(self) -> int:
        return _size(self._root)

    def items(self) -> ItemsView[str, V]:
        return _ItemsView(self)

    def values(self) -> ValuesView[V]:
        return _ValuesView(self)

    def set(self, key: str, value: V) -> PersistentMap[V]:
        if not isinstance(key, str):
            raise TypeError("PersistentMap keys must be strings")
        root = _set(self._root, key, value)
        return self if root is self._root else self._from_root(root)

    def delete(self, key: str) -> PersistentMap[V]:
        if not isinstance(key, str):
            return self
        root = _delete(self._root, key)
        return self if root is self._root else self._from_root(root)

    def evolve(self, updated: Mapping[str, V], removed: Iterable[str] = ()) -> PersistentMap[V]:
        """Remove keys, then apply updates; updates win if a key appears in both."""
        result = self
        for key in removed:
            result = result.delete(key)
        for key, value in updated.items():
            result = result.set(key, value)
        return result

    def __deepcopy__(self, memo: dict[int, object]) -> PersistentMap[V]:
        result: PersistentMap[V] = self._from_root(None)
        memo[id(self)] = result
        items = [(key, deepcopy(value, memo)) for key, value in self.items()]
        object.__setattr__(result, "_root", _build(items, 0, len(items)))
        return result


class _ItemsView(ItemsView[str, V]):
    def __init__(self, mapping: PersistentMap[V]) -> None:
        super().__init__(mapping)
        self._persistent_mapping = mapping

    def __iter__(self) -> Iterator[tuple[str, V]]:
        return ((node.key, node.value) for node in _nodes(self._persistent_mapping._root))


class _ValuesView(ValuesView[V]):
    def __init__(self, mapping: PersistentMap[V]) -> None:
        super().__init__(mapping)
        self._persistent_mapping = mapping

    def __iter__(self) -> Iterator[V]:
        return (node.value for node in _nodes(self._persistent_mapping._root))


class MapMutation(MutableMapping[str, V]):
    """Mutable cursor over a persistent root; finish returns an independent snapshot."""

    def __init__(self, values: Mapping[str, V] | Iterable[tuple[str, V]] = ()) -> None:
        self._map = PersistentMap(values)

    def __getitem__(self, key: str) -> V:
        return self._map[key]

    def __setitem__(self, key: str, value: V) -> None:
        self._map = self._map.set(key, value)

    def __delitem__(self, key: str) -> None:
        updated = self._map.delete(key)
        if updated is self._map:
            raise KeyError(key)
        self._map = updated

    def __iter__(self) -> Iterator[str]:
        return iter(self._map)

    def __len__(self) -> int:
        return len(self._map)

    def finish(self) -> PersistentMap[V]:
        return self._map
