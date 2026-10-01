"""Keyed admission that waits on the event loop, not in a worker thread.

A route that must run one heavy call per key at a time (one floor export per
instance) awaits ``KeyedAdmission.admit(key)`` on the loop, then offloads the
call to the threadpool. Waiters hold no worker thread, so a queue behind one
slow call cannot exhaust the pool that cheap requests need.

Bookkeeping is exact: each key has one lock while anyone holds or waits for it,
and the entry is dropped when the last of them leaves, so idle keys cost
nothing. A contended ``asyncio.Lock`` binds its loop; entries are kept per
loop in a weak-keyed map and the loop's map is dropped with its last entry, so
nothing here keeps a finished loop alive.

Everything here runs on the event loop thread, so the bookkeeping needs no
lock of its own. The event-loop guardrail checks this module's ``admit`` call
by call; keep it to asyncio and dictionary bookkeeping.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class _Entry:
    __slots__ = ("lock", "users")

    def __init__(self, lock: asyncio.Lock) -> None:
        self.lock = lock
        self.users = 0


class KeyedAdmission:
    """One holder per key at a time, admitted on the running event loop."""

    def __init__(self) -> None:
        self._loops: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Entry]] = (
            weakref.WeakKeyDictionary()
        )

    @asynccontextmanager
    async def admit(self, key: str) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        entries = self._loops.get(loop)
        if entries is None:
            entries = {}
            self._loops[loop] = entries
        entry = entries.get(key)
        if entry is None:
            entry = _Entry(asyncio.Lock())
            entries[key] = entry
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del entries[key]
                if not entries:
                    del self._loops[loop]

    def active_keys(self) -> int:
        """Keys someone holds or waits for, across loops; for tests and status."""

        return sum(len(entries) for entries in list(self._loops.values()))

    def active_loops(self) -> int:
        return len(self._loops)


__all__ = ["KeyedAdmission"]
