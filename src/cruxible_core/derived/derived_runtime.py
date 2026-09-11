"""Local lifecycle and resource bounds for explicitly registered derived state.

This registry owns disposable resources only. It neither verifies source state
nor turns an adapter registration into an incremental-rebuild guarantee. Byte
counts are caller estimates of retained inputs, not measurements of Python RSS.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from cruxible_client._error_base import CoreError

V = TypeVar("V")


class BuildCapacityError(CoreError):
    """The bounded queue cannot admit another synchronous derived-state build."""

    error_code = "playbill.derived.capacity"


class BoundedCache(Generic[V]):
    """Thread-safe LRU with incremental byte accounting and explicit invalidation."""

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[V, int]] = OrderedDict()
        self._bytes = 0
        self._generation = 0
        self._hits = 0
        self._evictions = 0
        self.configure(max_entries=max_entries, max_bytes=max_bytes)

    def configure(self, *, max_entries: int, max_bytes: int) -> None:
        if max_entries < 0 or max_bytes < 0:
            raise ValueError("cache budgets must be nonnegative")
        with self._lock:
            self._max_entries = max_entries
            self._max_bytes = max_bytes
            self._trim()

    def _trim(self) -> None:
        while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
            _, (_, weight) = self._entries.popitem(last=False)
            self._bytes -= weight
            self._evictions += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def get(self, key: Hashable) -> V | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry[0]

    def put(
        self,
        key: Hashable,
        value: V,
        *,
        weight: int,
        expected_generation: int | None = None,
    ) -> bool:
        if weight < 0:
            raise ValueError("cache weight must be nonnegative")
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                return False
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            if self._max_entries == 0 or weight > self._max_bytes:
                return False
            self._entries[key] = (value, weight)
            self._bytes += weight
            self._trim()
            return True

    def pop(self, key: Hashable) -> V | None:
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is None:
                return None
            self._bytes -= previous[1]
            return previous[0]

    def values(self) -> tuple[V, ...]:
        with self._lock:
            return tuple(value for value, _ in self._entries.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "estimated_bytes": self._bytes,
                "max_entries": self._max_entries,
                "max_bytes": self._max_bytes,
                "generation": self._generation,
                "hits": self._hits,
                "evictions": self._evictions,
            }


@dataclass(frozen=True, slots=True)
class Registration:
    name: str
    namespace: str
    version: str
    adapter: object
    clear: Callable[[], None] | None = None


class Lease(Generic[V]):
    """Explicit retained-root handle. Closing releases accounting once, without traversal."""

    def __init__(self, root: V, release: Callable[[], None]) -> None:
        self._root: V | None = root
        self._release = release
        self._closed = False
        self._lock = threading.Lock()

    @property
    def root(self) -> V:
        with self._lock:
            if self._closed:
                raise RuntimeError("derived-state lease is closed")
            return self._root  # type: ignore[return-value]

    def __enter__(self) -> V:
        return self.root

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._root = None
        self._release()


class Registry:
    """Instance-owned registrations, memos, build admission and explicit leases."""

    def __init__(self, *, max_builds: int = 2, max_pending: int = 32) -> None:
        if max_builds < 1 or max_pending < 0:
            raise ValueError("build concurrency must be positive and pending budget nonnegative")
        self._condition = threading.Condition()
        self._max_builds = max_builds
        self._max_pending = max_pending
        self._registrations: dict[str, Registration] = {}
        self._memos: dict[str, BoundedCache[Any]] = {}
        self._active: dict[Hashable, int] = {}
        self._pending: deque[tuple[object, Hashable]] = deque()
        self._builds = 0
        self._failed_builds = 0
        self._refused_builds = 0
        self._leases = 0
        self._lease_bytes = 0

    def register(
        self,
        name: str,
        namespace: str,
        version: str,
        adapter: object,
        clear: Callable[[], None] | None = None,
    ) -> object:
        with self._condition:
            if name in self._registrations:
                raise ValueError("derived index is already registered")
            self._registrations[name] = Registration(name, namespace, version, adapter, clear)
        return adapter

    def memo(self, name: str, *, max_entries: int, max_bytes: int) -> BoundedCache[Any]:
        with self._condition:
            memo = self._memos.get(name)
            if memo is None:
                memo = BoundedCache(max_entries=max_entries, max_bytes=max_bytes)
                self._memos[name] = memo
            else:
                memo.configure(max_entries=max_entries, max_bytes=max_bytes)
            return memo

    def _can_start(self, ticket: object, key: Hashable) -> bool:
        if len(self._active) >= self._max_builds or key in self._active:
            return False
        # The oldest runnable waiter wins. A duplicate of an active key cannot
        # consume every free slot by blocking unrelated work behind it.
        return next((token for token, k in self._pending if k not in self._active), None) is ticket

    @contextmanager
    def build(self, key: Hashable) -> Iterator[None]:
        """Bound builds; duplicate keys serialize and callers recheck their cache inside.

        The body runs without a registry lock. A full waiting queue raises an
        explicit capacity refusal; no unbounded condition/event table is created.
        """
        ticket = object()
        thread = threading.get_ident()
        with self._condition:
            if thread in self._active.values():
                raise RuntimeError("nested builds on one registry are not supported")
            if (
                not any(pending_key not in self._active for _, pending_key in self._pending)
                and key not in self._active
                and len(self._active) < self._max_builds
            ):
                self._active[key] = thread
            else:
                if len(self._pending) >= self._max_pending:
                    self._refused_builds += 1
                    raise BuildCapacityError("derived-state pending build budget exhausted")
                self._pending.append((ticket, key))
                self._condition.notify_all()
                try:
                    while not self._can_start(ticket, key):
                        self._condition.wait()
                    self._active[key] = thread
                finally:
                    self._pending.remove((ticket, key))
                    self._condition.notify_all()
            self._builds += 1
        try:
            yield
        except BaseException:
            with self._condition:
                self._failed_builds += 1
            raise
        finally:
            with self._condition:
                del self._active[key]
                self._condition.notify_all()

    def lease(self, root: V, *, estimated_bytes: int = 0) -> Lease[V]:
        if estimated_bytes < 0:
            raise ValueError("lease estimate must be nonnegative")
        with self._condition:
            self._leases += 1
            self._lease_bytes += estimated_bytes

        def release() -> None:
            with self._condition:
                self._leases -= 1
                self._lease_bytes -= estimated_bytes

        return Lease(root, release)

    def clear(self, name: str | None = None) -> None:
        with self._condition:
            registrations = tuple(
                entry for key, entry in self._registrations.items() if name is None or name == key
            )
            memos = tuple(memo for key, memo in self._memos.items() if name is None or name == key)
        for memo in memos:
            memo.clear()
        for registration in registrations:
            if registration.clear is not None:
                registration.clear()

    def status(self) -> dict[str, object]:
        with self._condition:
            result: dict[str, object] = {
                "registrations": tuple(
                    {"name": row.name, "namespace": row.namespace, "version": row.version}
                    for row in self._registrations.values()
                ),
                "active_builds": len(self._active),
                "pending_builds": len(self._pending),
                "max_builds": self._max_builds,
                "max_pending": self._max_pending,
                "builds_started": self._builds,
                "failed_builds": self._failed_builds,
                "refused_builds": self._refused_builds,
                "active_leases": self._leases,
                "leased_estimated_bytes": self._lease_bytes,
            }
            memos = tuple(self._memos.items())
        result["memos"] = {name: memo.status() for name, memo in memos}
        return result
