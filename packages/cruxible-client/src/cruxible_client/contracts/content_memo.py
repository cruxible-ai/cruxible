"""Bounded per-process memos for pure parses of immutable artifacts, keyed by content.

A governed artifact's parse is a pure function of its path, its codec and its
exact bytes. Within one write or one Line run the same few ClaimTypes,
CaptureContracts, Subjects and Claims are parsed dozens of times by different
laws and indexes. These memos make each of those a lookup:

- ``ParseMemo`` keys a parse by ``(path, codec, bytes)``: the content itself,
  never a caller's object. It keeps the parsed value pickled and hands every
  caller its own detached copy, so nothing a caller does to a returned model
  (frozen models are frozen only shallowly) can reach a later parse. A refused
  parse is never remembered, so it refuses again exactly as before.
- ``ValueMemo`` keys a pure function by a key made of immutable values only
  (strings, numbers, tuples of them), never by a model a caller holds.

Digests of parsed models are not memoized here: a model's nested containers
can change under its identity, and keying by its serialization costs what the
digest does. All memos are bounded and least-recently-used, and safe to share
across threads: every access is one short critical section.
"""

from __future__ import annotations

import pickle
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Generic, TypeVar

_V = TypeVar("_V")

_MISSING = object()


class ParseMemo(Generic[_V]):
    """``(path, codec, exact bytes) -> parsed artifact``, bounded by entries and bytes."""

    def __init__(self, *, capacity: int, max_bytes: int) -> None:
        self._entries: OrderedDict[tuple[str, object, bytes], bytes] = OrderedDict()
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._bytes = 0
        self._lock = threading.Lock()

    def parse(self, content: bytes, path: str, codec: object, parse: Callable[[], _V]) -> _V:
        if type(content) is not bytes or type(path) is not str:
            return parse()
        key = (path, codec, content)
        try:
            hash(key)
        except TypeError:
            return parse()
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
        if found is not None:
            return pickle.loads(found)  # type: ignore[no-any-return]
        value = parse()
        try:
            frozen = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except (pickle.PicklingError, TypeError, AttributeError):
            return value
        weight = len(content) + len(path) + len(frozen)
        if weight * 16 > self._max_bytes:
            return value
        with self._lock:
            if key not in self._entries:
                self._entries[key] = frozen
                self._bytes += weight
            while self._entries and (
                len(self._entries) > self._capacity or self._bytes > self._max_bytes
            ):
                (old_path, _codec, old_content), old = self._entries.popitem(last=False)
                self._bytes -= len(old_content) + len(old_path) + len(old)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


class ValueMemo(Generic[_V]):
    """A value derived from a key of immutable values (its equality is the content's)."""

    def __init__(self, *, capacity: int) -> None:
        self._entries: OrderedDict[Any, _V] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def derive(self, key: Any, derive: Callable[[], _V]) -> _V:
        try:
            hash(key)
        except TypeError:
            return derive()
        with self._lock:
            found = self._entries.get(key, _MISSING)
            if found is not _MISSING:
                self._entries.move_to_end(key)
                return found  # type: ignore[return-value]
        value = derive()
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = ["ParseMemo", "ValueMemo"]
