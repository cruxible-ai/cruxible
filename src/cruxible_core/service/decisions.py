"""Decision record lifecycle and auto-logging helpers."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from cruxible_core.decision.types import DecisionClass, DecisionEvent, DecisionRecord
from cruxible_core.errors import ConfigError
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.types import (
    DecisionEventListResult,
    DecisionRecordListResult,
    DecisionRecordServiceResult,
    OperationContext,
)

logger = logging.getLogger(__name__)
_SUMMARY_CHARS = 200


def digest_payload(payload: Any) -> tuple[str, str]:
    """Return deterministic digest and bounded summary for a JSON-like payload."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return digest, canonical[:_SUMMARY_CHARS]


def service_create_decision_record(
    instance: InstanceProtocol,
    *,
    question: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    opened_by: str = "human",
) -> DecisionRecordServiceResult:
    """Create a new open decision record."""
    record = DecisionRecord(
        question=question,
        subject_type=subject_type,
        subject_id=subject_id,
        opened_by=opened_by,  # type: ignore[arg-type]
    )
    store = instance.get_decision_store()
    try:
        store.save_record(record)
    finally:
        store.close()
    return DecisionRecordServiceResult(record=record)


def service_get_decision_record(
    instance: InstanceProtocol,
    decision_record_id: str,
    *,
    include_events: bool = True,
) -> DecisionRecordServiceResult:
    store = instance.get_decision_store()
    try:
        record = store.get_record(decision_record_id)
        if record is None:
            raise ConfigError(f"Decision record '{decision_record_id}' not found")
        events = store.list_events(decision_record_id) if include_events else []
    finally:
        store.close()
    return DecisionRecordServiceResult(record=record, events=events)


def service_list_decision_records(
    instance: InstanceProtocol,
    *,
    status: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    decision_class: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> DecisionRecordListResult:
    store = instance.get_decision_store()
    try:
        records = store.list_records(
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        )
    finally:
        store.close()
    return DecisionRecordListResult(records=records)


def service_list_decision_events(
    instance: InstanceProtocol,
    *,
    decision_record_id: str | None = None,
    receipt_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> DecisionEventListResult:
    store = instance.get_decision_store()
    try:
        if decision_record_id is not None:
            events = store.list_events(decision_record_id, limit=limit)
        else:
            events = store.find_events(
                receipt_id=receipt_id,
                trace_id=trace_id,
                status=status,
                limit=limit,
            )
    finally:
        store.close()
    return DecisionEventListResult(events=events)


def service_finalize_decision_record(
    instance: InstanceProtocol,
    decision_record_id: str,
    *,
    final_decision: str,
    decision_class: DecisionClass,
    rationale: str = "",
) -> DecisionRecordServiceResult:
    store = instance.get_decision_store()
    try:
        record = store.finalize_record(
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        )
        events = store.list_events(decision_record_id)
    finally:
        store.close()
    return DecisionRecordServiceResult(record=record, events=events)


def service_abandon_decision_record(
    instance: InstanceProtocol,
    decision_record_id: str,
    *,
    reason: str = "",
) -> DecisionRecordServiceResult:
    store = instance.get_decision_store()
    try:
        record = store.abandon_record(decision_record_id, reason=reason)
        events = store.list_events(decision_record_id)
    finally:
        store.close()
    return DecisionRecordServiceResult(record=record, events=events)


def ensure_decision_record_open(instance: InstanceProtocol, decision_record_id: str) -> None:
    """Raise if the decision record does not exist or is closed."""
    store = instance.get_decision_store()
    try:
        record = store.get_record(decision_record_id)
    finally:
        store.close()
    if record is None:
        raise ConfigError(f"Decision record '{decision_record_id}' not found")
    if record.status != "open":
        raise ConfigError(f"Decision record '{decision_record_id}' is not open")


def _append_event_if_context(
    instance: InstanceProtocol,
    context: OperationContext | None,
    *,
    command: str,
    status: str,
    input_payload: Any,
    started_at: datetime,
    output_payload: Any | None = None,
    receipt_id: str | None = None,
    trace_ids: list[str] | None = None,
    head_snapshot_id: str | None = None,
    error: BaseException | None = None,
) -> None:
    """Best-effort append of an operation event when a decision context exists.

    ``started_at`` must be captured by the caller before doing the work so
    duration reflects real elapsed time. ``finished_at`` is captured here.
    """
    if context is None or context.decision_record_id is None:
        return
    input_digest, input_summary = digest_payload(input_payload)
    output_digest: str | None = None
    output_summary: str | None = None
    if output_payload is not None:
        output_digest, output_summary = digest_payload(output_payload)
    event = DecisionEvent(
        decision_record_id=context.decision_record_id,
        command=command,
        status=status,  # type: ignore[arg-type]
        input_digest=input_digest,
        input_summary=input_summary,
        output_digest=output_digest,
        output_summary=output_summary,
        receipt_id=receipt_id,
        trace_ids=trace_ids or [],
        head_snapshot_id=head_snapshot_id,
        error_type=error.__class__.__name__ if error is not None else None,
        error_message=str(error) if error is not None else None,
        surface=context.surface,
        request_id=context.request_id,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
    )
    store = None
    try:
        store = instance.get_decision_store()
        store.append_event(event)
    except Exception:
        logger.warning(
            "Failed to append decision event for %s", context.decision_record_id, exc_info=True
        )
    finally:
        if store is not None:
            store.close()
