"""Receipt and provider trace persistence for workflow execution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.trace_payloads import (
    DEFAULT_TRACE_PAYLOAD_INLINE_BYTES,
    TracePayloadRetention,
    retain_payload,
)
from cruxible_core.provider.types import ExecutionTrace, ProviderRuntime
from cruxible_core.receipt.types import Receipt
from cruxible_core.temporal import utc_now


def persist_receipt(instance: InstanceProtocol, receipt: Receipt) -> None:
    with instance.write_transaction() as uow:
        uow.receipts.save_receipt(receipt)


def persist_trace(instance: InstanceProtocol, trace: ExecutionTrace) -> ExecutionTrace:
    with instance.write_transaction() as uow:
        uow.receipts.save_trace(trace)
    return trace


def apply_trace_payload_retention(
    trace: ExecutionTrace,
    *,
    retention: TracePayloadRetention = "preview",
    inline_byte_limit: int = DEFAULT_TRACE_PAYLOAD_INLINE_BYTES,
) -> ExecutionTrace:
    """Return a trace whose payload fields follow the configured retention policy."""
    updates: dict[str, Any] = {}
    if trace.input_payload_metadata is None:
        input_payload, input_metadata = retain_payload(
            trace.input_payload,
            retention=retention,
            inline_byte_limit=inline_byte_limit,
        )
        updates["input_payload"] = input_payload
        updates["input_payload_metadata"] = input_metadata
    if trace.output_payload_metadata is None:
        output_payload, output_metadata = retain_payload(
            trace.output_payload,
            retention=retention,
            inline_byte_limit=inline_byte_limit,
        )
        updates["output_payload"] = output_payload
        updates["output_payload_metadata"] = output_metadata
    if not updates:
        return trace
    return trace.model_copy(update=updates, deep=True)


def build_trace(
    *,
    workflow_name: str,
    step_id: str,
    provider_name: str,
    provider_version: str,
    provider_ref: str,
    provider_entrypoint_digest: str | None,
    runtime: ProviderRuntime,
    deterministic: bool,
    side_effects: bool,
    artifact_name: str | None,
    artifact_digest: str | None,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    status: Literal["success", "error"],
    error: str | None,
    started_at: datetime,
    duration_ms: float,
) -> ExecutionTrace:
    return ExecutionTrace(
        workflow_name=workflow_name,
        step_id=step_id,
        provider_name=provider_name,
        provider_version=provider_version,
        provider_ref=provider_ref,
        provider_entrypoint_digest=provider_entrypoint_digest,
        runtime=runtime,
        deterministic=deterministic,
        side_effects=side_effects,
        artifact_name=artifact_name,
        artifact_digest=artifact_digest,
        input_payload=input_payload,
        output_payload=output_payload,
        status=status,
        error=error,
        started_at=started_at,
        finished_at=utc_now(),
        duration_ms=round(duration_ms, 3),
    )
