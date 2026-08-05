"""SQLite persistence for aggregate boundary telemetry.

This store DELIBERATELY does not join the UnitOfWork, and is exempted from the
store-registration checklist in ``tests/test_guardrails/test_store_registration.py``
for that reason: fail-open telemetry must never join or block the transaction
that carries the request's real work. Its only writer is the off-request
flusher in ``cruxible_core.telemetry.buffer``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

from cruxible_core.telemetry.types import (
    BoundaryAggregate,
    BoundaryCounter,
    BoundaryTelemetrySummary,
)
from cruxible_core.temporal import parse_datetime

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

_MERGE_STATEMENT = (
    "INSERT INTO boundary_telemetry "
    "(surface_name, call_count, error_count, total_response_bytes, "
    "total_duration_ms, max_duration_ms, first_recorded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(surface_name) DO UPDATE SET "
    "call_count = call_count + excluded.call_count, "
    "error_count = error_count + excluded.error_count, "
    "total_response_bytes = total_response_bytes + excluded.total_response_bytes, "
    "total_duration_ms = total_duration_ms + excluded.total_duration_ms, "
    "max_duration_ms = MAX(max_duration_ms, excluded.max_duration_ms)"
)


class SQLiteTelemetryStore:
    """Aggregated counters stored in an instance's authoritative state DB."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row

    def merge(self, aggregates: Mapping[str, BoundaryAggregate]) -> None:
        """Fold one buffered batch into the aggregate rows it belongs to.

        Counters accumulate in place, so replaying a batch is a single
        ``executemany`` regardless of how many calls it represents.
        """
        self._conn.executemany(
            _MERGE_STATEMENT,
            [
                (
                    surface_name,
                    aggregate.call_count,
                    aggregate.error_count,
                    aggregate.total_response_bytes,
                    aggregate.total_duration_ms,
                    aggregate.max_duration_ms,
                    aggregate.first_recorded_at,
                )
                for surface_name, aggregate in aggregates.items()
            ],
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
    def merge_best_effort(
        cls,
        db_path: str | Path,
        aggregates: Mapping[str, BoundaryAggregate],
    ) -> None:
        """Write one batch, dropping it rather than waiting or raising.

        Called only off the request path (the flusher thread, or a read that
        wants its own instance current), so a drop costs counters and nothing
        else. ``timeout=0`` plus ``busy_timeout = 0`` means a concurrent writer
        makes this fail immediately instead of queueing behind real work.

        NO-SCHEMA-INIT, AND WHY THE FIRST BATCH CAN DROP: the state schema is
        created on ordinary instance access. This path deliberately does not
        initialize or migrate it, because doing so could take the migration
        lock from a background thread while the request path is using the DB.
        The deliberate consequence is that on a state DB no one has opened
        normally yet, ``boundary_telemetry`` does not exist and this batch is
        dropped. Every read path calls ``_ensure_state_initialized()`` first,
        so the table exists by the time any caller can observe the counters.
        """
        if not aggregates:
            return
        connection: sqlite3.Connection | None = None
        resolved_db_path = Path(db_path)
        if not resolved_db_path.is_file():
            return
        try:
            connection = sqlite3.connect(str(resolved_db_path), timeout=0)
            connection.execute("PRAGMA busy_timeout = 0")
            cls(connection).merge(aggregates)
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
