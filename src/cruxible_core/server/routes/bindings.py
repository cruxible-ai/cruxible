"""Compute-slot binding ledger routes (read-only).

Bindings are deployment records, so the useful HTTP surface is inspection: what
is this install running each slot on, and what did it run on before. The
governed WRITE verbs (bind / rebind / retire) stay service-only in this phase.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["slot-bindings"])


@router.get(
    "/{instance_id}/slot-bindings",
    response_model=contracts.SlotBindingListResult,
)
async def list_slot_bindings(
    instance_id: str,
    install_id: str | None = Query(default=None),
    slot_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.SlotBindingListResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.list_slot_bindings(
        instance_id=resolved_instance_id,
        install_id=install_id,
        slot_name=slot_name,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/slot-bindings/{binding_id}/history",
    response_model=contracts.SlotBindingHistoryResult,
)
async def get_slot_binding_history(
    instance_id: str,
    binding_id: str,
    limit: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.SlotBindingHistoryResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.get_slot_binding_history(
        instance_id=resolved_instance_id,
        binding_id=binding_id,
        limit=limit,
        offset=offset,
    )
