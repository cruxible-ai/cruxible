"""Per-key exclusion shared by the event loop and worker threads.

A heavy call that must run one at a time per key (one floor render per
instance) takes the key first. Two kinds of caller take it:

- an ``async def`` HTTP route awaits ``admit(key)`` on the event loop, then
  offloads the call to the threadpool through the ticket admission yields:
  ``await run_in_threadpool(ticket.run, call)``. A route that has to wait parks
  on a future of its own loop, so a queue behind one slow call holds no worker
  thread and cannot exhaust the pool cheap requests need. Once the worker has
  started the call, the key is the worker's: it is released when the call
  ends, not when the route leaves ``admit``, so a route cancelled mid-call
  (a client gone, a task cancelled) never lets the next holder in beside a
  render still running;
- a worker thread (a daemon consumer refreshing a floor in-process) enters
  ``hold(key)``, which blocks that thread until the key is its own. It refuses
  to run on a thread with a running event loop, which it would stall.

HTTP request paths must never reach ``hold``, even from a synchronous route
or an offloaded helper: waiting there consumes worker capacity before admission
and can deadlock against an admitted route waiting for that capacity. Only
non-request threads use ``hold``; HTTP paths admit before offloading and call
bodies that do not acquire the key again. The server marks every HTTP request
with ``HTTP_REQUEST_CONTEXT``, which propagates into offloaded workers. ``hold``
refuses that context before acquiring or waiting for any key; this runtime check
is the guarantee across all call indirections. Ticket workers keep the request
context and run normally because they have already been admitted. Consumer
threads have no request context.

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
keep that path to this bookkeeping. Its transitive hold scan is a cheap early
warning smoke check; the request-context check in ``hold`` guarantees the HTTP
prohibition at runtime.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Literal, TypeVar

_T = TypeVar("_T")

#: Set by the server for HTTP requests, including their offloaded worker calls.
HTTP_REQUEST_CONTEXT: ContextVar[bool] = ContextVar("cruxible_http_request", default=False)


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


class _Ticket:
    """A key admitted on the loop, which the worker running the call takes over.

    ``held``: the route holds it. ``worker``: a worker started the call and
    releases the key when the call ends. ``closed``: the route left before any
    worker started, and released it.
    """

    __slots__ = ("admission", "key", "state")

    def __init__(self, admission: KeyedAdmission, key: str) -> None:
        self.admission = admission
        self.key = key
        self.state: Literal["held", "worker", "closed"] = "held"

    def run(self, call: Callable[[], _T]) -> _T:
        """Run ``call`` holding the key; the key is released when it ends.

        Runs in the worker thread. A route that already left admission (it was
        cancelled before the worker got here) has released the key, and the
        call is not made.
        """

        if not self.admission._claim(self):
            raise RuntimeError("the admitted caller left before its call started")
        try:
            return call()
        finally:
            self.admission._leave(self.key)


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

    def _claim(self, ticket: _Ticket) -> bool:
        """A worker takes over an admitted key, unless its route already let it go."""

        with self._lock:
            if ticket.state != "held":
                return False
            ticket.state = "worker"
            return True

    def _close(self, ticket: _Ticket) -> None:
        """A route leaves admission: release the key unless a worker has it."""

        with self._lock:
            if ticket.state == "held":
                ticket.state = "closed"
                self._pass_on(ticket.key)

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
    async def admit(self, key: str) -> AsyncIterator[_Ticket]:
        """Hold ``key`` for an async route; waiting parks on the loop, not a worker.

        Offload the call with ``run_in_threadpool(ticket.run, call)``: the key
        then stays held until the call ends, even if the route is cancelled.
        """

        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        waiter = self._enter(key, loop, future, None)
        if waiter is not None:
            try:
                await future
            except BaseException:
                self._withdraw(key, waiter)
                raise
        ticket = _Ticket(self, key)
        try:
            yield ticket
        finally:
            self._close(ticket)

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """Hold ``key`` from a worker thread, blocking it until the key is its own."""

        if HTTP_REQUEST_CONTEXT.get():
            raise RuntimeError(
                "floor admission from an HTTP request must use async admit with the ticket"
            )
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


__all__ = ["FLOOR_ADMISSION", "HTTP_REQUEST_CONTEXT", "KeyedAdmission"]
