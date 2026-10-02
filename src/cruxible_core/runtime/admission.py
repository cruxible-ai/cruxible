"""Per-key exclusion shared by the event loop and worker threads.

A heavy call that must run one at a time per key (one floor render per
instance) takes the key first. Two kinds of caller take it:

- an ``async def`` HTTP route awaits ``admit(key)`` on the event loop, then
  offloads the call to the threadpool. A route that has to wait parks on a
  future of its own loop, so a queue behind one slow call holds no worker
  thread and cannot exhaust the pool cheap requests need;
- a worker thread (a daemon consumer refreshing a floor in-process) enters
  ``hold(key)``, which blocks that thread until the key is its own. It refuses
  to run on a thread with a running event loop, which it would stall.

Both kinds queue on the same key in arrival order and exclude each other: an
export, a delta and a consumer refresh of one instance never overlap, while
different keys proceed independently. Key by the resolved instance id.

``self._lock`` guards the bookkeeping only. It is held for a few dictionary and
deque operations and the hand-off itself (``Event.set`` or
``call_soon_threadsafe``), never across a wait, so taking it on the loop never
waits behind a holder of a key. The holder releasing a key hands it directly to
the first waiter, so nobody can overtake the queue. Bookkeeping is exact: a key
has an entry only while someone holds or waits for it. A waiter refers to its
loop only while queued; a cancelled one is withdrawn, or, if the key reached it
in the same instant, passes the key on, and a waiter whose loop has closed is
skipped. So nothing here keeps a finished loop alive.

The event-loop guardrail (``tests/test_architecture/test_routes_off_the_event_loop.py``)
checks every call ``admit`` makes on the loop, through each method it reaches;
keep that path to this bookkeeping.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager


def _grant(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class _Waiter:
    """One queued caller: a task parked on a future, or a thread on an event."""

    __slots__ = ("event", "future", "granted", "loop")

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None,
        future: asyncio.Future[None] | None,
        event: threading.Event | None,
    ) -> None:
        self.loop = loop
        self.future = future
        self.event = event
        self.granted = False

    def wake(self) -> bool:
        """Hand this waiter the key; False when nobody is left to take it."""

        if self.event is not None:
            self.event.set()
            return True
        if self.loop is None or self.future is None:
            return False
        try:
            self.loop.call_soon_threadsafe(_grant, self.future)
        except RuntimeError:
            # Its loop is closed, so its task will never run again.
            return False
        return True


class _Entry:
    """A held key and the callers queued for it, first come first served."""

    __slots__ = ("waiters",)

    def __init__(self) -> None:
        self.waiters: deque[_Waiter] = deque()


class KeyedAdmission:
    """One holder per key at a time, across the event loop and worker threads."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    # -- bookkeeping; every method runs with self._lock held only briefly ----------

    def _enter(
        self,
        key: str,
        loop: asyncio.AbstractEventLoop | None,
        future: asyncio.Future[None] | None,
        event: threading.Event | None,
    ) -> _Waiter | None:
        """Take ``key`` now (``None``), or queue for it and return the waiter."""

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry()
                return None
            waiter = _Waiter(loop, future, event)
            entry.waiters.append(waiter)
            return waiter

    def _pass_on(self, key: str) -> None:
        """Hand a released ``key`` to its first live waiter; drop it when none is left.

        Called with ``self._lock`` held.
        """

        entry = self._entries[key]
        while entry.waiters:
            waiter = entry.waiters.popleft()
            waiter.granted = waiter.wake()
            if waiter.granted:
                return
        del self._entries[key]

    def _leave(self, key: str) -> None:
        with self._lock:
            self._pass_on(key)

    def _withdraw(self, key: str, waiter: _Waiter) -> None:
        """A waiter gives up: leave the queue, or pass on a key it was just handed."""

        with self._lock:
            if waiter.granted:
                self._pass_on(key)
                return
            entry = self._entries[key]
            entry.waiters.remove(waiter)

    # -- the two ways in -----------------------------------------------------------

    @asynccontextmanager
    async def admit(self, key: str) -> AsyncIterator[None]:
        """Hold ``key`` for an async route; waiting parks on the loop, not a worker."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        waiter = self._enter(key, loop, future, None)
        if waiter is not None:
            try:
                await future
            except BaseException:
                self._withdraw(key, waiter)
                raise
        try:
            yield
        finally:
            self._leave(key)

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """Hold ``key`` from a worker thread, blocking it until the key is its own."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("KeyedAdmission.hold would block a running event loop; use admit")
        event = threading.Event()
        waiter = self._enter(key, None, None, event)
        if waiter is not None:
            try:
                event.wait()
            except BaseException:
                self._withdraw(key, waiter)
                raise
        try:
            yield
        finally:
            self._leave(key)

    # -- status --------------------------------------------------------------------

    def active_keys(self) -> int:
        """Keys someone holds or waits for; for tests and status."""

        with self._lock:
            return len(self._entries)

    def active_loops(self) -> int:
        """Distinct event loops with a task queued here; for tests and status."""

        with self._lock:
            return len(
                {
                    id(waiter.loop)
                    for entry in self._entries.values()
                    for waiter in entry.waiters
                    if waiter.loop is not None
                }
            )


#: The one floor render admission per instance, keyed by the resolved instance
#: id: HTTP floor export and delta routes ``admit``, and in-process floor
#: delivery on a consumer worker thread ``hold``s, so no two floor renders of an
#: instance ever overlap.
FLOOR_ADMISSION = KeyedAdmission()


__all__ = ["FLOOR_ADMISSION", "KeyedAdmission"]
