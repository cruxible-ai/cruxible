"""Typed boundary-telemetry read models and the in-memory accumulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BoundaryAggregate:
    """One surface's observations accumulated in memory between flushes.

    Deliberately mutable and unfrozen: this is the accumulator merged on the
    request path, where allocating a replacement per observation is precisely
    the cost the in-memory buffer exists to avoid. ``first_recorded_at`` is
    stamped by the observation that created the aggregate, so a flushed row
    dates from when the traffic happened, not from when it reached SQLite.
    """

    first_recorded_at: str
    call_count: int = 0
    error_count: int = 0
    total_response_bytes: int = 0
    total_duration_ms: float = 0.0
    max_duration_ms: float = 0.0

    def absorb(self, other: BoundaryAggregate) -> None:
        """Fold another accumulator for the SAME surface into this one.

        Used when an undelivered batch is merged back into pending and the
        surface has accumulated again since the flush drained it. The folded
        result must be indistinguishable from never having attempted the write,
        so ``first_recorded_at`` takes the EARLIER stamp: the re-merged batch
        predates whatever accumulated behind it.
        """
        self.call_count += other.call_count
        self.error_count += other.error_count
        self.total_response_bytes += other.total_response_bytes
        self.total_duration_ms += other.total_duration_ms
        self.max_duration_ms = max(self.max_duration_ms, other.max_duration_ms)
        self.first_recorded_at = min(self.first_recorded_at, other.first_recorded_at)


@dataclass(frozen=True)
class BoundaryCounter:
    """One aggregate row for a named core surface."""

    surface_name: str
    call_count: int
    error_count: int
    total_response_bytes: int
    total_duration_ms: float
    max_duration_ms: float


@dataclass(frozen=True)
class BoundaryTelemetrySummary:
    """All boundary counters for one instance, plus what they are missing.

    The counters are best-effort by design: capture never blocks or fails a
    call, so a surface cap, an undeliverable batch, or a command that outran the
    CLI collector costs counters instead. The drop totals are what makes that
    cost READABLE — without them an undercount is indistinguishable from quiet
    traffic, and a reader would trust a number that had silently lost calls.
    Both are cumulative for the instance and never reset.
    """

    earliest_recorded_at: datetime | None
    counters: list[BoundaryCounter] = field(default_factory=list)
    dropped_observations: int = 0
    dropped_events: int = 0
