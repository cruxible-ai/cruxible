"""Receipt and provider trace persistence for workflow execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace, ProviderRuntime
from cruxible_core.receipt.types import Receipt


def _persist_receipt(instance: InstanceProtocol, receipt: Receipt) -> None:
    store = instance.get_receipt_store()
    try:
        store.save_receipt(receipt)
    finally:
        store.close()


def _persist_trace(instance: InstanceProtocol, trace: ExecutionTrace) -> None:
    store = instance.get_receipt_store()
    try:
        store.save_trace(trace)
    finally:
        store.close()


def _build_trace(
    *,
    workflow_name: str,
    step_id: str,
    provider_name: str,
    provider_version: str,
    provider_ref: str,
    provider_entrypoint_sha256: str | None,
    runtime: ProviderRuntime,
    deterministic: bool,
    side_effects: bool,
    artifact_name: str | None,
    artifact_sha256: str | None,
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
        provider_entrypoint_sha256=provider_entrypoint_sha256,
        runtime=runtime,
        deterministic=deterministic,
        side_effects=side_effects,
        artifact_name=artifact_name,
        artifact_sha256=artifact_sha256,
        input_payload=input_payload,
        output_payload=output_payload,
        status=status,
        error=error,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        duration_ms=round(duration_ms, 3),
    )
