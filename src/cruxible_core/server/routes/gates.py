"""Declared gate evaluation routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import GateCheckRequest
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["gates"])


@router.post(
    "/{instance_id}/gates/{gate_name}/check",
    response_model=contracts.GateEvaluationResult,
)
async def gate_check(
    instance_id: str,
    gate_name: str,
    req: GateCheckRequest,
) -> contracts.GateEvaluationResult:
    """Evaluate caller-derived candidates at one state revision."""
    return api.gate_check(
        resolve_server_instance_id(instance_id),
        gate_name,
        req.candidates,
        error_reason=req.error_reason,
    )
