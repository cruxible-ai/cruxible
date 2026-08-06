"""In-memory aggregation of boundary observations, flushed off the request path.

Every boundary observation is taken where the work happens: the asyncio event
loop for HTTP and MCP, the foreground for the CLI. Opening SQLite there cost
roughly a millisecond per request — around 10% of a small request — because
each observation paid connect + PRAGMA + INSERT + commit + close on the loop.

So nothing on that path touches storage. ``BoundaryTelemetryBuffer.add`` merges
into a per-instance dict under a lock held for that dict update alone and never
across I/O, exactly as the SQLite counters would have aggregated in place. One
process-wide daemon thread drains every buffer on an interval, and a read
flushes its own instance first so ``telemetry summary`` never lags the calls it
is summarizing.

Failure stays fail-open at every step: nothing here raises, waits on a lock held
across I/O, or lets a storage problem reach the caller. A drained batch the store
refuses (busy DB, missing file, uninitialized schema) is merged BACK into pending
and retried on the next flush rather than lost, and a full buffer drops new
surfaces rather than growing without bound. What genuinely cannot be kept is
counted, and those counts are published in the summary, so an undercount is
visible to a reader instead of silent.
"""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path

from cruxible_core.telemetry.store import SQLiteTelemetryStore
from cruxible_core.telemetry.types import BoundaryAggregate
from cruxible_core.temporal import ensure_utc, utc_now

# Surfaces are route names, MCP tool names, and CLI verbs — a set fixed by the
# code, not by callers. The cap only guards against a future caller minting
# surface names from request data; hitting it is a bug, so drops are counted.
MAX_BUFFERED_SURFACES = 512

# How long an observation can sit in memory before it is durable. Short enough
# that a killed daemon loses a couple of seconds of counters, long enough that
# a busy instance writes its state DB a few times a minute instead of per call.
FLUSH_INTERVAL_SECONDS = 2.0


class BoundaryTelemetryBuffer:
    """Pending observations for one instance's state DB."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._pending: dict[str, BoundaryAggregate] = {}
        # Undelivered drop deltas, drained by ``flush`` exactly like the
        # aggregates are. They are counts of what this buffer could not keep,
        # so they must reach the summary or the undercount stays invisible.
        self.dropped_observations = 0
        self.dropped_events = 0

    def add(
        self,
        surface_name: str,
        *,
        response_bytes: int,
        duration_ms: float,
        error: bool,
    ) -> None:
        """Merge one observation in memory. No I/O, no waiting, no raising."""
        clamped_bytes = max(0, response_bytes)
        clamped_ms = max(0.0, duration_ms)
        with self._lock:
            aggregate = self._pending.get(surface_name)
            if aggregate is None:
                if len(self._pending) >= MAX_BUFFERED_SURFACES:
                    self.dropped_observations += 1
                    return
                # Stamped only when the aggregate is created — the steady-state
                # observation is a handful of integer adds and nothing else.
                # ``format_datetime`` is the optional-input variant and would
                # widen this to None, which the summary's earliest-recorded
                # reading is derived from and must never be.
                aggregate = BoundaryAggregate(first_recorded_at=ensure_utc(utc_now()).isoformat())
                self._pending[surface_name] = aggregate
            aggregate.call_count += 1
            aggregate.error_count += 1 if error else 0
            aggregate.total_response_bytes += clamped_bytes
            aggregate.total_duration_ms += clamped_ms
            aggregate.max_duration_ms = max(aggregate.max_duration_ms, clamped_ms)

    def add_dropped_events(self, count: int) -> None:
        """Count events lost before they could ever become observations.

        The CLI collector caps how many service verbs one command may hold; past
        that the events never reach ``add`` at all. Counting them here is what
        puts them in the same summary as the calls they are missing from.
        """
        if count <= 0:
            return
        with self._lock:
            self.dropped_events += count

    def flush(self) -> None:
        """Drain and write. The lock covers the swap only, never the write.

        A batch the store refuses is merged BACK into pending, so a busy DB
        costs a flush interval rather than counters. Only what will not fit
        under ``MAX_BUFFERED_SURFACES`` on the way back is truly lost, and that
        is counted into ``dropped_observations`` for the summary to publish.
        """
        with self._lock:
            dropped_observations = self.dropped_observations
            dropped_events = self.dropped_events
            if not self._pending and not dropped_observations and not dropped_events:
                return
            pending = self._pending
            self._pending = {}
            self.dropped_observations = 0
            self.dropped_events = 0
        delivered = False
        try:
            delivered = SQLiteTelemetryStore.merge_best_effort(
                self._db_path,
                pending,
                dropped_observations=dropped_observations,
                dropped_events=dropped_events,
            )
        finally:
            # ``finally``, not ``except``: the store is contracted never to
            # raise, so a raise is a bug — and a bug must not be the one path
            # that loses the batch this whole method exists to keep. The
            # exception still propagates to the caller, which is where the
            # existing fail-open guards are.
            if not delivered:
                self._restore(pending, dropped_observations, dropped_events)

    def _restore(
        self,
        batch: dict[str, BoundaryAggregate],
        dropped_observations: int,
        dropped_events: int,
    ) -> None:
        """Fold an undelivered batch back into pending, within the surface cap.

        Observations kept accumulating while the write was in flight, so a
        surface may be pending again; ``absorb`` folds the two accumulators and
        keeps the earlier ``first_recorded_at``. The cap is on distinct
        surfaces, not calls, and it still binds here — a buffer that could not
        write must not grow without bound just because the write failed — so a
        returning surface with nowhere to go counts every call it carried as
        dropped rather than being silently discarded.
        """
        with self._lock:
            self.dropped_observations += dropped_observations
            self.dropped_events += dropped_events
            for surface_name, aggregate in batch.items():
                current = self._pending.get(surface_name)
                if current is not None:
                    current.absorb(aggregate)
                    continue
                if len(self._pending) >= MAX_BUFFERED_SURFACES:
                    self.dropped_observations += aggregate.call_count
                    continue
                self._pending[surface_name] = aggregate


# One buffer per instance state DB. The number of instances a process serves is
# what bounds this map: the HTTP capture resolves through the governed registry
# and the MCP/CLI captures through the instance manager, so no caller-supplied
# string can mint an entry.
_buffers: dict[str, BoundaryTelemetryBuffer] = {}
_registry_lock = threading.Lock()
_flusher_thread: threading.Thread | None = None
_flusher_stop = threading.Event()


def telemetry_buffer(db_path: str | Path) -> BoundaryTelemetryBuffer:
    """Return the buffer for a state DB, starting the flusher on first use."""
    key = str(db_path)
    with _registry_lock:
        buffer = _buffers.get(key)
        if buffer is None:
            buffer = BoundaryTelemetryBuffer(key)
            _buffers[key] = buffer
        _start_flusher_locked()
    return buffer


def flush_all() -> None:
    """Write every buffer's pending batch, absorbing per-buffer failures."""
    with _registry_lock:
        buffers = list(_buffers.values())
    for buffer in buffers:
        try:
            buffer.flush()
        except Exception:
            pass


def reset_boundary_telemetry_buffers() -> None:
    """Drop all buffered state without writing it. For test isolation."""
    with _registry_lock:
        _buffers.clear()


def _flush_loop() -> None:
    while not _flusher_stop.wait(FLUSH_INTERVAL_SECONDS):
        flush_all()


def _start_flusher_locked() -> None:
    """Start the single flusher thread. Caller holds ``_registry_lock``."""
    global _flusher_thread
    if _flusher_thread is not None and _flusher_thread.is_alive():
        return
    _flusher_stop.clear()
    _flusher_thread = threading.Thread(
        target=_flush_loop,
        name="cruxible-telemetry-flush",
        daemon=True,
    )
    _flusher_thread.start()


def _reset_after_fork() -> None:
    """Rebuild thread state in a forked child.

    A child does not inherit the flusher thread, and could inherit a lock the
    parent held at fork time. Both are rebuilt here; the child re-registers its
    own buffers on its first observation.
    """
    global _flusher_thread, _registry_lock, _buffers
    _registry_lock = threading.Lock()
    _flusher_thread = None
    _buffers = {}


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX only
    os.register_at_fork(after_in_child=_reset_after_fork)

atexit.register(flush_all)


__all__ = [
    "FLUSH_INTERVAL_SECONDS",
    "MAX_BUFFERED_SURFACES",
    "BoundaryTelemetryBuffer",
    "flush_all",
    "reset_boundary_telemetry_buffers",
    "telemetry_buffer",
]
