"""Decision record lifecycle and auto-logging helpers."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cruxible_core.decision.types import (
    DecisionClass,
    DecisionEvent,
    DecisionRecord,
    digest_payload,
)
from cruxible_core.errors import ConfigError
from cruxible_core.governance.actors import GovernedActorContext, derived_actor_kind
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.mutation_receipts import mutation_receipt
from cruxible_core.service.types import (
    DecisionEventAppendOutcome,
    DecisionEventListResult,
    DecisionRecordListResult,
    DecisionRecordServiceResult,
    OperationContext,
)
from cruxible_core.temporal import utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "digest_payload",
    "ensure_decision_record_open",
    "record_decision_event_for_context",
    "service_abandon_decision_record",
    "service_create_decision_record",
    "service_finalize_decision_record",
    "service_get_decision_record",
    "service_list_decision_events",
    "service_list_decision_records",
]


def service_create_decision_record(
    instance: InstanceProtocol,
    *,
    question: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    actor_context: GovernedActorContext | None = None,
) -> DecisionRecordServiceResult:
    """Create a new open decision record."""
    record = DecisionRecord(
        question=question,
        subject_type=subject_type,
        subject_id=subject_id,
        opened_actor_context=actor_context,
    )
    with mutation_receipt(
        instance,
        "decision_record_open",
        {
            "decision_record_id": record.decision_record_id,
            "question": question,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        ctx.builder.record_validation(
            passed=True,
            detail={
                "decision_record_id": record.decision_record_id,
                "opened_by": derived_actor_kind(actor_context),
            },
            entity_type="DecisionRecord",
            entity_id=record.decision_record_id,
        )
        ctx.uow.decisions.save_record(record)
        ctx.set_result(DecisionRecordServiceResult(record=record))

    result = ctx.result
    assert isinstance(result, DecisionRecordServiceResult)
    return result


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
        total = store.count_records(
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
        )
    finally:
        store.close()
    return DecisionRecordListResult(items=records, total=total)


def service_list_decision_events(
    instance: InstanceProtocol,
    *,
    decision_record_id: str | None = None,
    receipt_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> DecisionEventListResult:
    store = instance.get_decision_store()
    try:
        if decision_record_id is not None:
            events = store.list_events(decision_record_id, limit=limit, offset=offset)
            total = store.count_events(decision_record_id=decision_record_id)
        else:
            events = store.find_events(
                receipt_id=receipt_id,
                trace_id=trace_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            total = store.count_events(
                receipt_id=receipt_id,
                trace_id=trace_id,
                status=status,
            )
    finally:
        store.close()
    return DecisionEventListResult(items=events, total=total)


def service_finalize_decision_record(
    instance: InstanceProtocol,
    decision_record_id: str,
    *,
    final_decision: str,
    decision_class: DecisionClass,
    rationale: str = "",
    actor_context: GovernedActorContext | None = None,
) -> DecisionRecordServiceResult:
    with mutation_receipt(
        instance,
        "decision_record_finalize",
        {
            "decision_record_id": decision_record_id,
            "final_decision": final_decision,
            "decision_class": decision_class,
            "rationale": rationale,
        },
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        record = ctx.uow.decisions.finalize_record(
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
            actor_context=actor_context,
        )
        events = ctx.uow.decisions.list_events(decision_record_id)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "decision_record_id": decision_record_id,
                "decision_class": decision_class,
                "event_count": len(events),
            },
            entity_type="DecisionRecord",
            entity_id=decision_record_id,
        )
        ctx.set_result(DecisionRecordServiceResult(record=record, events=events))

    result = ctx.result
    assert isinstance(result, DecisionRecordServiceResult)
    return result


def service_abandon_decision_record(
    instance: InstanceProtocol,
    decision_record_id: str,
    *,
    reason: str = "",
    actor_context: GovernedActorContext | None = None,
) -> DecisionRecordServiceResult:
    with mutation_receipt(
        instance,
        "decision_record_abandon",
        {"decision_record_id": decision_record_id, "reason": reason},
        actor_context=actor_context,
    ) as ctx:
        assert ctx.builder is not None
        assert ctx.uow is not None
        record = ctx.uow.decisions.abandon_record(
            decision_record_id,
            reason=reason,
            actor_context=actor_context,
        )
        events = ctx.uow.decisions.list_events(decision_record_id)
        ctx.builder.record_validation(
            passed=True,
            detail={
                "decision_record_id": decision_record_id,
                "reason": reason,
                "event_count": len(events),
            },
            entity_type="DecisionRecord",
            entity_id=decision_record_id,
        )
        ctx.set_result(DecisionRecordServiceResult(record=record, events=events))

    result = ctx.result
    assert isinstance(result, DecisionRecordServiceResult)
    return result


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


def record_decision_event_for_context(
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
) -> DecisionEventAppendOutcome:
    """Best-effort append of a decision-record event when context requests it.

    A ``decision_record_id`` is the switch for decision recording mode: reads
    and writes can both be captured as audit evidence for the decision. This is
    decision-record audit metadata, not an operation receipt.

    A failed append still does not fail the underlying operation — audit
    metadata must never break the work it observes. It is no longer swallowed
    either: the outcome is returned, and a failure is also recorded on
    ``context.decision_event_failures`` so a caller that ignores the return
    value still carries the evidence loss forward instead of it living only in
    a log line.

    ``started_at`` must be captured by the caller before doing the work so
    duration reflects real elapsed time. ``finished_at`` is captured here.
    """
    if context is None or context.decision_record_id is None:
        return DecisionEventAppendOutcome(requested=False, appended=False)
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
        actor_context=context.actor_context,
        started_at=started_at,
        finished_at=utc_now(),
    )
    try:
        with instance.write_transaction() as uow:
            decision_event_id = uow.decisions.append_event(event)
    except Exception as exc:
        logger.error(
            "Failed to append decision event for %s", context.decision_record_id, exc_info=True
        )
        failure = DecisionEventAppendOutcome(
            requested=True,
            appended=False,
            decision_record_id=context.decision_record_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        context.decision_event_failures.append(failure)
        return failure
    return DecisionEventAppendOutcome(
        requested=True,
        appended=True,
        decision_record_id=context.decision_record_id,
        decision_event_id=decision_event_id,
    )
