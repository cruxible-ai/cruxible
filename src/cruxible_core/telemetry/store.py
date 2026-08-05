"""SQLite persistence for aggregate boundary telemetry."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from cruxible_core.telemetry.types import BoundaryCounter, BoundaryTelemetrySummary
from cruxible_core.temporal import format_datetime, parse_datetime, utc_now

BOUNDARY_TELEMETRY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS boundary_telemetry (
    surface_name TEXT PRIMARY KEY,
    call_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    total_response_bytes INTEGER NOT NULL,
    total_duration_ms REAL NOT NULL,
    max_duration_ms REAL NOT NULL,
    first_recorded_at TEXT NOT NULL
);
"""


class SQLiteTelemetryStore:
    """Aggregated counters stored in an instance's authoritative state DB."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row

    def record(
        self,
        surface_name: str,
        *,
        response_bytes: int,
        duration_ms: float,
        error: bool,
    ) -> None:
        """Atomically add one observation to its aggregate row."""
        recorded_at = format_datetime(utc_now())
        self._conn.execute(
            "INSERT INTO boundary_telemetry "
            "(surface_name, call_count, error_count, total_response_bytes, "
            "total_duration_ms, max_duration_ms, first_recorded_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?) "
            "ON CONFLICT(surface_name) DO UPDATE SET "
            "call_count = call_count + 1, "
            "error_count = error_count + excluded.error_count, "
            "total_response_bytes = total_response_bytes + excluded.total_response_bytes, "
            "total_duration_ms = total_duration_ms + excluded.total_duration_ms, "
            "max_duration_ms = MAX(max_duration_ms, excluded.max_duration_ms)",
            (
                surface_name,
                int(error),
                max(0, response_bytes),
                max(0.0, duration_ms),
                max(0.0, duration_ms),
                recorded_at,
            ),
        )

    def summary(self) -> BoundaryTelemetrySummary:
        """Return all counters ordered by surface name."""
        rows = self._conn.execute(
            "SELECT surface_name, call_count, error_count, total_response_bytes, "
            "total_duration_ms, max_duration_ms, first_recorded_at "
            "FROM boundary_telemetry ORDER BY surface_name"
        ).fetchall()
        earliest = min((str(row["first_recorded_at"]) for row in rows), default=None)
        return BoundaryTelemetrySummary(
            earliest_recorded_at=parse_datetime(earliest),
            counters=[
                BoundaryCounter(
                    surface_name=str(row["surface_name"]),
                    call_count=int(row["call_count"]),
                    error_count=int(row["error_count"]),
                    total_response_bytes=int(row["total_response_bytes"]),
                    total_duration_ms=float(row["total_duration_ms"]),
                    max_duration_ms=float(row["max_duration_ms"]),
                )
                for row in rows
            ],
        )

    @classmethod
    def record_best_effort(
        cls,
        db_path: str | Path,
        surface_name: str,
        *,
        response_bytes: int,
        duration_ms: float,
        error: bool,
    ) -> None:
        """Record without waiting for a busy DB or propagating telemetry failures.

        The state schema is initialized on ordinary instance access. This path
        deliberately does not initialize or migrate it: doing so could wait on
        the migration lock after the underlying request has already completed.
        """
        connection: sqlite3.Connection | None = None
        resolved_db_path = Path(db_path)
        if not resolved_db_path.is_file():
            return
        try:
            connection = sqlite3.connect(str(resolved_db_path), timeout=0)
            connection.execute("PRAGMA busy_timeout = 0")
            cls(connection).record(
                surface_name,
                response_bytes=response_bytes,
                duration_ms=duration_ms,
                error=error,
            )
            connection.commit()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
