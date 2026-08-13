"""Playbill Family-1 HTTP routes; all orchestration stays in the runtime/service core."""

from __future__ import annotations

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.runtime import api
from cruxible_core.server.request_models import (
    PlaybillApprovalChallengeRequest,
    PlaybillApprovalRequest,
    PlaybillExplainRequest,
    PlaybillInitRequest,
    PlaybillProposeDocumentRequest,
    PlaybillProposePrincipalRequest,
    PlaybillReviewRequest,
    PlaybillSourceBundleRequest,
    PlaybillSourceProposeRequest,
    PlaybillStoreBodyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id

router = APIRouter(prefix="/api/v1", tags=["playbill"])


def _coordinate(
    git_oid: str | None,
    semantic_root: str | None,
    generation_root: str | None,
    compiler_digest: str | None,
) -> AcceptedCoordinate | None:
    values = (git_oid, semantic_root, generation_root, compiler_digest)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise PlaybillFormatError("accepted coordinate query requires all four coordinate fields")
    assert git_oid is not None
    assert semantic_root is not None
    assert generation_root is not None
    assert compiler_digest is not None
    return AcceptedCoordinate(
        git_oid=git_oid,
        semantic_root=semantic_root,
        generation_root=generation_root,
        compiler_digest=compiler_digest,
    )


@router.post("/{instance_id}/playbill/init", response_model=contracts.PlaybillInitResult)
async def playbill_init(
    instance_id: str,
    req: PlaybillInitRequest,
) -> contracts.PlaybillInitResult:
    return api.playbill_init(
        resolve_server_instance_id(instance_id),
        principals=req.principals,
        operating_profile=req.operating_profile,
    )


@router.post(
    "/{instance_id}/playbill/bodies",
    response_model=contracts.PlaybillCasObjectResult,
)
async def store_body(
    instance_id: str,
    req: PlaybillStoreBodyRequest,
) -> contracts.PlaybillCasObjectResult:
    return api.playbill_store_body(
        resolve_server_instance_id(instance_id), content_base64=req.content_base64
    )


@router.post(
    "/{instance_id}/playbill/documents/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
async def propose_document(
    instance_id: str,
    req: PlaybillProposeDocumentRequest,
) -> contracts.PlaybillProposalInspection:
    return api.playbill_propose_document(
        resolve_server_instance_id(instance_id),
        shell=req.shell,
        proposal_name=req.proposal_name,
        source_compilation_digest=req.source_compilation_digest,
        base=req.base,
    )


@router.post(
    "/{instance_id}/playbill/principals/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
async def propose_principal(
    instance_id: str,
    req: PlaybillProposePrincipalRequest,
) -> contracts.PlaybillProposalInspection:
    return api.playbill_propose_principal_change(
        resolve_server_instance_id(instance_id),
        principal=req.principal,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.get(
    "/{instance_id}/playbill/principals",
    response_model=contracts.PlaybillPrincipalList,
)
async def list_principals(instance_id: str) -> contracts.PlaybillPrincipalList:
    return api.playbill_list_principals(resolve_server_instance_id(instance_id))


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}",
    response_model=contracts.PlaybillProposalInspection,
)
async def inspect_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalInspection:
    return api.playbill_inspect_proposal(resolve_server_instance_id(instance_id), proposal_id)


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}/refusal",
    response_model=contracts.PlaybillRefusalInspection,
)
async def inspect_refusal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillRefusalInspection:
    return api.playbill_inspect_refusal(resolve_server_instance_id(instance_id), proposal_id)


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/review",
    response_model=contracts.PlaybillProposalReview,
)
async def review_proposal(
    instance_id: str,
    proposal_id: str,
    req: PlaybillReviewRequest,
) -> contracts.PlaybillProposalReview:
    return api.playbill_review_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        include_body=req.include_body,
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
    response_model=contracts.PlaybillApprovalChallenge,
)
async def prepare_approval(
    instance_id: str,
    proposal_id: str,
    req: PlaybillApprovalChallengeRequest,
) -> contracts.PlaybillApprovalChallenge:
    return api.playbill_prepare_approval(
        resolve_server_instance_id(instance_id),
        proposal_id,
        signer_id=req.signer_id,
        include_body=req.include_body,
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/approvals",
    response_model=contracts.PlaybillApprovalReceipt,
)
async def submit_approval(
    instance_id: str,
    proposal_id: str,
    req: PlaybillApprovalRequest,
) -> contracts.PlaybillApprovalReceipt:
    return api.playbill_submit_approval(
        resolve_server_instance_id(instance_id),
        proposal_id,
        attestation=req.attestation,
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/activate",
    response_model=contracts.PlaybillActivationReceipt,
)
async def activate_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillActivationReceipt:
    return api.playbill_activate(resolve_server_instance_id(instance_id), proposal_id)


@router.get(
    "/{instance_id}/playbill/documents",
    response_model=contracts.PlaybillDocumentList,
)
async def list_documents(
    instance_id: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillDocumentList:
    return api.playbill_list_documents(
        resolve_server_instance_id(instance_id),
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/documents/{identity}",
    response_model=contracts.PlaybillDocumentView,
)
async def get_document(
    instance_id: str,
    identity: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillDocumentView:
    return api.playbill_get_document(
        resolve_server_instance_id(instance_id),
        identity,
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/documents/{identity}/body",
    response_model=contracts.PlaybillBodyRead,
)
async def dereference_document(
    instance_id: str,
    identity: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillBodyRead:
    return api.playbill_dereference_document(
        resolve_server_instance_id(instance_id),
        identity,
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/documents/{identity}/history",
    response_model=contracts.PlaybillDocumentHistory,
)
async def document_history(
    instance_id: str,
    identity: str,
) -> contracts.PlaybillDocumentHistory:
    return api.playbill_document_history(resolve_server_instance_id(instance_id), identity)


@router.post(
    "/{instance_id}/playbill/explain",
    response_model=contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail,
)
async def explain(
    instance_id: str,
    req: PlaybillExplainRequest,
) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
    return api.playbill_explain(
        resolve_server_instance_id(instance_id),
        subject=req.subject,
        at=req.at,
        detail=req.detail,
        include_body=req.include_body,
    )


@router.get(
    "/{instance_id}/playbill/sources/context",
    response_model=contracts.PlaybillSourceContext,
)
async def source_context(instance_id: str) -> contracts.PlaybillSourceContext:
    return api.playbill_source_context(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/sources/check",
    response_model=contracts.PlaybillSourceCheckResult,
)
async def check_sources(
    instance_id: str,
    req: PlaybillSourceBundleRequest,
) -> contracts.PlaybillSourceCheckResult:
    return api.playbill_check_source_bundle(
        resolve_server_instance_id(instance_id), bundle=req.bundle
    )


@router.post(
    "/{instance_id}/playbill/sources/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
async def propose_sources(
    instance_id: str,
    req: PlaybillSourceProposeRequest,
) -> contracts.PlaybillProposalInspection:
    return api.playbill_propose_source_bundle(
        resolve_server_instance_id(instance_id),
        bundle=req.bundle,
        source_name=req.source_name,
        proposal_name=req.proposal_name,
    )


__all__ = ["router"]
