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
    """All boundary counters for one instance."""

    earliest_recorded_at: datetime | None
    counters: list[BoundaryCounter] = field(default_factory=list)
