"""Hidden HTTP parity routes for claim attestations."""

from __future__ import annotations

from fastapi import APIRouter, Query

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import AttestRequest, ResolveAttestationRequest
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["attestations"])


@router.post(
    "/{instance_id}/attestations/record",
    response_model=contracts.AttestationRecordResult,
    include_in_schema=False,
)
async def attest(
    instance_id: str,
    req: AttestRequest,
) -> contracts.AttestationRecordResult:
    return api.attest(
        resolve_server_instance_id(instance_id),
        relationship_type=req.relationship_type,
        from_type=req.from_type,
        from_id=req.from_id,
        to_type=req.to_type,
        to_id=req.to_id,
        stance=req.stance,
        evidence_refs=req.evidence_refs,
        observed_at=req.observed_at,
        edge_key=req.edge_key,
        properties=req.properties,
        note=req.note,
        idempotency_key=req.idempotency_key,
        actor_context=req.actor_context,
    )


@router.get(
    "/{instance_id}/attestations",
    response_model=contracts.ListResult,
    include_in_schema=False,
)
async def list_attestations(
    instance_id: str,
    relationship_type: str | None = None,
    from_type: str | None = None,
    from_id: str | None = None,
    to_type: str | None = None,
    to_id: str | None = None,
    stance: contracts.AttestationStance | None = None,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.list_attestations(
        resolve_server_instance_id(instance_id),
        relationship_type=relationship_type,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        stance=stance,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{instance_id}/attestations/queue",
    response_model=contracts.ListResult,
    include_in_schema=False,
)
async def attestation_queue(
    instance_id: str,
    limit: int = Query(default=100, ge=1),
    offset: int = Query(default=0, ge=0),
) -> contracts.ListResult:
    return api.attestation_queue(
        resolve_server_instance_id(instance_id),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{instance_id}/attestations/{attestation_id}/resolve",
    response_model=contracts.AttestationDispositionResult,
    include_in_schema=False,
)
async def resolve_attestation(
    instance_id: str,
    attestation_id: str,
    req: ResolveAttestationRequest,
) -> contracts.AttestationDispositionResult:
    return api.resolve_attestation(
        resolve_server_instance_id(instance_id),
        attestation_id,
        verdict=req.verdict,
        note=req.note,
        follow_up_receipt_id=req.follow_up_receipt_id,
        actor_context=req.actor_context,
    )


__all__ = ["router"]
