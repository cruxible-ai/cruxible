"""Internal HTTP transport routes for governed procedure surfaces."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.procedure.types import ProcedureStatus
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    ProposeProcedureRequest,
    ResolveProcedureRequest,
    RetireProcedureRequest,
    RunProcedureRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["procedures"])


@router.post(
    "/{instance_id}/procedures/propose",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def propose_procedure(
    instance_id: str,
    req: ProposeProcedureRequest,
) -> dict[str, Any]:
    return api.propose_procedure(
        resolve_server_instance_id(instance_id),
        req.definition,
        supersedes_procedure_id=req.supersedes_procedure_id,
        evidence_refs=req.evidence_refs,
        actor_context=req.actor_context,
    )


@router.get(
    "/{instance_id}/procedures",
    response_model=contracts.ListResult,
    include_in_schema=False,
)
async def list_procedures(
    instance_id: str,
    status: ProcedureStatus | None = None,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.list_procedures(
        resolve_server_instance_id(instance_id),
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/procedures/{procedure_id}",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def get_procedure(instance_id: str, procedure_id: str) -> dict[str, Any]:
    return api.get_procedure(resolve_server_instance_id(instance_id), procedure_id)


@router.post(
    "/{instance_id}/procedures/{procedure_id}/resolve",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def resolve_procedure(
    instance_id: str,
    procedure_id: str,
    req: ResolveProcedureRequest,
) -> dict[str, Any]:
    return api.resolve_procedure(
        resolve_server_instance_id(instance_id),
        procedure_id,
        action=req.action,
        expected_version=req.expected_version,
        reason=req.reason,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/procedures/{procedure_id}/retire",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def retire_procedure(
    instance_id: str,
    procedure_id: str,
    req: RetireProcedureRequest,
) -> dict[str, Any]:
    return api.retire_procedure(
        resolve_server_instance_id(instance_id),
        procedure_id,
        expected_version=req.expected_version,
        reason=req.reason,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/procedures/{procedure_id}/run",
    response_model=dict[str, Any],
    include_in_schema=False,
)
async def run_procedure(
    instance_id: str,
    procedure_id: str,
    req: RunProcedureRequest,
) -> dict[str, Any]:
    return api.run_procedure(
        resolve_server_instance_id(instance_id),
        procedure_id,
        input_payload=req.input_payload,
        actor_context=req.actor_context,
    )


@router.get(
    "/{instance_id}/procedures/{procedure_id}/runs",
    response_model=contracts.ListResult,
    include_in_schema=False,
)
async def list_procedure_runs(
    instance_id: str,
    procedure_id: str,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.list_procedure_runs(
        resolve_server_instance_id(instance_id),
        procedure_id,
        limit=limit,
        offset=offset,
    )
