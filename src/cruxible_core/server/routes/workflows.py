"""Workflow execution and proposal routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    WorkflowApplyRequest,
    WorkflowInputRequest,
    WorkflowLockRequest,
    WorkflowTestRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["workflows"])


@router.post("/{instance_id}/workflows/lock", response_model=contracts.WorkflowLockResult)
async def workflow_lock(
    instance_id: str,
    req: WorkflowLockRequest | None = None,
) -> contracts.WorkflowLockResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.workflow_lock(
        resolved_instance_id,
        force=req.force if req is not None else False,
    )


@router.post("/{instance_id}/workflows/plan", response_model=contracts.WorkflowPlanResult)
async def workflow_plan(
    instance_id: str,
    req: WorkflowInputRequest,
) -> contracts.WorkflowPlanResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.workflow_plan(
        resolved_instance_id,
        req.workflow_name,
        req.input,
    )


@router.post("/{instance_id}/workflows/run", response_model=contracts.WorkflowRunResult)
async def workflow_run(
    instance_id: str,
    req: WorkflowInputRequest,
) -> contracts.WorkflowRunResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.workflow_run(
        resolved_instance_id,
        req.workflow_name,
        req.input,
        decision_record_id=req.decision_record_id,
        surface="http",
    )


@router.post("/{instance_id}/workflows/apply", response_model=contracts.WorkflowApplyResult)
async def workflow_apply(
    instance_id: str,
    req: WorkflowApplyRequest,
) -> contracts.WorkflowApplyResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.workflow_apply(
        resolved_instance_id,
        req.workflow_name,
        req.expected_apply_digest,
        req.expected_head_snapshot_id,
        req.input,
        decision_record_id=req.decision_record_id,
        surface="http",
    )


@router.post("/{instance_id}/workflows/test", response_model=contracts.WorkflowTestResult)
async def workflow_test(
    instance_id: str,
    req: WorkflowTestRequest,
) -> contracts.WorkflowTestResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.workflow_test(resolved_instance_id, req.name)


@router.post("/{instance_id}/workflows/propose", response_model=contracts.WorkflowProposeResult)
async def workflow_propose(
    instance_id: str,
    req: WorkflowInputRequest,
) -> contracts.WorkflowProposeResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.propose_workflow(
        resolved_instance_id,
        req.workflow_name,
        req.input,
        decision_record_id=req.decision_record_id,
        surface="http",
    )
