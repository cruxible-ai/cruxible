"""Playbill Family-1 HTTP routes; all orchestration stays in the runtime/service core."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from cruxible_client import contracts
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.change_control import ChangeControlRequest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
)
from cruxible_client.contracts.claim_reads import (
    ClaimBackingsRequest,
    ClaimBackingsResult,
    ClaimReadBatchRequest,
    ClaimReadBatchResult,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.evidence_rule_upgrade import (
    EvidenceRuleUpgradeRequest,
    EvidenceRuleUpgradeResult,
)
from cruxible_client.contracts.floor import PlaybillFloorDelta
from cruxible_client.contracts.get_reads import (
    PlaybillGetBatchRequest,
    PlaybillGetBatchResult,
    PlaybillGetRequest,
    PlaybillGetResult,
)
from cruxible_client.contracts.kits import (
    PlaybillKitAddRequest,
    PlaybillKitBuildRequest,
    PlaybillKitBuildResult,
    PlaybillKitChangeResult,
    PlaybillKitRemoveRequest,
    PlaybillKitStatus,
)
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_client.contracts.provider_installation import (
    PlaybillProviderCatalog,
    PlaybillProviderInstallRequest,
    PlaybillProviderInstallResult,
)
from cruxible_client.contracts.write import (
    PlaybillRetireRequest,
    PlaybillSetRequest,
    PlaybillWriteRequest,
    WriteOutcome,
)
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.admission import FLOOR_ADMISSION
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.server.playbill_request_models import (
    PlaybillApprovalChallengeRequest,
    PlaybillApprovalRequest,
    PlaybillAuditRequest,
    PlaybillAuthoringCompileRequest,
    PlaybillAuthoringCompileRequestV2,
    PlaybillAuthoringCompileRequestV3,
    PlaybillAuthoringCreateRequest,
    PlaybillAuthoringCreateRequestV2,
    PlaybillAuthoringCreateRequestV3,
    PlaybillAuthoringInputCompileRequest,
    PlaybillAuthoringInputCreateRequest,
    PlaybillAuthoringPreflightRequest,
    PlaybillAuthoringRebaseRequest,
    PlaybillAuthoringSubmitRequest,
    PlaybillBlockDeclareRequest,
    PlaybillBlockDepublishRequest,
    PlaybillCompilerUpgradeRequest,
    PlaybillCurationAcceptFixedRequest,
    PlaybillCurationListRequest,
    PlaybillCurationOverruleRequest,
    PlaybillCurationSuppressRequest,
    PlaybillFloorDeltaRequest,
    PlaybillFloorExportRequest,
    PlaybillInitRequest,
    PlaybillInsertionAbandonRequest,
    PlaybillInstanceDecommissionRequest,
    PlaybillLedgerMirrorRequest,
    PlaybillLedgerPublishRequest,
    PlaybillNextRequest,
    PlaybillNextRequestV1,
    PlaybillProposalReadmitRequest,
    PlaybillProposalWithdrawRequest,
    PlaybillProposeClaimTypeInputRequest,
    PlaybillProposeClaimTypeRequest,
    PlaybillProposeDocumentRequest,
    PlaybillProposePrincipalRequest,
    PlaybillResolveCoverageRequest,
    PlaybillReviewRequest,
    PlaybillSourceBundleRequest,
    PlaybillSourceProposeRequest,
    PlaybillStoreBodyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureBindRequest,
    ProcedureReadinessRequestV1,
    ProcedureRunRequest,
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


@router.post(
    "/{instance_id}/playbill/init",
    response_model=contracts.PlaybillInitResult,
    response_model_exclude={"git_workspace_note"},
)
def playbill_init(
    instance_id: str,
    req: PlaybillInitRequest,
    request: Request,
) -> contracts.PlaybillInitResult:
    return playbill_api.playbill_init(
        resolve_server_instance_id(instance_id),
        principals=req.principals,
        operating_profile=req.operating_profile,
        require_independent_approval=req.require_independent_approval,
        workspace_root=req.workspace_root,
        workspace_attachment_authorized=(
            request.scope.get("client") is None
            and resolve_server_settings().server_socket is not None
        ),
        git_object_format=req.git_object_format,
        mirror_url=req.mirror_url,
    )


@router.post(
    "/{instance_id}/playbill/instance/decommission",
    response_model=contracts.PlaybillInstanceDecommissionResult,
)
def instance_decommission(
    instance_id: str,
    req: PlaybillInstanceDecommissionRequest,
) -> contracts.PlaybillInstanceDecommissionResult:
    return playbill_api.playbill_instance_decommission(
        resolve_server_instance_id(instance_id),
        reason=req.reason,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/ledger/mirror",
    response_model=contracts.PlaybillLedgerMirror,
)
def set_ledger_mirror(
    instance_id: str,
    req: PlaybillLedgerMirrorRequest,
) -> contracts.PlaybillLedgerMirror:
    return playbill_api.playbill_ledger_set_mirror(
        resolve_server_instance_id(instance_id),
        url=req.url,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/ledger/publish",
    response_model=contracts.PlaybillLedgerMirror,
)
def publish_ledger(
    instance_id: str,
    req: PlaybillLedgerPublishRequest,
) -> contracts.PlaybillLedgerMirror:
    return playbill_api.playbill_ledger_publish(
        resolve_server_instance_id(instance_id),
        timeout=req.timeout,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.get(
    "/{instance_id}/playbill/ledger/mirror",
    response_model=contracts.PlaybillLedgerMirror,
)
def ledger_clone_url(instance_id: str) -> contracts.PlaybillLedgerMirror:
    return playbill_api.playbill_ledger_clone_url(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/playbill/providers", response_model=PlaybillProviderCatalog)
def provider_catalog(instance_id: str) -> PlaybillProviderCatalog:
    return playbill_api.playbill_provider_catalog(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/providers/install", response_model=PlaybillProviderInstallResult
)
def provider_install(
    instance_id: str, request: PlaybillProviderInstallRequest
) -> PlaybillProviderInstallResult:
    return playbill_api.playbill_provider_install(resolve_server_instance_id(instance_id), request)


@router.post("/{instance_id}/playbill/kits/build", response_model=PlaybillKitBuildResult)
def kit_build(instance_id: str, request: PlaybillKitBuildRequest) -> PlaybillKitBuildResult:
    return playbill_api.playbill_kit_build(resolve_server_instance_id(instance_id), request)


@router.get("/{instance_id}/playbill/kits", response_model=PlaybillKitStatus)
def kit_status(instance_id: str) -> PlaybillKitStatus:
    return playbill_api.playbill_kit_status(resolve_server_instance_id(instance_id))


@router.post("/{instance_id}/playbill/kits", response_model=PlaybillKitChangeResult)
def kit_add(instance_id: str, request: PlaybillKitAddRequest) -> PlaybillKitChangeResult:
    return playbill_api.playbill_kit_add(resolve_server_instance_id(instance_id), request)


@router.post(
    "/{instance_id}/playbill/claim-types/evidence-rules/upgrade",
    response_model=EvidenceRuleUpgradeResult,
)
def evidence_rules_upgrade(
    instance_id: str, request: EvidenceRuleUpgradeRequest
) -> EvidenceRuleUpgradeResult:
    return playbill_api.playbill_evidence_rules_upgrade(
        resolve_server_instance_id(instance_id), request
    )


@router.post(
    "/{instance_id}/playbill/claim-types/upgrade",
    response_model=ClaimTypeUpgradeResult,
)
def claim_type_upgrade(
    instance_id: str, request: ClaimTypeUpgradeRequest
) -> ClaimTypeUpgradeResult:
    return playbill_api.playbill_claim_type_upgrade(
        resolve_server_instance_id(instance_id), request
    )


@router.post("/{instance_id}/playbill/kits/remove", response_model=PlaybillKitChangeResult)
def kit_remove(instance_id: str, request: PlaybillKitRemoveRequest) -> PlaybillKitChangeResult:
    return playbill_api.playbill_kit_remove(resolve_server_instance_id(instance_id), request)


@router.post(
    "/{instance_id}/playbill/bodies",
    response_model=contracts.PlaybillCasObjectResult,
)
def store_body(
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
def propose_document(
    instance_id: str,
    req: PlaybillProposeDocumentRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_document(
        resolve_server_instance_id(instance_id),
        shell=req.shell,
        proposal_name=req.proposal_name,
        source_compilation_digest=req.source_compilation_digest,
        base=req.base,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/compiler/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
def propose_compiler_upgrade(
    instance_id: str,
    req: PlaybillCompilerUpgradeRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_compiler_upgrade(
        resolve_server_instance_id(instance_id),
        target=req.target,
        base=req.base,
        proposal_name=req.proposal_name,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/principals/proposals",
    response_model=contracts.PlaybillProposalInspection,
)
def propose_principal(
    instance_id: str,
    req: PlaybillProposePrincipalRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_principal_change(
        resolve_server_instance_id(instance_id),
        principal=req.principal,
        proposal_name=req.proposal_name,
        base=req.base,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.get(
    "/{instance_id}/playbill/whoami",
    response_model=contracts.PlaybillWhoAmI,
)
def whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
    return playbill_api.playbill_whoami(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/playbill/head", response_model=contracts.PlaybillHead)
def head(
    instance_id: str,
    at: str | None = Query(
        default=None,
        pattern=r"^[0-9a-f]{1,64}$",
        description="An accepted generation's Git OID, or a unique prefix of 12+ hex.",
    ),
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.PlaybillHead:
    coordinate = _coordinate(git_oid, semantic_root, generation_root, compiler_digest)
    if at is not None and coordinate is not None:
        raise PlaybillFormatError("head takes at or the four coordinate fields, not both")
    return playbill_api.playbill_head(
        resolve_server_instance_id(instance_id), at=at if at is not None else coordinate
    )


@router.get(
    "/{instance_id}/playbill/orient",
    response_model=contracts.PlaybillOrientResult,
)
def orient(
    instance_id: str,
    kind: str | None = Query(default=None, max_length=256),
    section: contracts.PlaybillOrientSection | None = None,
    limit: int = Query(
        default=contracts.PLAYBILL_ORIENT_DEFAULT_LIMIT,
        ge=1,
        le=contracts.PLAYBILL_ORIENT_MAX_LIMIT,
    ),
    cursor: str | None = Query(default=None, max_length=4096),
    at: str | None = Query(
        default=None,
        pattern=r"^[0-9a-f]{1,64}$",
        description=(
            "An accepted generation's Git OID, or a unique prefix of at least 12 hex "
            "characters; the four coordinate fields also pin one."
        ),
    ),
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
    evaluation_time: datetime | None = None,
    surface: contracts.PlaybillOrientSurface = "cli",
    caller_tools: list[str] | None = Query(
        default=None,
        description=(
            "Advertised MCP repair tools; repeat for each tool, or send an empty value for none."
        ),
    ),
) -> contracts.PlaybillOrientResult:
    coordinate = _coordinate(git_oid, semantic_root, generation_root, compiler_digest)
    if at is not None and coordinate is not None:
        raise PlaybillFormatError("orient takes at or the four coordinate fields, not both")
    return playbill_api.playbill_orient(
        resolve_server_instance_id(instance_id),
        kind=kind,
        section=section,
        limit=limit,
        cursor=cursor,
        at=at if at is not None else coordinate,
        evaluation_time=evaluation_time,
        surface=surface,
        caller_tools=None if caller_tools is None else tuple(name for name in caller_tools if name),
    )


@router.get(
    "/{instance_id}/playbill/proposals",
    response_model=contracts.PlaybillProposalList,
)
def list_proposals(
    instance_id: str,
    status: Literal["open", "settled", "incomplete"] | None = None,
    limit: int = Query(
        default=contracts.PLAYBILL_PROPOSAL_LIST_DEFAULT_LIMIT,
        ge=1,
        le=contracts.PLAYBILL_PROPOSAL_LIST_MAX_LIMIT,
    ),
    cursor: str | None = Query(default=None, max_length=4096),
) -> contracts.PlaybillProposalList:
    return playbill_api.playbill_list_proposals(
        resolve_server_instance_id(instance_id),
        status=status,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/{instance_id}/playbill/proposal-selector",
    response_model=contracts.PlaybillProposalSelectorResult,
)
def resolve_proposal_selector(
    instance_id: str,
    selector: str,
) -> contracts.PlaybillProposalSelectorResult:
    return playbill_api.playbill_resolve_proposal_selector(
        resolve_server_instance_id(instance_id),
        selector,
    )


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}",
    response_model=contracts.PlaybillProposalInspection,
)
def inspect_proposal(
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
def readmit_proposal(
    instance_id: str,
    proposal_id: str,
    req: PlaybillProposalReadmitRequest,
) -> contracts.PlaybillProposalReadmitResult:
    return playbill_api.playbill_readmit_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/withdraw",
    response_model=contracts.PlaybillProposalWithdrawResult,
)
def withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    req: PlaybillProposalWithdrawRequest,
) -> contracts.PlaybillProposalWithdrawResult:
    return playbill_api.playbill_withdraw_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        req.reason,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}/status",
    response_model=contracts.PlaybillProposalListEntry,
)
def proposal_status(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalListEntry:
    return playbill_api.playbill_proposal_status(
        resolve_server_instance_id(instance_id), proposal_id
    )


@router.get(
    "/{instance_id}/playbill/proposals/{proposal_id}/refusal",
    response_model=contracts.PlaybillRefusalInspection,
)
def inspect_refusal(
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
def review_proposal(
    instance_id: str,
    proposal_id: str,
    req: PlaybillReviewRequest,
) -> contracts.PlaybillProposalReview:
    return playbill_api.playbill_review_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        include_body=req.include_body,
        workspace_observation=(
            None
            if req.workspace_observation is None
            else req.workspace_observation.model_dump(mode="json")
        ),
    )


@router.post(
    "/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
    response_model=contracts.PlaybillApprovalChallenge,
)
def prepare_approval(
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
    response_model_exclude={"git_workspace_note"},
)
# Synchronous, like every other mutating Playbill route: this one now publishes
# the ledger to its mirror, and a blocking `git push` inside the event loop would
# let one unreachable remote stall every request the daemon is serving. The push
# has its own deadline as well; both bounds are needed, because a bounded stall
# on the loop is still a stall of the whole process.
def submit_approval(
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
def activate_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillActivationReceipt:
    return playbill_api.playbill_activate(resolve_server_instance_id(instance_id), proposal_id)


@router.post("/{instance_id}/playbill/captures/read", response_model=CaptureRead)
def read_capture(instance_id: str, request: CaptureReadRequest) -> CaptureRead:
    return playbill_api.playbill_read_capture(resolve_server_instance_id(instance_id), request)


@router.get(
    "/{instance_id}/playbill/sources/context",
    response_model=contracts.PlaybillSourceContext,
)
def source_context(instance_id: str) -> contracts.PlaybillSourceContext:
    return playbill_api.playbill_source_context(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/sources/check",
    response_model=contracts.PlaybillSourceCheckResult,
)
def check_sources(
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
def propose_sources(
    instance_id: str,
    req: PlaybillSourceProposeRequest,
) -> contracts.PlaybillProposalInspection:
    return playbill_api.playbill_propose_source_bundle(
        resolve_server_instance_id(instance_id),
        bundle=req.bundle,
        source_name=req.source_name,
        proposal_name=req.proposal_name,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/claim-types/proposals",
    response_model=(
        contracts.PlaybillProposalInspection | contracts.PlaybillClaimTypeInputProposalResult
    ),
)
def propose_claim_type(
    instance_id: str,
    req: PlaybillProposeClaimTypeRequest | PlaybillProposeClaimTypeInputRequest,
) -> contracts.PlaybillProposalInspection | contracts.PlaybillClaimTypeInputProposalResult:
    if isinstance(req, PlaybillProposeClaimTypeInputRequest):
        return playbill_api.playbill_propose_claim_type_input(
            resolve_server_instance_id(instance_id),
            input=req.input,
            proposal_name=req.proposal_name,
            dry_run=req.dry_run,
            at=req.at,
        )
    return playbill_api.playbill_propose_claim_type(
        resolve_server_instance_id(instance_id),
        claim_type=req.claim_type,
        proposal_name=req.proposal_name,
        base=req.base,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/playbill/claim-types/migrations",
    response_model=contracts.PlaybillClaimTypeMigrationResponse,
)
def migrate_claim_type(
    instance_id: str,
    req: ClaimTypeMigrationRequestAny,
) -> contracts.PlaybillClaimTypeMigrationResponse:
    return playbill_api.playbill_migrate_claim_type(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/claim-attestations",
    response_model=ClaimAttestationAppendResult,
)
def append_claim_attestation(
    instance_id: str,
    req: ClaimAttestationAppendRequest,
) -> ClaimAttestationAppendResult:
    return playbill_api.playbill_append_claim_attestation(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/claim-attestations/recover",
    response_model=None,
    status_code=204,
)
def recover_claim_attestations(instance_id: str) -> Response:
    playbill_api.playbill_recover_claim_attestations(
        resolve_server_instance_id(instance_id),
    )
    return Response(status_code=204)


@router.post(
    "/{instance_id}/playbill/resolution-contracts/query",
    response_model=contracts.ResolutionContractsResult,
)
def resolution_contracts(
    instance_id: str, req: contracts.ResolutionContractsRequest
) -> contracts.ResolutionContractsResult:
    return playbill_api.playbill_resolution_contracts(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/playbill/predictions",
    response_model=contracts.PlaybillPredictResult,
)
def predict(
    instance_id: str,
    req: contracts.PlaybillPredictRequest,
) -> contracts.PlaybillPredictResult:
    return playbill_api.playbill_predict(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/predictions/{prediction_id}/settlements",
    response_model=contracts.PlaybillSettleResult,
)
def settle_prediction(
    instance_id: str,
    prediction_id: str,
    req: contracts.PlaybillSettleRequest,
) -> contracts.PlaybillSettleResult:
    return playbill_api.playbill_settle_prediction(
        resolve_server_instance_id(instance_id),
        prediction_id,
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents",
    response_model=contracts.PlaybillAuthoringIntentView,
)
def create_authoring_intent(
    instance_id: str,
    req: (
        PlaybillAuthoringCreateRequest
        | PlaybillAuthoringCreateRequestV2
        | PlaybillAuthoringCreateRequestV3
        | PlaybillAuthoringInputCreateRequest
    ),
) -> contracts.PlaybillAuthoringIntentView:
    if isinstance(req, PlaybillAuthoringInputCreateRequest):
        return playbill_api.playbill_authoring_create_input(
            resolve_server_instance_id(instance_id), input=req.input
        )
    if isinstance(req, PlaybillAuthoringCreateRequestV3):
        return playbill_api.playbill_authoring_create(
            resolve_server_instance_id(instance_id),
            payload=req.payload,
            reference_expectations=req.reference_expectations,
            program_stamp=req.program_stamp,
        )
    if isinstance(req, PlaybillAuthoringCreateRequestV2):
        return playbill_api.playbill_authoring_create(
            resolve_server_instance_id(instance_id),
            payload=req.payload,
            reference_expectations=req.reference_expectations,
        )
    return playbill_api.playbill_authoring_create(
        resolve_server_instance_id(instance_id),
        payload=req.payload,
    )


@router.get(
    "/{instance_id}/playbill/authoring/intents",
    response_model=contracts.PlaybillAuthoringIntentList,
)
def list_pending_authoring_intents(
    instance_id: str,
) -> contracts.PlaybillAuthoringIntentList:
    return playbill_api.playbill_authoring_list_pending(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/playbill/authoring/compile",
    response_model=contracts.PlaybillAuthoringPreflightResult,
)
def compile_authoring(
    instance_id: str,
    req: (
        PlaybillAuthoringCompileRequest
        | PlaybillAuthoringCompileRequestV2
        | PlaybillAuthoringCompileRequestV3
        | PlaybillAuthoringInputCompileRequest
    ),
) -> contracts.PlaybillAuthoringPreflightResult:
    if isinstance(req, PlaybillAuthoringInputCompileRequest):
        return playbill_api.playbill_authoring_compile_input(
            resolve_server_instance_id(instance_id),
            input=req.input,
            intent_id=req.intent_id,
        )
    if isinstance(req, PlaybillAuthoringCompileRequestV3):
        return playbill_api.playbill_authoring_compile(
            resolve_server_instance_id(instance_id),
            payload=req.payload,
            intent_id=req.intent_id,
            reference_expectations=req.reference_expectations,
            program_stamp=req.program_stamp,
        )
    if isinstance(req, PlaybillAuthoringCompileRequestV2):
        return playbill_api.playbill_authoring_compile(
            resolve_server_instance_id(instance_id),
            payload=req.payload,
            intent_id=req.intent_id,
            reference_expectations=req.reference_expectations,
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
def get_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return playbill_api.playbill_authoring_get(resolve_server_instance_id(instance_id), intent_id)


@router.get(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/resume",
    response_model=contracts.PlaybillAuthoringIntentView,
)
def resume_authoring_intent(
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
def rebase_authoring_intent(
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
def preflight_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: PlaybillAuthoringPreflightRequest,
) -> contracts.PlaybillAuthoringPreflightResult:
    return playbill_api.playbill_authoring_preflight(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/submit",
    response_model=contracts.PlaybillAuthoringSubmitResult,
)
def compile_and_submit_authoring(
    instance_id: str,
    req: PlaybillAuthoringCompileRequestV3,
) -> contracts.PlaybillAuthoringSubmitResult:
    return playbill_api.playbill_authoring_compile_and_submit(
        resolve_server_instance_id(instance_id),
        payload=req.payload,
        reference_expectations=req.reference_expectations,
        program_stamp=req.program_stamp,
        intent_id=req.intent_id,
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/submit",
    response_model=contracts.PlaybillAuthoringSubmitResult,
)
def submit_authoring_intent(
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
def authoring_intent_status(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillCandidateStatus:
    return playbill_api.playbill_authoring_status(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/playbill/authoring/intents/{intent_id}/insertion/abandon",
    response_model=contracts.PlaybillInsertionAbandonResult,
)
def abandon_authoring_insertion(
    instance_id: str,
    intent_id: str,
    req: PlaybillInsertionAbandonRequest,
) -> contracts.PlaybillInsertionAbandonResult:
    return playbill_api.playbill_authoring_abandon_insertion(
        resolve_server_instance_id(instance_id),
        intent_id,
        expectation_id=req.expectation_id,
    )


@router.post(
    "/{instance_id}/playbill/blocks/declare",
    response_model=contracts.PlaybillBlockDeclareResult,
)
def declare_playbill_block(
    instance_id: str,
    req: PlaybillBlockDeclareRequest,
) -> contracts.PlaybillBlockDeclareResult:
    return playbill_api.playbill_block_declare(
        resolve_server_instance_id(instance_id),
        req.stamp,
    )


@router.post(
    "/{instance_id}/playbill/blocks/depublish",
    response_model=contracts.PlaybillBlockDepublishResult,
)
def depublish_playbill_block(
    instance_id: str,
    req: PlaybillBlockDepublishRequest,
) -> contracts.PlaybillBlockDepublishResult:
    return playbill_api.playbill_block_depublish(
        resolve_server_instance_id(instance_id),
        req.source_id,
        req.block_id,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post("/{instance_id}/playbill/claims/read-batch", response_model=ClaimReadBatchResult)
def read_claim_batch(instance_id: str, req: ClaimReadBatchRequest) -> ClaimReadBatchResult:
    return playbill_api.playbill_read_claim_batch(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post("/{instance_id}/playbill/get", response_model=PlaybillGetResult)
def get_by_ref(instance_id: str, req: PlaybillGetRequest) -> PlaybillGetResult:
    """One governed thing by reference, values first; ``detail`` chooses the depth."""
    return playbill_api.playbill_get(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/playbill/get-batch", response_model=PlaybillGetBatchResult)
def get_batch(instance_id: str, req: PlaybillGetBatchRequest) -> PlaybillGetBatchResult:
    """SDK-internal: several references at one coordinate (agents call get per reference)."""
    return playbill_api.playbill_get_batch(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/playbill/set", response_model=WriteOutcome)
def set_value(instance_id: str, req: PlaybillSetRequest) -> WriteOutcome:
    """Put one value in one field of one Subject; a refusal is an outcome, not an error."""
    return playbill_api.playbill_set(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/playbill/retire", response_model=WriteOutcome)
def retire(instance_id: str, req: PlaybillRetireRequest) -> WriteOutcome:
    """End one live Claim, named by ID or by its Subject and field."""
    return playbill_api.playbill_retire(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/playbill/write", response_model=WriteOutcome)
def write(instance_id: str, req: PlaybillWriteRequest) -> WriteOutcome:
    """Apply set, add and retire changes as one change set."""
    return playbill_api.playbill_write(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/playbill/claims/backings", response_model=ClaimBackingsResult)
def read_claim_backings(instance_id: str, req: ClaimBackingsRequest) -> ClaimBackingsResult:
    return playbill_api.playbill_read_claim_backings(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/playbill/projections/check",
    response_model=contracts.PlaybillProjectionCheckResult,
)
def check_projection_blocks(
    instance_id: str, req: contracts.PlaybillProjectionCheckRequest
) -> contracts.PlaybillProjectionCheckResult:
    return playbill_api.playbill_check_projection_blocks(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/playbill/projections/sync-backing",
    response_model=contracts.PlaybillBlockSyncReadResult,
)
def read_block_sync_backing(
    instance_id: str,
    req: contracts.PlaybillBlockSyncReadRequest,
) -> contracts.PlaybillBlockSyncReadResult:
    return playbill_api.playbill_read_block_sync_backing(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post("/{instance_id}/playbill/query", response_model=contracts.PlaybillQueryResult)
def query_playbill(
    instance_id: str,
    req: contracts.PlaybillQueryRequest,
) -> contracts.PlaybillQueryResult:
    return playbill_api.playbill_query(resolve_server_instance_id(instance_id), request=req)


@router.post(
    "/{instance_id}/playbill/procedures/source/preview", response_model=ProcedureSourcePreview
)
def procedure_source_preview(
    instance_id: str, req: ProcedureSourcePreviewRequest
) -> ProcedureSourcePreview:
    return playbill_api.playbill_procedure_source_preview(
        resolve_server_instance_id(instance_id), request=req
    )


@router.get(
    "/{instance_id}/playbill/procedures/{name}/readiness",
    response_model=contracts.PlaybillProcedureReadiness,
)
def procedure_readiness(
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
def bind_procedure(
    instance_id: str,
    name: str,
    req: ProcedureBindRequest,
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
def run_procedure(
    instance_id: str,
    name: str,
    req: ProcedureRunRequest,
) -> contracts.PlaybillProcedureRunState:
    return playbill_api.playbill_procedure_run(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/lines/{line}/check", response_model=contracts.LineTriggerCheckResult
)
def check_line_trigger(
    instance_id: str, line: str, req: contracts.LineTriggerCheckRequest
) -> contracts.LineTriggerCheckResult:
    return playbill_api.playbill_line_check(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post("/{instance_id}/playbill/lines/{line}/arm", response_model=contracts.LineArm)
def arm_line(
    instance_id: str, line: str, req: ChangeControlRequest | None = None
) -> contracts.LineArm:
    control = req or ChangeControlRequest()
    return playbill_api.playbill_line_arm(
        resolve_server_instance_id(instance_id), line, dry_run=control.dry_run, at=control.at
    )


@router.post("/{instance_id}/playbill/lines/{line}/disarm", response_model=contracts.LineArm)
def disarm_line(
    instance_id: str, line: str, req: ChangeControlRequest | None = None
) -> contracts.LineArm:
    control = req or ChangeControlRequest()
    return playbill_api.playbill_line_disarm(
        resolve_server_instance_id(instance_id), line, dry_run=control.dry_run, at=control.at
    )


@router.get("/{instance_id}/playbill/lines/{line}/arm", response_model=contracts.LineArm)
def line_status(instance_id: str, line: str) -> contracts.LineArm:
    return playbill_api.playbill_line_status(resolve_server_instance_id(instance_id), line)


@router.post(
    "/{instance_id}/playbill/lines/{line}/evaluate",
    response_model=contracts.LineTriggerCheckResult,
)
def evaluate_line(
    instance_id: str, line: str, req: contracts.LineEvaluateRequest
) -> contracts.LineTriggerCheckResult:
    return playbill_api.playbill_line_evaluate(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post(
    "/{instance_id}/playbill/lines/{line}/dispatch", response_model=contracts.LineDispatchResult
)
def dispatch_line(
    instance_id: str, line: str, req: contracts.LineDispatchRequest
) -> contracts.LineDispatchResult:
    return playbill_api.playbill_line_dispatch(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post(
    "/{instance_id}/playbill/lines/{line}/runs",
    response_model=contracts.PlaybillProcedureRunState,
)
def run_line(
    instance_id: str,
    line: str,
    req: LineRunRequest,
) -> contracts.PlaybillProcedureRunState:
    return playbill_api.playbill_line_run(
        resolve_server_instance_id(instance_id),
        line,
        request=req,
    )


@router.get(
    "/{instance_id}/playbill/procedure-runs/{run_id}",
    response_model=contracts.PlaybillProcedureRunState,
)
def procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.PlaybillProcedureRunState:
    return playbill_api.playbill_procedure_run_status(
        resolve_server_instance_id(instance_id),
        run_id,
    )


@router.post(
    "/{instance_id}/playbill/procedures/{name}/measurements",
    response_model=contracts.PlaybillProcedureMeasureResult,
)
def procedure_measure(
    instance_id: str,
    name: str,
    req: contracts.PlaybillProcedureMeasureRequest,
) -> contracts.PlaybillProcedureMeasureResult:
    return playbill_api.playbill_procedure_measure(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/procedures/{name}/readings",
    response_model=contracts.PlaybillProcedureReadingsResult,
)
def procedure_readings(
    instance_id: str,
    name: str,
    req: contracts.PlaybillProcedureReadingsRequest,
) -> contracts.PlaybillProcedureReadingsResult:
    return playbill_api.playbill_procedure_readings(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/next",
    response_model=contracts.PlaybillNextResult,
)
def next_work(
    instance_id: str,
    req: PlaybillNextRequestV1 | PlaybillNextRequest,
) -> contracts.PlaybillNextResult:
    return playbill_api.playbill_next(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/playbill/curation/list",
    response_model=contracts.PlaybillCurationListResult,
)
def curation_list(
    instance_id: str,
    req: PlaybillCurationListRequest,
) -> contracts.PlaybillCurationListResult:
    return playbill_api.playbill_curation_list(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/playbill/audit",
    response_model=contracts.PlaybillAuditResult,
)
def audit(
    instance_id: str,
    req: PlaybillAuditRequest,
) -> contracts.PlaybillAuditResult:
    return playbill_api.playbill_audit(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/playbill/curation/overrule",
    response_model=contracts.PlaybillCurationActionResult,
)
def curation_overrule(
    instance_id: str,
    req: PlaybillCurationOverruleRequest,
) -> contracts.PlaybillCurationActionResult:
    return playbill_api.playbill_curation_overrule(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/playbill/curation/accept-fixed",
    response_model=contracts.PlaybillCurationActionResult,
)
def curation_accept_fixed(
    instance_id: str,
    req: PlaybillCurationAcceptFixedRequest,
) -> contracts.PlaybillCurationActionResult:
    return playbill_api.playbill_curation_accept_fixed(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/playbill/curation/suppress",
    response_model=contracts.PlaybillCurationActionResult,
)
def curation_suppress(
    instance_id: str,
    req: PlaybillCurationSuppressRequest,
) -> contracts.PlaybillCurationActionResult:
    return playbill_api.playbill_curation_suppress(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/playbill/since",
    response_model=contracts.PlaybillSinceResult,
)
def since(
    instance_id: str,
    req: contracts.PlaybillSinceRequest,
) -> contracts.PlaybillSinceResult:
    return playbill_api.playbill_since(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/playbill/coverage/resolve",
    response_model=contracts.PlaybillCoverageResult,
)
def resolve_coverage(
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
    # An export is the daemon's heaviest read (hundreds of MB of working set on
    # real state). Exports of one instance are admitted one at a time, so a
    # second export of the same head is answered from the first one's memo
    # instead of doubling the working set. `FLOOR_ADMISSION` is the one floor
    # admission per instance, shared with the delta route and with in-process
    # floor delivery on consumer threads, so no two floor renders of an
    # instance overlap. Admission waits on the event loop, not in a worker
    # thread, so queued exports never hold threadpool capacity that cheap
    # reads, lifecycle routes and other instances need; the export itself runs
    # in the threadpool through the admission ticket, which keeps the instance
    # held until the export ends even if this route is cancelled mid-call. This
    # is the one shape of async route the event-loop guardrail allows: await
    # admission, then offload through its ticket.
    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def export() -> contracts.PlaybillFloorExport:
        return playbill_api.playbill_export_floor(
            resolved,
            at=req.at,
            format_version=req.format_version,
            include=req.include,
            review_notes_oid=req.review_notes_oid,
        )

    async with FLOOR_ADMISSION.admit(resolved) as admitted:
        return await run_in_threadpool(admitted.run, export)


@router.post(
    "/{instance_id}/playbill/floor/delta",
    response_model=PlaybillFloorDelta,
)
async def floor_delta(
    instance_id: str,
    req: PlaybillFloorDeltaRequest,
) -> PlaybillFloorDelta:
    """What brings the caller's floor at its base generation to the head.

    Admitted exactly like `export_floor`, under the same `FLOOR_ADMISSION`: an
    export, a delta and an in-process floor delivery of one instance never
    render at once, and a delta queued behind them waits on the loop, holding
    no worker thread.
    """

    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def delta() -> PlaybillFloorDelta:
        return playbill_api.playbill_floor_delta(
            resolved,
            at=req.at,
            base_generation=req.base_generation,
            base_renderer=req.base_renderer,
        )

    async with FLOOR_ADMISSION.admit(resolved) as admitted:
        return await run_in_threadpool(admitted.run, delta)


__all__ = ["router"]
