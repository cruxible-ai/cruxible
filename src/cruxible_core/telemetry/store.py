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

CREATE TABLE IF NOT EXISTS boundary_telemetry_drops (
    drop_kind TEXT PRIMARY KEY,
    dropped_count INTEGER NOT NULL
);
"""

# Observations the buffer never got to keep (surface cap, or a batch that could
# not be re-merged), and events a CLI command produced faster than the collector
# would hold them. Two kinds because they are lost at different stages and mean
# different things to a reader: the first says counters are incomplete, the
# second says one command's verb list is.
OBSERVATION_DROP_KIND = "observations"
EVENT_DROP_KIND = "events"

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
    "max_duration_ms = MAX(max_duration_ms, excluded.max_duration_ms), "
    # Batches do NOT arrive in stamp order: a batch the store refused is
    # re-merged and lands after batches that were accepted while it waited, and
    # a read flushes its own instance ahead of the periodic flusher. Keeping the
    # stored stamp unless the incoming one is earlier makes the row report when
    # the surface was FIRST seen rather than which flush happened to win.
    #
    # Lexicographic MIN is the right comparison here: every stamp is written by
    # BoundaryTelemetryBuffer as an ensure_utc().isoformat() string, so all share
    # one UTC offset and one field layout. Optional microseconds are the only
    # width variation, and they compare correctly anyway -- position 19 is '+'
    # without them and '.' with, and '+' sorts before '.', so the whole second
    # sorts before any fraction of it.
    "first_recorded_at = MIN(first_recorded_at, excluded.first_recorded_at)"
)

_DROP_MERGE_STATEMENT = (
    "INSERT INTO boundary_telemetry_drops (drop_kind, dropped_count) VALUES (?, ?) "
    "ON CONFLICT(drop_kind) DO UPDATE SET "
    "dropped_count = dropped_count + excluded.dropped_count"
)


class SQLiteTelemetryStore:
    """Aggregated counters stored in an instance's authoritative state DB."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row

    def merge(
        self,
        aggregates: Mapping[str, BoundaryAggregate],
        *,
        dropped_observations: int = 0,
        dropped_events: int = 0,
    ) -> None:
        """Fold one buffered batch, and what it lost, into the rows they belong to.

        Counters accumulate in place, so replaying a batch is a single
        ``executemany`` regardless of how many calls it represents. The drop
        deltas ride the same transaction as the counters they are missing from,
        so a summary can never show the counters without the caveat.
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
        drops = [
            (kind, count)
            for kind, count in (
                (OBSERVATION_DROP_KIND, dropped_observations),
                (EVENT_DROP_KIND, dropped_events),
            )
            if count > 0
        ]
        if drops:
            self._conn.executemany(_DROP_MERGE_STATEMENT, drops)

    def summary(self) -> BoundaryTelemetrySummary:
        """Return all counters ordered by surface name, with their drop totals."""
        rows = self._conn.execute(
            "SELECT surface_name, call_count, error_count, total_response_bytes, "
            "total_duration_ms, max_duration_ms, first_recorded_at "
            "FROM boundary_telemetry ORDER BY surface_name"
        ).fetchall()
        drop_rows = self._conn.execute(
            "SELECT drop_kind, dropped_count FROM boundary_telemetry_drops"
        ).fetchall()
        drops = {str(row["drop_kind"]): int(row["dropped_count"]) for row in drop_rows}
        earliest = min((str(row["first_recorded_at"]) for row in rows), default=None)
        return BoundaryTelemetrySummary(
            earliest_recorded_at=parse_datetime(earliest),
            dropped_observations=drops.get(OBSERVATION_DROP_KIND, 0),
            dropped_events=drops.get(EVENT_DROP_KIND, 0),
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
        *,
        dropped_observations: int = 0,
        dropped_events: int = 0,
    ) -> bool:
        """Write one batch, refusing it rather than waiting or raising.

        Returns True when the batch is durable and False when the caller still
        owns it. It never raises and never waits: called only off the request
        path (the flusher thread, or a read that wants its own instance
        current), ``timeout=0`` plus ``busy_timeout = 0`` means a concurrent
        writer makes this fail IMMEDIATELY instead of queueing behind real work.

        Returning a verdict rather than swallowing the failure is what keeps a
        moment of contention from costing counters. This used to return None and
        the caller had already cleared its pending batch, so any transient lock
        silently undercounted; the caller now merges a refused batch back in.

        NO-SCHEMA-INIT: the state schema is created on ordinary instance access.
        This path deliberately does not initialize or migrate it, because doing
        so could take the migration lock from a background thread while the
        request path is using the DB. So on a state DB no one has opened
        normally yet, ``boundary_telemetry`` does not exist, this refuses the
        batch, and the caller holds it until a read or an ordinary open has
        created the schema. Every read path calls ``_ensure_state_initialized()``
        first, so the table exists by the time any caller can observe counters.
        """
        if not aggregates and not dropped_observations and not dropped_events:
            return True
        connection: sqlite3.Connection | None = None
        resolved_db_path = Path(db_path)
        if not resolved_db_path.is_file():
            return False
        try:
            connection = sqlite3.connect(str(resolved_db_path), timeout=0)
            connection.execute("PRAGMA busy_timeout = 0")
            cls(connection).merge(
                aggregates,
                dropped_observations=dropped_observations,
                dropped_events=dropped_events,
            )
            connection.commit()
            return True
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return False
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
