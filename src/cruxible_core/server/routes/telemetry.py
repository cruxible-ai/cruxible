"""Instance-local boundary telemetry routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["telemetry"])


@router.get(
    "/{instance_id}/telemetry/summary",
    response_model=contracts.BoundaryTelemetrySummaryResult,
)
async def telemetry_summary(instance_id: str) -> contracts.BoundaryTelemetrySummaryResult:
    return api.telemetry_summary(resolve_server_instance_id(instance_id))
