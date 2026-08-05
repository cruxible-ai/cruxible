"""Typed boundary-telemetry read models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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
