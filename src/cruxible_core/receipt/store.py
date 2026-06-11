"""SQLite backend for receipt and execution-trace persistence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cruxible_core.instance_protocol import ReceiptStoreProtocol
from cruxible_core.provider.trace_payloads import (
    DEFAULT_TRACE_PAYLOAD_INLINE_BYTES,
    TracePayloadMetadata,
    TracePayloadRetention,
    retain_payload,
)
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.types import Receipt
from cruxible_core.temporal import format_datetime

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    query_name TEXT NOT NULL,
    parameters TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    operation_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_query_name ON receipts(query_name);
CREATE INDEX IF NOT EXISTS idx_receipts_created_at ON receipts(created_at);
CREATE INDEX IF NOT EXISTS idx_receipts_operation_type ON receipts(operation_type);

CREATE TABLE IF NOT EXISTS receipt_entities (
    receipt_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (receipt_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_receipt_entities_lookup
ON receipt_entities(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS execution_traces (
    trace_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    step_id TEXT NOT NULL,
    provider_name TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    provider_ref TEXT NOT NULL,
    runtime TEXT NOT NULL,
    deterministic INTEGER NOT NULL,
    side_effects INTEGER NOT NULL,
    artifact_name TEXT,
    artifact_sha256 TEXT,
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_traces_workflow
ON execution_traces(workflow_name);
CREATE INDEX IF NOT EXISTS idx_execution_traces_provider
ON execution_traces(provider_name);
"""


class SQLiteReceiptStore(ReceiptStoreProtocol):
    """Stores and retrieves receipts and execution traces from SQLite."""

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        initialize_schema: bool = True,
        trace_payload_inline_bytes: int = DEFAULT_TRACE_PAYLOAD_INLINE_BYTES,
        trace_payload_retention: TracePayloadRetention = "preview",
    ) -> None:
        self._db_path = str(db_path)
        self._conn = connection if connection is not None else sqlite3.connect(self._db_path)
        self._owns_connection = connection is None
        self._trace_payload_inline_bytes = trace_payload_inline_bytes
        self._trace_payload_retention = trace_payload_retention
        self._conn.row_factory = sqlite3.Row
        if initialize_schema:
            self._conn.executescript(_SCHEMA)

    def save_receipt(self, receipt: Receipt) -> str:
        """Persist a receipt. Returns the receipt_id."""
        self._conn.execute(
            "INSERT OR REPLACE INTO receipts "
            "(receipt_id, query_name, parameters, receipt_json, created_at, duration_ms, "
            "operation_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.query_name,
                json.dumps(receipt.parameters),
                receipt.model_dump_json(),
                format_datetime(receipt.created_at),
                receipt.duration_ms,
                receipt.operation_type,
            ),
        )
        self._conn.execute(
            "DELETE FROM receipt_entities WHERE receipt_id = ?",
            (receipt.receipt_id,),
        )
        indexed = set()
        for node in receipt.nodes:
            if not node.entity_type or not node.entity_id:
                continue
            key = (receipt.receipt_id, node.entity_type, node.entity_id)
            if key in indexed:
                continue
            indexed.add(key)
            self._conn.execute(
                "INSERT OR REPLACE INTO receipt_entities (receipt_id, entity_type, entity_id) "
                "VALUES (?, ?, ?)",
                key,
            )
        return receipt.receipt_id

    def get_receipt(self, receipt_id: str) -> Receipt | None:
        """Load a receipt by ID. Returns None if not found."""
        row = self._conn.execute(
            "SELECT receipt_json FROM receipts WHERE receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            return None
        return Receipt.model_validate_json(row["receipt_json"])

    def save_trace(self, trace: ExecutionTrace) -> str:
        """Persist an execution trace. Returns the trace_id."""
        input_payload, input_metadata = self._retain_payload_field(
            trace.input_payload,
            trace.input_payload_metadata,
        )
        output_payload, output_metadata = self._retain_payload_field(
            trace.output_payload,
            trace.output_payload_metadata,
        )
        retained_trace = trace.model_copy(
            update={
                "input_payload": input_payload,
                "input_payload_metadata": input_metadata,
                "output_payload": output_payload,
                "output_payload_metadata": output_metadata,
            },
            deep=True,
        )
        self._insert_trace_row(retained_trace)
        trace.input_payload = input_payload
        trace.output_payload = output_payload
        trace.input_payload_metadata = input_metadata
        trace.output_payload_metadata = output_metadata
        return trace.trace_id

    def _insert_trace_row(self, trace: ExecutionTrace) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO execution_traces "
            "(trace_id, workflow_name, step_id, provider_name, provider_version, provider_ref, "
            "runtime, deterministic, side_effects, artifact_name, artifact_sha256, trace_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace.trace_id,
                trace.workflow_name,
                trace.step_id,
                trace.provider_name,
                trace.provider_version,
                trace.provider_ref,
                trace.runtime,
                int(trace.deterministic),
                int(trace.side_effects),
                trace.artifact_name,
                trace.artifact_digest,
                trace.model_dump_json(),
                format_datetime(trace.started_at),
            ),
        )

    def _retain_payload_field(
        self,
        payload: dict[str, Any],
        metadata: TracePayloadMetadata | None,
    ) -> tuple[dict[str, Any], TracePayloadMetadata]:
        if metadata is not None:
            return payload, metadata
        return retain_payload(
            payload,
            retention=self._trace_payload_retention,
            inline_byte_limit=self._trace_payload_inline_bytes,
        )

    def get_trace(self, trace_id: str) -> ExecutionTrace | None:
        """Load an execution trace by ID."""
        row = self._conn.execute(
            "SELECT trace_json FROM execution_traces WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        return ExecutionTrace.model_validate_json(row["trace_json"])

    def list_receipts(
        self,
        *,
        query_name: str | None = None,
        operation_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List receipt summaries, optionally filtered by query name or operation type."""
        conditions: list[str] = []
        params: list[Any] = []
        if query_name is not None:
            conditions.append("query_name = ?")
            params.append(query_name)
        if operation_type is not None:
            conditions.append("operation_type = ?")
            params.append(operation_type)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        rows = self._conn.execute(
            "SELECT receipt_id, query_name, parameters, created_at, duration_ms, "
            "operation_type "
            f"FROM receipts{where} ORDER BY created_at DESC, receipt_id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

        return [
            {
                "receipt_id": r["receipt_id"],
                "query_name": r["query_name"],
                "parameters": json.loads(r["parameters"]),
                "created_at": r["created_at"],
                "duration_ms": r["duration_ms"],
                "operation_type": r["operation_type"],
            }
            for r in rows
        ]

    def list_traces(
        self,
        *,
        workflow_name: str | None = None,
        provider_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List execution-trace summaries with optional workflow/provider filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if workflow_name is not None:
            conditions.append("workflow_name = ?")
            params.append(workflow_name)
        if provider_name is not None:
            conditions.append("provider_name = ?")
            params.append(provider_name)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        rows = self._conn.execute(
            "SELECT trace_id, workflow_name, step_id, provider_name, provider_version, "
            "runtime, created_at, trace_json "
            f"FROM execution_traces{where} ORDER BY created_at DESC, trace_id DESC "
            "LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        summaries: list[dict[str, Any]] = []
        for row in rows:
            trace = ExecutionTrace.model_validate_json(row["trace_json"])
            summary = {
                "trace_id": row["trace_id"],
                "workflow_name": row["workflow_name"],
                "step_id": row["step_id"],
                "provider_name": row["provider_name"],
                "provider_version": row["provider_version"],
                "runtime": row["runtime"],
                "created_at": row["created_at"],
            }
            if trace.input_payload_metadata is not None:
                summary["input_payload_metadata"] = trace.input_payload_metadata.model_dump(
                    mode="json",
                )
            if trace.output_payload_metadata is not None:
                summary["output_payload_metadata"] = trace.output_payload_metadata.model_dump(
                    mode="json",
                )
            summaries.append(summary)
        return summaries

    def count_traces(
        self,
        *,
        workflow_name: str | None = None,
        provider_name: str | None = None,
    ) -> int:
        """Count execution-trace records with optional workflow/provider filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if workflow_name is not None:
            conditions.append("workflow_name = ?")
            params.append(workflow_name)
        if provider_name is not None:
            conditions.append("provider_name = ?")
            params.append(provider_name)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM execution_traces{where}",
            params,
        ).fetchone()
        return int(row["count"]) if row else 0

    def count_receipts(
        self,
        *,
        query_name: str | None = None,
        operation_type: str | None = None,
    ) -> int:
        """Count receipt records with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []
        if query_name is not None:
            conditions.append("query_name = ?")
            params.append(query_name)
        if operation_type is not None:
            conditions.append("operation_type = ?")
            params.append(operation_type)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM receipts{where}",
            params,
        ).fetchone()
        return int(row["count"]) if row else 0

    def get_receipts_for_entity(self, entity_type: str, entity_id: str) -> list[str]:
        """List receipt IDs where the entity appears in receipt nodes."""
        rows = self._conn.execute(
            "SELECT re.receipt_id FROM receipt_entities re "
            "JOIN receipts r ON r.receipt_id = re.receipt_id "
            "WHERE re.entity_type = ? AND re.entity_id = ? "
            "ORDER BY r.created_at DESC",
            (entity_type, entity_id),
        ).fetchall()
        return [str(r["receipt_id"]) for r in rows]

    def delete_receipt(self, receipt_id: str) -> bool:
        """Delete a receipt. Returns True if it existed."""
        self._conn.execute(
            "DELETE FROM receipt_entities WHERE receipt_id = ?",
            (receipt_id,),
        )
        cursor = self._conn.execute(
            "DELETE FROM receipts WHERE receipt_id = ?",
            (receipt_id,),
        )
        return cursor.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        if self._owns_connection:
            self._conn.close()
