"""Decision record routes."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    DecisionRecordAbandonRequest,
    DecisionRecordCreateRequest,
    DecisionRecordFinalizeRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["decision-records"])


@router.post("/{instance_id}/decision-records", response_model=contracts.DecisionRecordResult)
async def create_decision_record(
    instance_id: str,
    req: DecisionRecordCreateRequest,
) -> contracts.DecisionRecordResult:
    return api.create_decision_record(
        resolve_server_instance_id(instance_id),
        question=req.question,
        subject_type=req.subject_type,
        subject_id=req.subject_id,
        opened_by=req.opened_by,
    )


@router.get("/{instance_id}/decision-records", response_model=contracts.DecisionRecordListResult)
async def list_decision_records(
    instance_id: str,
    status: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    decision_class: contracts.DecisionClass | None = None,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.DecisionRecordListResult:
    return api.list_decision_records(
        resolve_server_instance_id(instance_id),
        status=status,
        subject_type=subject_type,
        subject_id=subject_id,
        decision_class=decision_class,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/decision-records/events",
    response_model=contracts.DecisionEventListResult,
)
async def list_decision_events(
    instance_id: str,
    decision_record_id: str | None = None,
    receipt_id: str | None = None,
    trace_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.DecisionEventListResult:
    return api.list_decision_events(
        resolve_server_instance_id(instance_id),
        decision_record_id=decision_record_id,
        receipt_id=receipt_id,
        trace_id=trace_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/decision-records/{decision_record_id}",
    response_model=contracts.DecisionRecordResult,
)
async def get_decision_record(
    instance_id: str,
    decision_record_id: str,
    include_events: bool = True,
) -> contracts.DecisionRecordResult:
    return api.get_decision_record(
        resolve_server_instance_id(instance_id),
        decision_record_id,
        include_events=include_events,
    )


@router.post(
    "/{instance_id}/decision-records/{decision_record_id}/finalize",
    response_model=contracts.DecisionRecordResult,
)
async def finalize_decision_record(
    instance_id: str,
    decision_record_id: str,
    req: DecisionRecordFinalizeRequest,
) -> contracts.DecisionRecordResult:
    return api.finalize_decision_record(
        resolve_server_instance_id(instance_id),
        decision_record_id,
        final_decision=req.final_decision,
        decision_class=req.decision_class,
        rationale=req.rationale,
    )


@router.post(
    "/{instance_id}/decision-records/{decision_record_id}/abandon",
    response_model=contracts.DecisionRecordResult,
)
async def abandon_decision_record(
    instance_id: str,
    decision_record_id: str,
    req: DecisionRecordAbandonRequest,
) -> contracts.DecisionRecordResult:
    return api.abandon_decision_record(
        resolve_server_instance_id(instance_id),
        decision_record_id,
        reason=req.reason,
    )
