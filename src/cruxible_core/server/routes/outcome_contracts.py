"""Hidden HTTP parity routes for outcome resolution contracts."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    DisposeOutcomeResolutionRequest,
    OpenOutcomeContractRequest,
    ResolveOutcomeRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["outcome-contracts"])


@router.post(
    "/{instance_id}/outcome-contracts/open",
    response_model=contracts.OutcomeContractResult,
)
async def open_outcome_contract(
    instance_id: str,
    req: OpenOutcomeContractRequest,
) -> contracts.OutcomeContractResult:
    return api.open_outcome_contract(
        resolve_server_instance_id(instance_id),
        entity_type=req.entity_type,
        entity_id=req.entity_id,
        description=req.description,
        check_at=req.check_at,
        expires_at=req.expires_at,
        measurement=req.measurement,
        idempotency_key=req.idempotency_key,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/outcome-contracts/{contract_id}/resolve",
    response_model=contracts.OutcomeResolutionResult,
)
async def resolve_outcome(
    instance_id: str,
    contract_id: str,
    req: ResolveOutcomeRequest,
) -> contracts.OutcomeResolutionResult:
    return api.resolve_outcome(
        resolve_server_instance_id(instance_id),
        contract_id,
        verdict=req.verdict,
        observed_at=req.observed_at,
        evidence_refs=req.evidence_refs,
        note=req.note,
        resolving_query_receipt_id=req.resolving_query_receipt_id,
        resolving_attestation_ids=req.resolving_attestation_ids,
        actor_context=req.actor_context,
    )


@router.post(
    "/{instance_id}/outcome-resolutions/{resolution_id}/dispose",
    response_model=contracts.OutcomeDispositionResult,
)
async def dispose_outcome_resolution(
    instance_id: str,
    resolution_id: str,
    req: DisposeOutcomeResolutionRequest,
) -> contracts.OutcomeDispositionResult:
    return api.dispose_outcome_resolution(
        resolve_server_instance_id(instance_id),
        resolution_id,
        verdict=req.verdict,
        note=req.note,
        actor_context=req.actor_context,
    )


@router.get(
    "/{instance_id}/outcome-contracts",
    response_model=contracts.ListResult,
)
async def list_outcome_contracts(
    instance_id: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    status: contracts.ContractStatus | None = None,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.list_outcome_contracts(
        resolve_server_instance_id(instance_id),
        entity_type=entity_type,
        entity_id=entity_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/outcome-contracts/queue",
    response_model=contracts.ListResult,
)
async def outcome_due(
    instance_id: str,
    queue: contracts.ContractQueue = "due",
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.outcome_due(
        resolve_server_instance_id(instance_id),
        queue=queue,
        limit=limit,
        offset=offset,
    )


__all__ = ["router"]
