"""Persistent maps and sets: an update copies the path it changes, never the whole collection.

Derived state that advances per generation (the floor index, sources/LEDGER)
keeps every prior version intact for concurrent readers and delta bases. A
plain ``dict`` copy per version costs the whole population on every change;
these share all unchanged structure, so a version that changes ``k`` keys
costs ``O(k log n)``.

``PMap`` is a hash array mapped trie (32-way, bitmap-indexed). An
``evolver()`` batches updates and mutates only the nodes it created itself;
``persistent()`` freezes them, so earlier versions never change.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Set
from typing import Any, Generic, TypeVar, cast

K = TypeVar("K")
V = TypeVar("V")
T = TypeVar("T")

_BITS = 5
_MASK = (1 << _BITS) - 1
_HASH_BITS = 64
_HASH_MASK = (1 << _HASH_BITS) - 1
_MISSING: Any = object()


def _hash(key: object) -> int:
    return hash(key) & _HASH_MASK


class _Leaf:
    __slots__ = ("hash", "key", "value")

    def __init__(self, hash_value: int, key: Any, value: Any) -> None:
        self.hash = hash_value
        self.key = key
        self.value = value


class _Collision:
    """Entries whose full hashes are equal, as a short list."""

    __slots__ = ("edit", "hash", "leaves")

    def __init__(self, edit: object, hash_value: int, leaves: list[_Leaf]) -> None:
        self.edit = edit
        self.hash = hash_value
        self.leaves = leaves


class _Node:
    __slots__ = ("array", "bitmap", "edit")

    def __init__(self, edit: object, bitmap: int, array: list[Any]) -> None:
        self.edit = edit
        self.bitmap = bitmap
        self.array = array


_Entry = _Node | _Collision | _Leaf
_EMPTY = _Node(object(), 0, [])


def _get(node: _Entry, hash_value: int, shift: int, key: Any) -> Any:
    while True:
        if isinstance(node, _Leaf):
            return node.value if node.key == key else _MISSING
        if isinstance(node, _Collision):
            for leaf in node.leaves:
                if leaf.key == key:
                    return leaf.value
            return _MISSING
        bit = 1 << ((hash_value >> shift) & _MASK)
        if not node.bitmap & bit:
            return _MISSING
        node = node.array[(node.bitmap & (bit - 1)).bit_count()]
        shift += _BITS


def _merge(edit: object, shift: int, first: _Entry, second: _Leaf) -> _Entry:
    """A node holding ``first`` (a leaf or collision) and the new leaf ``second``."""

    first_hash = first.hash  # type: ignore[union-attr]
    if shift >= _HASH_BITS or first_hash == second.hash:
        if isinstance(first, _Collision):
            return _Collision(edit, first_hash, [*first.leaves, second])
        assert isinstance(first, _Leaf)
        return _Collision(edit, first_hash, [first, second])
    a = (first_hash >> shift) & _MASK
    b = (second.hash >> shift) & _MASK
    if a == b:
        return _Node(edit, 1 << a, [_merge(edit, shift + _BITS, first, second)])
    if a < b:
        return _Node(edit, (1 << a) | (1 << b), [first, second])
    return _Node(edit, (1 << a) | (1 << b), [second, first])


def _assoc(edit: object, node: _Entry, shift: int, leaf: _Leaf) -> tuple[_Entry, bool]:
    """``node`` with ``leaf`` set; the flag says whether the key is new."""

    if isinstance(node, _Leaf):
        if node.key == leaf.key:
            return leaf, False
        return _merge(edit, shift, node, leaf), True
    if isinstance(node, _Collision):
        if node.hash != leaf.hash:
            return _merge(edit, shift, node, leaf), True
        leaves = node.leaves if node.edit is edit else list(node.leaves)
        for index, held in enumerate(leaves):
            if held.key == leaf.key:
                leaves[index] = leaf
                return (node if node.edit is edit else _Collision(edit, node.hash, leaves)), False
        leaves.append(leaf)
        return (node if node.edit is edit else _Collision(edit, node.hash, leaves)), True
    bit = 1 << ((leaf.hash >> shift) & _MASK)
    index = (node.bitmap & (bit - 1)).bit_count()
    owned = node.edit is edit
    if not node.bitmap & bit:
        if owned:
            node.array.insert(index, leaf)
            node.bitmap |= bit
            return node, True
        array = list(node.array)
        array.insert(index, leaf)
        return _Node(edit, node.bitmap | bit, array), True
    child, added = _assoc(edit, node.array[index], shift + _BITS, leaf)
    if owned:
        node.array[index] = child
        return node, added
    if child is node.array[index]:
        return node, added
    array = list(node.array)
    array[index] = child
    return _Node(edit, node.bitmap, array), added


def _dissoc(
    edit: object, node: _Entry, shift: int, hash_value: int, key: Any
) -> tuple[_Entry | None, bool]:
    """``node`` without ``key`` (None when nothing is left); the flag says whether it was there."""

    if isinstance(node, _Leaf):
        return (None, True) if node.key == key else (node, False)
    if isinstance(node, _Collision):
        remaining = [leaf for leaf in node.leaves if leaf.key != key]
        if len(remaining) == len(node.leaves):
            return node, False
        if len(remaining) == 1:
            return remaining[0], True
        return _Collision(edit, node.hash, remaining), True
    bit = 1 << ((hash_value >> shift) & _MASK)
    if not node.bitmap & bit:
        return node, False
    index = (node.bitmap & (bit - 1)).bit_count()
    child, removed = _dissoc(edit, node.array[index], shift + _BITS, hash_value, key)
    if not removed:
        return node, False
    owned = node.edit is edit
    if child is None:
        if len(node.array) == 1:
            return None, True
        if len(node.array) == 2 and shift > 0:
            other = node.array[1 - index]
            if not isinstance(other, _Node):
                # A lone leaf or collision moves up to its parent's slot.
                return other, True
        array = node.array if owned else list(node.array)
        del array[index]
        bitmap = node.bitmap & ~bit
        if owned:
            node.bitmap = bitmap
            return node, True
        return _Node(edit, bitmap, array), True
    if len(node.array) == 1 and shift > 0 and not isinstance(child, _Node):
        return child, True
    if owned:
        node.array[index] = child
        return node, True
    array = list(node.array)
    array[index] = child
    return _Node(edit, node.bitmap, array), True


def _leaves(node: _Entry) -> Iterator[_Leaf]:
    if isinstance(node, _Leaf):
        yield node
    elif isinstance(node, _Collision):
        yield from node.leaves
    else:
        for entry in node.array:
            if isinstance(entry, _Leaf):
                yield entry
            else:
                yield from _leaves(entry)


class PMap(Mapping[K, V], Generic[K, V]):
    """An immutable mapping that shares structure between versions."""

    __slots__ = ("_length", "_root")

    def __init__(self, items: Mapping[K, V] | Iterable[tuple[K, V]] | None = None) -> None:
        self._root: _Entry = _EMPTY
        self._length = 0
        if items:
            evolver = PMapEvolver(self)
            pairs = items.items() if isinstance(items, Mapping) else items
            for key, value in pairs:
                evolver[key] = value
            self._root = evolver._root
            self._length = evolver._length

    @classmethod
    def _of(cls, root: _Entry, length: int) -> PMap[K, V]:
        made: PMap[K, V] = cls.__new__(cls)
        made._root = root
        made._length = length
        return made

    def __getitem__(self, key: K) -> V:
        value = _get(self._root, _hash(key), 0, key)
        if value is _MISSING:
            raise KeyError(key)
        return cast(V, value)

    def get(self, key: K, default: Any = None) -> Any:
        value = _get(self._root, _hash(key), 0, key)
        return default if value is _MISSING else value

    def __contains__(self, key: object) -> bool:
        return _get(self._root, _hash(key), 0, key) is not _MISSING

    def __iter__(self) -> Iterator[K]:
        return (leaf.key for leaf in _leaves(self._root))

    def __len__(self) -> int:
        return self._length

    def items(self) -> Any:
        return _Items(self)

    def values(self) -> Any:
        return _Values(self)

    def evolver(self) -> PMapEvolver[K, V]:
        return PMapEvolver(self)

    def set(self, key: K, value: V) -> PMap[K, V]:
        evolver = self.evolver()
        evolver[key] = value
        return evolver.persistent()

    def discard(self, key: K) -> PMap[K, V]:
        evolver = self.evolver()
        evolver.discard(key)
        return evolver.persistent()

    def __repr__(self) -> str:
        return f"PMap({dict(self.items())!r})"


class _Items:
    __slots__ = ("_map",)

    def __init__(self, pmap: PMap[Any, Any]) -> None:
        self._map = pmap

    def __iter__(self) -> Iterator[tuple[Any, Any]]:
        return ((leaf.key, leaf.value) for leaf in _leaves(self._map._root))

    def __len__(self) -> int:
        return len(self._map)


class _Values:
    __slots__ = ("_map",)

    def __init__(self, pmap: PMap[Any, Any]) -> None:
        self._map = pmap

    def __iter__(self) -> Iterator[Any]:
        return (leaf.value for leaf in _leaves(self._map._root))

    def __len__(self) -> int:
        return len(self._map)


class PMapEvolver(Generic[K, V]):
    """A batch of updates to one ``PMap``; ``persistent()`` returns the new version."""

    __slots__ = ("_edit", "_length", "_root")

    def __init__(self, pmap: PMap[K, V]) -> None:
        self._root: _Entry = pmap._root
        self._length = len(pmap)
        self._edit = object()

    def __setitem__(self, key: K, value: V) -> None:
        root, added = _assoc(self._edit, self._root, 0, _Leaf(_hash(key), key, value))
        self._root = root
        self._length += int(added)

    def discard(self, key: K) -> None:
        root, removed = _dissoc(self._edit, self._root, 0, _hash(key), key)
        if removed:
            self._root = _EMPTY if root is None else root
            self._length -= 1

    def pop(self, key: K, default: Any = _MISSING) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            if default is _MISSING:
                raise KeyError(key)
            return default
        self.discard(key)
        return value

    def get(self, key: K, default: Any = None) -> Any:
        value = _get(self._root, _hash(key), 0, key)
        return default if value is _MISSING else value

    def __getitem__(self, key: K) -> V:
        value = _get(self._root, _hash(key), 0, key)
        if value is _MISSING:
            raise KeyError(key)
        return cast(V, value)

    def __contains__(self, key: object) -> bool:
        return _get(self._root, _hash(key), 0, key) is not _MISSING

    def __len__(self) -> int:
        return self._length

    def persistent(self) -> PMap[K, V]:
        """The new version; later updates through this evolver copy, never mutate it."""

        self._edit = object()
        # Updates at the root never collapse it: it stays a node.
        assert isinstance(self._root, _Node)
        return PMap._of(self._root, self._length)


class PSet(Set[T], Generic[T]):
    """An immutable set that shares structure between versions."""

    __slots__ = ("_map",)

    def __init__(self, items: Iterable[T] | None = None) -> None:
        self._map: PMap[T, bool] = PMap((item, True) for item in items) if items else PMap()

    @classmethod
    def _wrap(cls, pmap: PMap[T, bool]) -> PSet[T]:
        made: PSet[T] = cls.__new__(cls)
        made._map = pmap
        return made

    @classmethod
    def _from_iterable(cls, items: Iterable[Any]) -> PSet[Any]:
        return cls(items)

    def __contains__(self, item: object) -> bool:
        return item in self._map

    def __iter__(self) -> Iterator[T]:
        return iter(self._map)

    def __len__(self) -> int:
        return len(self._map)

    def add(self, item: T) -> PSet[T]:
        return self if item in self._map else PSet._wrap(self._map.set(item, True))

    def remove(self, item: T) -> PSet[T]:
        return PSet._wrap(self._map.discard(item)) if item in self._map else self

    def update(self, added: Iterable[T] = (), removed: Iterable[T] = ()) -> PSet[T]:
        evolver = self._map.evolver()
        for item in removed:
            evolver.discard(item)
        for item in added:
            evolver[item] = True
        return PSet._wrap(evolver.persistent())

    def __hash__(self) -> int:
        return self._hash()

    def __repr__(self) -> str:
        return f"PSet({set(self)!r})"


EMPTY_PSET: PSet[Any] = PSet()


_WIDTH = 1 << _BITS


def _counted(node: Any, level: int, key: int, delta: int) -> Any:
    """``node`` with ``delta`` added to ``key``'s count: copies one node per level."""

    index = (key >> (_BITS * level)) & _MASK
    if level == 0:
        counts = [0] * _WIDTH if node is None else list(node)
        counts[index] += delta
        if counts[index] < 0:
            raise ValueError(f"PCounts has no {key} to remove")
        return tuple(counts) if any(counts) else None
    children = [None] * _WIDTH if node is None else list(node)
    children[index] = _counted(children[index], level - 1, key, delta)
    return tuple(children) if any(child is not None for child in children) else None


class PCounts:
    """An immutable multiset of non-negative ints that knows its maximum.

    A 32-way trie on the key's bits, most significant first, so the maximum
    is the rightmost occupied path. ``add`` and ``remove`` copy one node per
    level and ``max`` walks one path: each is bounded by the key's width, not
    by how many entries there are.
    """

    __slots__ = ("_levels", "_root", "_size")

    def __init__(self, items: Iterable[int] = ()) -> None:
        self._root: Any = None
        self._levels = 1
        self._size = 0
        for item in items:
            self._root, self._levels = self._grown(item)
            self._root = _counted(self._root, self._levels - 1, item, 1)
            self._size += 1

    def _grown(self, key: int) -> tuple[Any, int]:
        if key < 0:
            raise ValueError("PCounts holds non-negative ints")
        root, levels = self._root, self._levels
        while key >> (_BITS * levels):
            root = None if root is None else (root, *([None] * (_WIDTH - 1)))
            levels += 1
        return root, levels

    def _with(self, key: int, delta: int) -> PCounts:
        root, levels = self._grown(key)
        made: PCounts = PCounts.__new__(PCounts)
        made._root = _counted(root, levels - 1, key, delta)
        made._levels = levels
        made._size = self._size + delta
        return made

    def add(self, key: int) -> PCounts:
        return self._with(key, 1)

    def remove(self, key: int) -> PCounts:
        """One fewer ``key``; raises ``ValueError`` when there is none."""

        return self._with(key, -1)

    def count(self, key: int) -> int:
        if key < 0 or key >> (_BITS * self._levels):
            return 0
        node = self._root
        for level in range(self._levels - 1, -1, -1):
            if node is None:
                return 0
            node = node[(key >> (_BITS * level)) & _MASK]
        return int(node or 0)

    def max(self, default: int = 0) -> int:
        node = self._root
        if node is None:
            return default
        key = 0
        for level in range(self._levels - 1, -1, -1):
            index = next(i for i in range(_WIDTH - 1, -1, -1) if node[i])
            key |= index << (_BITS * level)
            node = node[index]
        return key

    def __len__(self) -> int:
        return self._size

    def __bool__(self) -> bool:
        return self._size > 0

    def __repr__(self) -> str:
        return f"PCounts(size={self._size}, max={self.max()})"


EMPTY_PCOUNTS = PCounts()

__all__ = ["EMPTY_PCOUNTS", "EMPTY_PSET", "PCounts", "PMap", "PMapEvolver", "PSet"]
