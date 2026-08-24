"""Playbill Family-1 HTTP routes; all orchestration stays in the runtime/service core."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter

from cruxible_client import contracts
from cruxible_core.playbill.claim_type_migrations import ClaimTypeMigrationRequest
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.runtime import playbill_api
from cruxible_core.server.playbill_request_models import (
    PlaybillApprovalChallengeRequest,
    PlaybillApprovalRequest,
    PlaybillAuthoringCompileRequest,
    PlaybillAuthoringCreateRequest,
    PlaybillAuthoringInputCompileRequest,
    PlaybillAuthoringInputCreateRequest,
    PlaybillAuthoringPreflightRequest,
    PlaybillAuthoringRebaseRequest,
    PlaybillAuthoringSubmitRequest,
    PlaybillClaimExplainRequest,
    PlaybillDiscoverRequest,
    PlaybillExpandRequest,
    PlaybillExplainRequest,
    PlaybillFloorExportRequest,
    PlaybillInitRequest,
    PlaybillInsertionAbandonRequest,
    PlaybillInsertionConfirmRequest,
    PlaybillProposalReadmitRequest,
    PlaybillProposeClaimRequest,
    PlaybillProposeClaimsRequest,
    PlaybillProposeClaimTypeInputRequest,
    PlaybillProposeClaimTypeRequest,
    PlaybillProposeDocumentRequest,
    PlaybillProposePrincipalRequest,
    PlaybillProposeQueryDefinitionRequest,
    PlaybillProposeSubjectRequest,
    PlaybillResolveCoverageRequest,
    PlaybillReviewRequest,
    PlaybillRunQueryRequest,
    PlaybillSearchRequest,
    PlaybillSourceBundleRequest,
    PlaybillSourceProposeRequest,
    PlaybillStoreBodyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id
from cruxible_core.service.playbill_procedure_runs import (
    ProcedureBindRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV1,
)

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
    return playbill_api.playbill_init(
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
    return playbill_api.playbill_store_body(
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
    return playbill_api.playbill_propose_document(
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
    return playbill_api.playbill_propose_principal_change(
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
    return playbill_api.playbill_list_principals(resolve_server_instance_id(instance_id))


@router.get(
    "/{instance_id}/playbill/whoami",
    response_model=contracts.PlaybillWhoAmI,
)
async def whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
    return playbill_api.playbill_whoami(resolve_server_instance_id(instance_id))


@router.get(
    "/{instance_id}/playbill/proposals",
    response_model=contracts.PlaybillProposalList,
)
async def list_proposals(
    instance_id: str,
    status: Literal["open", "settled"] | None = None,
) -> contracts.PlaybillProposalList:
    return playbill_api.playbill_list_proposals(
        resolve_server_instance_id(instance_id),
        status=status,
    )


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}",
    response_model=contracts.PlaybillProposalInspection,
)
async def inspect_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_inspect_proposal(
        resolve_server_instance_id(instance_id), proposal_id
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/readmit",
    response_model=contracts.PlaybillProposalReadmitResult,
)
async def readmit_proposal(
    instance_id: str,
    proposal_id: str,
    _req: PlaybillProposalReadmitRequest,
) -> contracts.PlaybillProposalReadmitResult:
    return playbill_api.playbill_readmit_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
    )


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}/refusal",
    response_model=contracts.PlaybillRefusalInspection,
)
async def inspect_refusal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillRefusalInspection:
    return playbill_api.playbill_inspect_refusal(
        resolve_server_instance_id(instance_id), proposal_id
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/review",
    response_model=contracts.PlaybillProposalReview,
)
async def review_proposal(
    instance_id: str,
    proposal_id: str,
    req: PlaybillReviewRequest,
) -> contracts.PlaybillProposalReview:
    return playbill_api.playbill_review_proposal(
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
    return playbill_api.playbill_prepare_approval(
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
    return playbill_api.playbill_submit_approval(
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
    return playbill_api.playbill_activate(resolve_server_instance_id(instance_id), proposal_id)


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
    return playbill_api.playbill_list_documents(
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
    return playbill_api.playbill_get_document(
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
    return playbill_api.playbill_dereference_document(
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
    return playbill_api.playbill_document_history(resolve_server_instance_id(instance_id), identity)


@router.post(
    "/{instance_id}/playbill/explain",
    response_model=contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail,
)
async def explain(
    instance_id: str,
    req: PlaybillExplainRequest,
) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
    return playbill_api.playbill_explain(
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
    return playbill_api.playbill_source_context(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/sources/check",
    response_model=contracts.PlaybillSourceCheckResult,
)
async def check_sources(
    instance_id: str,
    req: PlaybillSourceBundleRequest,
) -> contracts.PlaybillSourceCheckResult:
    return playbill_api.playbill_check_source_bundle(
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
    return playbill_api.playbill_propose_source_bundle(
        resolve_server_instance_id(instance_id),
        bundle=req.bundle,
        source_name=req.source_name,
        proposal_name=req.proposal_name,
    )


@router.post(
    "/{instance_id}/playbill/subjects/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
async def propose_subject(
    instance_id: str,
    req: PlaybillProposeSubjectRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_subject(
        resolve_server_instance_id(instance_id),
        shell=req.shell,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.get(
    "/{instance_id}/playbill/subjects",
    response_model=contracts.PlaybillSubjectList,
)
async def list_subjects(
    instance_id: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillSubjectList:
    return playbill_api.playbill_list_subjects(
        resolve_server_instance_id(instance_id),
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}",
    response_model=contracts.PlaybillSubjectView,
)
async def get_subject(
    instance_id: str,
    subject_kind: str,
    subject_id: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillSubjectView:
    return playbill_api.playbill_get_subject(
        resolve_server_instance_id(instance_id),
        f"Subject:{subject_kind}/{subject_id}",
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/subjects/{subject_kind}/{subject_id}/history",
    response_model=contracts.PlaybillSubjectHistory,
)
async def subject_history(
    instance_id: str,
    subject_kind: str,
    subject_id: str,
) -> contracts.PlaybillSubjectHistory:
    return playbill_api.playbill_subject_history(
        resolve_server_instance_id(instance_id),
        f"Subject:{subject_kind}/{subject_id}",
    )


@router.post(
    "/{instance_id}/playbill/claim-types/proposals",
    response_model=(
        contracts.PlaybillProposalInspection | contracts.PlaybillClaimTypeInputProposalResult
    ),
)
async def propose_claim_type(
    instance_id: str,
    req: PlaybillProposeClaimTypeRequest | PlaybillProposeClaimTypeInputRequest,
) -> contracts.PlaybillProposalInspection | contracts.PlaybillClaimTypeInputProposalResult:
    if isinstance(req, PlaybillProposeClaimTypeInputRequest):
        return playbill_api.playbill_propose_claim_type_input(
            resolve_server_instance_id(instance_id),
            input=req.input,
            proposal_name=req.proposal_name,
        )
    return playbill_api.playbill_propose_claim_type(
        resolve_server_instance_id(instance_id),
        claim_type=req.claim_type,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.post(
    "/{instance_id}/playbill/claim-types/migrations",
    response_model=contracts.PlaybillClaimTypeMigrationResponse,
)
async def migrate_claim_type(
    instance_id: str,
    req: ClaimTypeMigrationRequest,
) -> contracts.PlaybillClaimTypeMigrationResponse:
    return playbill_api.playbill_migrate_claim_type(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.get(
    "/{instance_id}/playbill/claim-types",
    response_model=contracts.PlaybillClaimTypeList,
)
async def list_claim_types(
    instance_id: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillClaimTypeList:
    return playbill_api.playbill_list_claim_types(
        resolve_server_instance_id(instance_id),
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/claim-types/{predicate}",
    response_model=contracts.PlaybillClaimTypeView,
)
async def get_claim_type(
    instance_id: str,
    predicate: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillClaimTypeView:
    return playbill_api.playbill_get_claim_type(
        resolve_server_instance_id(instance_id),
        predicate,
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.post(
    "/{instance_id}/playbill/claims/proposals",
    response_model=contracts.PlaybillClaimProposal,
)
async def propose_claim(
    instance_id: str,
    req: PlaybillProposeClaimRequest,
) -> contracts.PlaybillClaimProposal:
    return playbill_api.playbill_propose_claim(
        resolve_server_instance_id(instance_id),
        authoring=req.authoring,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.post(
    "/{instance_id}/playbill/claims/proposals/batch",
    response_model=contracts.PlaybillClaimBatchProposal,
)
async def propose_claims(
    instance_id: str,
    req: PlaybillProposeClaimsRequest,
) -> contracts.PlaybillClaimBatchProposal:
    return playbill_api.playbill_propose_claims(
        resolve_server_instance_id(instance_id),
        authorings=req.authorings,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents",
    response_model=contracts.PlaybillAuthoringIntentView,
)
async def create_authoring_intent(
    instance_id: str,
    req: PlaybillAuthoringCreateRequest | PlaybillAuthoringInputCreateRequest,
) -> contracts.PlaybillAuthoringIntentView:
    if isinstance(req, PlaybillAuthoringInputCreateRequest):
        return playbill_api.playbill_authoring_create_input(
            resolve_server_instance_id(instance_id), input=req.input
        )
    return playbill_api.playbill_authoring_create(
        resolve_server_instance_id(instance_id), payload=req.payload
    )


@router.get(
    "/{instance_id}/playbill/authoring/intents",
    response_model=contracts.PlaybillAuthoringIntentList,
)
async def list_pending_authoring_intents(
    instance_id: str,
) -> contracts.PlaybillAuthoringIntentList:
    return playbill_api.playbill_authoring_list_pending(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/authoring/compile",
    response_model=contracts.PlaybillAuthoringPreflightResult,
)
async def compile_authoring(
    instance_id: str,
    req: PlaybillAuthoringCompileRequest | PlaybillAuthoringInputCompileRequest,
) -> contracts.PlaybillAuthoringPreflightResult:
    if isinstance(req, PlaybillAuthoringInputCompileRequest):
        return playbill_api.playbill_authoring_compile_input(
            resolve_server_instance_id(instance_id),
            input=req.input,
            intent_id=req.intent_id,
        )
    return playbill_api.playbill_authoring_compile(
        resolve_server_instance_id(instance_id),
        payload=req.payload,
        intent_id=req.intent_id,
    )


@router.get(
    "/{instance_id}/playbill/authoring/intents/{intent_id}",
    response_model=contracts.PlaybillAuthoringIntentView,
)
async def get_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return playbill_api.playbill_authoring_get(resolve_server_instance_id(instance_id), intent_id)


@router.get(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/resume",
    response_model=contracts.PlaybillAuthoringIntentView,
)
async def resume_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return playbill_api.playbill_authoring_resume(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/rebase",
    response_model=contracts.PlaybillAuthoringIntentView,
)
async def rebase_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: PlaybillAuthoringRebaseRequest,
) -> contracts.PlaybillAuthoringIntentView:
    return playbill_api.playbill_authoring_rebase(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/preflight",
    response_model=contracts.PlaybillAuthoringPreflightResult,
)
async def preflight_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: PlaybillAuthoringPreflightRequest,
) -> contracts.PlaybillAuthoringPreflightResult:
    return playbill_api.playbill_authoring_preflight(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/submit",
    response_model=contracts.PlaybillAuthoringSubmitResult,
)
async def submit_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: PlaybillAuthoringSubmitRequest,
) -> contracts.PlaybillAuthoringSubmitResult:
    return playbill_api.playbill_authoring_submit(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.get(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/status",
    response_model=contracts.PlaybillCandidateStatus,
)
async def authoring_intent_status(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillCandidateStatus:
    return playbill_api.playbill_authoring_status(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/confirm",
    response_model=contracts.PlaybillInsertionConfirmResult,
)
async def confirm_authoring_insertion(
    instance_id: str,
    intent_id: str,
    req: PlaybillInsertionConfirmRequest,
) -> contracts.PlaybillInsertionConfirmResult:
    return playbill_api.playbill_authoring_confirm_insertion(
        resolve_server_instance_id(instance_id),
        intent_id,
        observation=req.observation,
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/abandon",
    response_model=contracts.PlaybillInsertionAbandonResult,
)
async def abandon_authoring_insertion(
    instance_id: str,
    intent_id: str,
    _req: PlaybillInsertionAbandonRequest,
) -> contracts.PlaybillInsertionAbandonResult:
    return playbill_api.playbill_authoring_abandon_insertion(
        resolve_server_instance_id(instance_id),
        intent_id,
    )


@router.get(
    "/{instance_id}/playbill/claims",
    response_model=contracts.PlaybillClaimList,
)
async def list_claims(
    instance_id: str,
    subject_path: str | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillClaimList:
    return playbill_api.playbill_list_claims(
        resolve_server_instance_id(instance_id),
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
        subject=(None if subject_path is None else SemanticAddress.whole_artifact(subject_path)),
        predicate=predicate,
        include_retired=include_retired,
    )


@router.get(
    "/{instance_id}/playbill/claims/{identity}",
    response_model=contracts.PlaybillClaimViewV2,
)
async def get_claim(
    instance_id: str,
    identity: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
    evaluation_time: datetime | None = None,
) -> contracts.PlaybillClaimViewV2:
    return playbill_api.playbill_get_claim(
        resolve_server_instance_id(instance_id),
        identity,
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
        evaluation_time=evaluation_time,
    )


@router.get(
    "/{instance_id}/playbill/claims/{identity}/history",
    response_model=contracts.PlaybillClaimHistory,
)
async def claim_history(
    instance_id: str,
    identity: str,
) -> contracts.PlaybillClaimHistory:
    return playbill_api.playbill_claim_history(resolve_server_instance_id(instance_id), identity)


@router.post(
    "/{instance_id}/playbill/claims/{identity}/explanation",
    response_model=contracts.PlaybillClaimExplanationV2,
)
async def explain_claim(
    instance_id: str,
    identity: str,
    req: PlaybillClaimExplainRequest,
) -> contracts.PlaybillClaimExplanationV2:
    return playbill_api.playbill_explain_claim(
        resolve_server_instance_id(instance_id),
        identity,
        at=req.at,
        evaluation_time=req.evaluation_time,
    )


@router.post(
    "/{instance_id}/playbill/queries/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
async def propose_query_definition(
    instance_id: str,
    req: PlaybillProposeQueryDefinitionRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_query_definition(
        resolve_server_instance_id(instance_id),
        query=req.query,
        proposal_name=req.proposal_name,
        base=req.base,
    )


@router.get(
    "/{instance_id}/playbill/queries",
    response_model=contracts.PlaybillQueryDefinitionList,
)
async def list_query_definitions(
    instance_id: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillQueryDefinitionList:
    return playbill_api.playbill_list_query_definitions(
        resolve_server_instance_id(instance_id),
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.get(
    "/{instance_id}/playbill/queries/{name}",
    response_model=contracts.PlaybillQueryDefinitionView,
)
async def get_query_definition(
    instance_id: str,
    name: str,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillQueryDefinitionView:
    return playbill_api.playbill_get_query_definition(
        resolve_server_instance_id(instance_id),
        name,
        at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
    )


@router.post(
    "/{instance_id}/playbill/queries/{name}/run",
    response_model=contracts.PlaybillQueryRun,
)
async def run_query(
    instance_id: str,
    name: str,
    req: PlaybillRunQueryRequest,
) -> contracts.PlaybillQueryRun:
    return playbill_api.playbill_run_query(
        resolve_server_instance_id(instance_id),
        name,
        at=req.at,
        evaluation_time=req.evaluation_time,
        parameters=req.parameters,
        budgets=req.budgets,
    )


@router.get(
    "/{instance_id}/playbill/procedures/{name}/readiness",
    response_model=contracts.PlaybillProcedureReadiness,
)
async def procedure_readiness(
    instance_id: str,
    name: str,
    evaluation_time: datetime,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillProcedureReadiness:
    return playbill_api.playbill_procedure_readiness(
        resolve_server_instance_id(instance_id),
        name,
        request=ProcedureReadinessRequestV1(
            at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
            evaluation_time=evaluation_time,
        ),
    )


@router.post(
    "/{instance_id}/playbill/procedures/{name}/bind",
    response_model=contracts.PlaybillProcedureBindResult,
)
async def bind_procedure(
    instance_id: str,
    name: str,
    req: ProcedureBindRequestV1,
) -> contracts.PlaybillProcedureBindResult:
    return playbill_api.playbill_procedure_bind(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/procedures/{name}/runs",
    response_model=contracts.PlaybillProcedureRunState,
)
async def run_procedure(
    instance_id: str,
    name: str,
    req: ProcedureRunRequestV1,
) -> contracts.PlaybillProcedureRunState:
    return playbill_api.playbill_procedure_run(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.get(
    "/{instance_id}/playbill/procedure-runs/{run_id}",
    response_model=contracts.PlaybillProcedureRunState,
)
async def procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.PlaybillProcedureRunState:
    return playbill_api.playbill_procedure_run_status(
        resolve_server_instance_id(instance_id),
        run_id,
    )


@router.post(
    "/{instance_id}/playbill/discover",
    response_model=contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory,
)
async def discover(
    instance_id: str,
    req: PlaybillDiscoverRequest,
) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
    return playbill_api.playbill_discover(
        resolve_server_instance_id(instance_id),
        query=req.query,
        entrypoint=req.entrypoint,
        at=req.at,
        evaluation_time=req.evaluation_time,
        profile=req.profile,
        budget=req.budget,
    )


@router.post(
    "/{instance_id}/playbill/search",
    response_model=contracts.PlaybillSearchResult,
)
async def search(
    instance_id: str,
    req: PlaybillSearchRequest,
) -> contracts.PlaybillSearchResult:
    return playbill_api.playbill_search(
        resolve_server_instance_id(instance_id),
        mode=req.mode,
        query=req.query,
        kinds=req.kinds,
        subject=req.subject,
        statuses=req.statuses,
        cursor=req.cursor,
        at=req.at,
        evaluation_time=req.evaluation_time,
        budgets=req.budgets,
    )


@router.post(
    "/{instance_id}/playbill/expand",
    response_model=contracts.PlaybillContextCapsule,
)
async def expand(
    instance_id: str,
    req: PlaybillExpandRequest,
) -> contracts.PlaybillContextCapsule:
    return playbill_api.playbill_expand(
        resolve_server_instance_id(instance_id),
        address=req.address,
        at=req.at,
        evaluation_time=req.evaluation_time,
        facets=req.facets,
        budget=req.budget,
    )


@router.post(
    "/{instance_id}/playbill/coverage/resolve",
    response_model=contracts.PlaybillCoverageResult,
)
async def resolve_coverage(
    instance_id: str,
    req: PlaybillResolveCoverageRequest,
) -> contracts.PlaybillCoverageResult:
    return playbill_api.playbill_resolve_coverage(
        resolve_server_instance_id(instance_id),
        observations=req.observations,
        at=req.at,
        budget=req.budget,
        scan_budget=req.scan_budget,
    )


@router.post(
    "/{instance_id}/playbill/floor/export",
    response_model=contracts.PlaybillFloorExport,
)
async def export_floor(
    instance_id: str,
    req: PlaybillFloorExportRequest,
) -> contracts.PlaybillFloorExport:
    return playbill_api.playbill_export_floor(resolve_server_instance_id(instance_id), at=req.at)


__all__ = ["router"]
