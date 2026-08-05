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

Failure stays fail-open at every step: a drained batch that cannot be written
(busy DB, missing file, uninitialized schema) is dropped, not retried and not
raised, and a full buffer drops new surfaces rather than growing without bound.
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
        self.dropped_observations = 0

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

    def flush(self) -> None:
        """Drain and write. The lock covers the swap only, never the write."""
        with self._lock:
            if not self._pending:
                return
            pending = self._pending
            self._pending = {}
        SQLiteTelemetryStore.merge_best_effort(self._db_path, pending)


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
