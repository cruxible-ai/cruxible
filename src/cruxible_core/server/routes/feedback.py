"""Feedback and outcome routes."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    AnalyzeFeedbackRequest,
    AnalyzeOutcomesRequest,
    FeedbackBatchRequest,
    FeedbackFromQueryRequest,
    FeedbackRequest,
    OutcomeRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["feedback"])


@router.post("/{instance_id}/feedback", response_model=contracts.FeedbackResult)
async def feedback(instance_id: str, req: FeedbackRequest) -> contracts.FeedbackResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.feedback(
        instance_id=resolved_instance_id,
        receipt_id=req.receipt_id,
        action=req.action,
        source=req.source,
        from_type=req.from_type,
        from_id=req.from_id,
        relationship_type=req.relationship_type,
        to_type=req.to_type,
        to_id=req.to_id,
        edge_key=req.edge_key,
        reason=req.reason,
        reason_code=req.reason_code,
        scope_hints=req.scope_hints,
        corrections=req.corrections,
        group_override=req.group_override,
    )


@router.post("/{instance_id}/feedback/batch", response_model=contracts.FeedbackBatchResult)
async def feedback_batch(
    instance_id: str,
    req: FeedbackBatchRequest,
) -> contracts.FeedbackBatchResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.feedback_batch(
        instance_id=resolved_instance_id,
        items=req.items,
        source=req.source,
    )


@router.post("/{instance_id}/feedback/from-query", response_model=contracts.FeedbackResult)
async def feedback_from_query(
    instance_id: str,
    req: FeedbackFromQueryRequest,
) -> contracts.FeedbackResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.feedback_from_query(
        instance_id=resolved_instance_id,
        receipt_id=req.receipt_id,
        result_index=req.result_index,
        action=req.action,
        source=req.source,
        reason=req.reason,
        reason_code=req.reason_code,
        scope_hints=req.scope_hints,
        corrections=req.corrections,
        group_override=req.group_override,
        path_index=req.path_index,
        path_alias=req.path_alias,
    )


@router.post(
    "/{instance_id}/feedback/analyze",
    response_model=contracts.AnalyzeFeedbackResult,
)
async def analyze_feedback(
    instance_id: str,
    req: AnalyzeFeedbackRequest,
) -> contracts.AnalyzeFeedbackResult:
    return api.analyze_feedback(
        instance_id=resolve_server_instance_id(instance_id),
        relationship_type=req.relationship_type,
        limit=req.limit,
        min_support=req.min_support,
        decision_surface_type=req.decision_surface_type,
        decision_surface_name=req.decision_surface_name,
        property_pairs=req.property_pairs,
    )


@router.get(
    "/{instance_id}/feedback/profiles/{relationship_type}",
    response_model=contracts.FeedbackProfileResult,
)
async def get_feedback_profile(
    instance_id: str,
    relationship_type: str,
) -> contracts.FeedbackProfileResult:
    return api.get_feedback_profile(
        instance_id=resolve_server_instance_id(instance_id),
        relationship_type=relationship_type,
    )


@router.post("/{instance_id}/outcome", response_model=contracts.OutcomeResult)
async def outcome(instance_id: str, req: OutcomeRequest) -> contracts.OutcomeResult:
    resolved_instance_id = resolve_server_instance_id(instance_id)
    return api.outcome(
        instance_id=resolved_instance_id,
        receipt_id=req.receipt_id,
        anchor_type=req.anchor_type,
        anchor_id=req.anchor_id,
        outcome=req.outcome,
        source=req.source,
        outcome_code=req.outcome_code,
        scope_hints=req.scope_hints,
        outcome_profile_key=req.outcome_profile_key,
        detail=req.detail,
    )


@router.get(
    "/{instance_id}/outcome/profile",
    response_model=contracts.OutcomeProfileResult,
)
async def get_outcome_profile(
    instance_id: str,
    anchor_type: contracts.OutcomeAnchorType,
    relationship_type: str | None = None,
    workflow_name: str | None = None,
    surface_type: str | None = None,
    surface_name: str | None = None,
) -> contracts.OutcomeProfileResult:
    return api.get_outcome_profile(
        instance_id=resolve_server_instance_id(instance_id),
        anchor_type=anchor_type,
        relationship_type=relationship_type,
        workflow_name=workflow_name,
        surface_type=surface_type,
        surface_name=surface_name,
    )


@router.post(
    "/{instance_id}/outcomes/analyze",
    response_model=contracts.AnalyzeOutcomesResult,
)
async def analyze_outcomes(
    instance_id: str,
    req: AnalyzeOutcomesRequest,
) -> contracts.AnalyzeOutcomesResult:
    return api.analyze_outcomes(
        instance_id=resolve_server_instance_id(instance_id),
        anchor_type=req.anchor_type,
        relationship_type=req.relationship_type,
        workflow_name=req.workflow_name,
        query_name=req.query_name,
        surface_type=req.surface_type,
        surface_name=req.surface_name,
        limit=req.limit,
        min_support=req.min_support,
    )
