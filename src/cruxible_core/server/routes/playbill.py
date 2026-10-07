"""Cruxible Family-1 HTTP routes; all orchestration stays in the runtime/service core."""

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
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.floor import FloorDelta
from cruxible_client.contracts.get_reads import (
    GetBatchRequest,
    GetBatchResult,
    GetRequest,
    GetResult,
)
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitBuildResult,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.procedures.source_requests import (
    ProcedureSourcePreview,
    ProcedureSourcePreviewRequest,
)
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
)
from cruxible_client.contracts.write import (
    RetireRequest,
    SetRequest,
    WriteOutcome,
    WriteRequest,
)
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.admission import FLOOR_ADMISSION
from cruxible_core.server.config import resolve_server_settings
from cruxible_core.server.playbill_request_models import (
    ApprovalChallengeRequest,
    ApprovalRequest,
    AuditRequest,
    AuthoringInputCompileRequest,
    AuthoringInputSubmitRequest,
    AuthoringPreflightRequest,
    AuthoringRebaseRequest,
    AuthoringSubmitRequest,
    BlockDeclareRequest,
    BlockDepublishRequest,
    CompilerUpgradeRequest,
    CurationAcceptFixedRequest,
    CurationListRequest,
    CurationObserveRequest,
    CurationOverruleRequest,
    CurationSuppressRequest,
    CurationUnsuppressRequest,
    FloorDeltaRequest,
    FloorExportRequest,
    InitRequest,
    InstanceDecommissionRequest,
    LedgerMirrorClearRequest,
    LedgerMirrorRequest,
    LedgerPublishRequest,
    NextRequest,
    NextRequestV1,
    PlaybillAuthoringCompileRequest,
    PlaybillAuthoringCompileRequestV3,
    ProposalReadmitRequest,
    ProposalWithdrawRequest,
    ProposeClaimTypeInputRequest,
    ProposeClaimTypeRequest,
    ProposeDocumentRequest,
    ProposePrincipalRequest,
    ResolveCoverageRequest,
    ReviewRequest,
    SourceBundleRequest,
    SourceProposeRequest,
    StoreBodyRequest,
)
from cruxible_core.server.routes import resolve_server_instance_id
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureBindRequest,
    ProcedureReadinessRequestV1,
    ProcedureRunRequest,
)

router = APIRouter(prefix="/api/v1", tags=["state"])


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
        raise FormatError("accepted coordinate query requires all four coordinate fields")
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
    "/{instance_id}/init",
    response_model=contracts.InitResult,
    response_model_exclude={"git_workspace_note"},
)
def playbill_init(
    instance_id: str,
    req: InitRequest,
    request: Request,
) -> contracts.InitResult:
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
    "/{instance_id}/instance/decommission",
    response_model=contracts.InstanceDecommissionResult,
)
def instance_decommission(
    instance_id: str,
    req: InstanceDecommissionRequest,
) -> contracts.InstanceDecommissionResult:
    return playbill_api.playbill_instance_decommission(
        resolve_server_instance_id(instance_id),
        reason=req.reason,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/ledger/mirror",
    response_model=contracts.LedgerMirror,
)
def set_ledger_mirror(
    instance_id: str,
    req: LedgerMirrorRequest,
) -> contracts.LedgerMirror:
    return playbill_api.playbill_ledger_set_mirror(
        resolve_server_instance_id(instance_id),
        url=req.url,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/ledger/mirror/clear",
    response_model=contracts.LedgerMirrorCleared,
)
def clear_ledger_mirror(
    instance_id: str,
    req: LedgerMirrorClearRequest,
) -> contracts.LedgerMirrorCleared:
    return playbill_api.playbill_ledger_clear_mirror(
        resolve_server_instance_id(instance_id),
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/ledger/publish",
    response_model=contracts.LedgerMirror,
)
def publish_ledger(
    instance_id: str,
    req: LedgerPublishRequest,
) -> contracts.LedgerMirror:
    return playbill_api.playbill_ledger_publish(
        resolve_server_instance_id(instance_id),
        timeout=req.timeout,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.get("/{instance_id}/providers", response_model=ProviderCatalog)
def provider_catalog(instance_id: str) -> ProviderCatalog:
    return playbill_api.playbill_provider_catalog(resolve_server_instance_id(instance_id))


@router.post("/{instance_id}/providers/install", response_model=ProviderInstallResult)
def provider_install(instance_id: str, request: ProviderInstallRequest) -> ProviderInstallResult:
    return playbill_api.playbill_provider_install(resolve_server_instance_id(instance_id), request)


@router.post("/{instance_id}/kits/build", response_model=KitBuildResult)
def kit_build(instance_id: str, request: KitBuildRequest) -> KitBuildResult:
    return playbill_api.playbill_kit_build(resolve_server_instance_id(instance_id), request)


@router.get("/{instance_id}/kits", response_model=KitStatus)
def kit_status(instance_id: str) -> KitStatus:
    return playbill_api.playbill_kit_status(resolve_server_instance_id(instance_id))


@router.post("/{instance_id}/kits", response_model=KitChangeResult)
def kit_add(instance_id: str, request: KitAddRequest) -> KitChangeResult:
    return playbill_api.playbill_kit_add(resolve_server_instance_id(instance_id), request)


@router.post(
    "/{instance_id}/claim-types/upgrade",
    response_model=ClaimTypeUpgradeResult,
)
def claim_type_upgrade(
    instance_id: str, request: ClaimTypeUpgradeRequest
) -> ClaimTypeUpgradeResult:
    return playbill_api.playbill_claim_type_upgrade(
        resolve_server_instance_id(instance_id), request
    )


@router.post("/{instance_id}/kits/remove", response_model=KitChangeResult)
def kit_remove(instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
    return playbill_api.playbill_kit_remove(resolve_server_instance_id(instance_id), request)


@router.post(
    "/{instance_id}/bodies",
    response_model=contracts.CasObjectResult,
)
def store_body(
    instance_id: str,
    req: StoreBodyRequest,
) -> contracts.CasObjectResult:
    return playbill_api.playbill_store_body(
        resolve_server_instance_id(instance_id), content_base64=req.content_base64
    )


@router.post(
    "/{instance_id}/documents/proposals",
    response_model=contracts.ProposalInspection,
)
def propose_document(
    instance_id: str,
    req: ProposeDocumentRequest,
) -> contracts.ProposalInspection:
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
    "/{instance_id}/compiler/proposals",
    response_model=contracts.ProposalInspection,
)
def propose_compiler_upgrade(
    instance_id: str,
    req: CompilerUpgradeRequest,
) -> contracts.ProposalInspection:
    return playbill_api.playbill_propose_compiler_upgrade(
        resolve_server_instance_id(instance_id),
        target=req.target,
        base=req.base,
        proposal_name=req.proposal_name,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/principals/proposals",
    response_model=contracts.ProposalInspection,
)
def propose_principal(
    instance_id: str,
    req: ProposePrincipalRequest,
) -> contracts.ProposalInspection:
    return playbill_api.playbill_propose_principal_change(
        resolve_server_instance_id(instance_id),
        principal=req.principal,
        proposal_name=req.proposal_name,
        base=req.base,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.get(
    "/{instance_id}/whoami",
    response_model=contracts.WhoAmI,
)
def whoami(instance_id: str) -> contracts.WhoAmI:
    return playbill_api.playbill_whoami(resolve_server_instance_id(instance_id))


@router.get("/{instance_id}/head", response_model=contracts.Head)
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
) -> contracts.Head:
    coordinate = _coordinate(git_oid, semantic_root, generation_root, compiler_digest)
    if at is not None and coordinate is not None:
        raise FormatError("head takes at or the four coordinate fields, not both")
    return playbill_api.playbill_head(
        resolve_server_instance_id(instance_id), at=at if at is not None else coordinate
    )


@router.get(
    "/{instance_id}/orient",
    response_model=contracts.OrientResult,
)
def orient(
    instance_id: str,
    kind: str | None = Query(default=None, max_length=256),
    section: contracts.OrientSection | None = None,
    limit: int = Query(
        default=contracts.ORIENT_DEFAULT_LIMIT,
        ge=1,
        le=contracts.ORIENT_MAX_LIMIT,
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
    surface: contracts.OrientSurface = "cli",
    caller_tools: list[str] | None = Query(
        default=None,
        description=(
            "Advertised MCP repair tools; repeat for each tool, or send an empty value for none."
        ),
    ),
) -> contracts.OrientResult:
    coordinate = _coordinate(git_oid, semantic_root, generation_root, compiler_digest)
    if at is not None and coordinate is not None:
        raise FormatError("orient takes at or the four coordinate fields, not both")
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
    "/{instance_id}/proposals",
    response_model=contracts.ProposalList,
)
def list_proposals(
    instance_id: str,
    status: Literal["open", "settled", "incomplete"] | None = None,
    limit: int = Query(
        default=contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
        ge=1,
        le=contracts.PROPOSAL_LIST_MAX_LIMIT,
    ),
    cursor: str | None = Query(default=None, max_length=4096),
) -> contracts.ProposalList:
    return playbill_api.playbill_list_proposals(
        resolve_server_instance_id(instance_id),
        status=status,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/{instance_id}/proposal-selector",
    response_model=contracts.ProposalSelectorResult,
)
def resolve_proposal_selector(
    instance_id: str,
    selector: str,
) -> contracts.ProposalSelectorResult:
    return playbill_api.playbill_resolve_proposal_selector(
        resolve_server_instance_id(instance_id),
        selector,
    )


@router.post(
    "/{instance_id}/proposals/{proposal_id}/readmit",
    response_model=contracts.ProposalReadmitResult,
)
def readmit_proposal(
    instance_id: str,
    proposal_id: str,
    req: ProposalReadmitRequest,
) -> contracts.ProposalReadmitResult:
    return playbill_api.playbill_readmit_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/proposals/{proposal_id}/withdraw",
    response_model=contracts.ProposalWithdrawResult,
)
def withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    req: ProposalWithdrawRequest,
) -> contracts.ProposalWithdrawResult:
    return playbill_api.playbill_withdraw_proposal(
        resolve_server_instance_id(instance_id),
        proposal_id,
        req.reason,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/proposals/{proposal_id}/review",
    response_model=contracts.ProposalReview,
)
def review_proposal(
    instance_id: str,
    proposal_id: str,
    req: ReviewRequest,
) -> contracts.ProposalReview:
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
    "/{instance_id}/proposals/{proposal_id}/approval-challenge",
    response_model=contracts.ApprovalChallenge,
)
def prepare_approval(
    instance_id: str,
    proposal_id: str,
    req: ApprovalChallengeRequest,
) -> contracts.ApprovalChallenge:
    return playbill_api.playbill_prepare_approval(
        resolve_server_instance_id(instance_id),
        proposal_id,
        signer_id=req.signer_id,
        include_body=req.include_body,
    )


@router.post(
    "/{instance_id}/proposals/{proposal_id}/approvals",
    response_model=contracts.ApprovalReceipt,
    response_model_exclude={"git_workspace_note"},
)
# Synchronous, like every other mutating Cruxible route: this one now publishes
# the ledger to its mirror, and a blocking `git push` inside the event loop would
# let one unreachable remote stall every request the daemon is serving. The push
# has its own deadline as well; both bounds are needed, because a bounded stall
# on the loop is still a stall of the whole process.
def submit_approval(
    instance_id: str,
    proposal_id: str,
    req: ApprovalRequest,
) -> contracts.ApprovalReceipt:
    return playbill_api.playbill_submit_approval(
        resolve_server_instance_id(instance_id),
        proposal_id,
        attestation=req.attestation,
    )


@router.post(
    "/{instance_id}/proposals/{proposal_id}/activate",
    response_model=contracts.ActivationReceipt,
)
def activate_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.ActivationReceipt:
    return playbill_api.playbill_activate(resolve_server_instance_id(instance_id), proposal_id)


@router.post("/{instance_id}/captures/read", response_model=CaptureRead)
def read_capture(instance_id: str, request: CaptureReadRequest) -> CaptureRead:
    return playbill_api.playbill_read_capture(resolve_server_instance_id(instance_id), request)


@router.get(
    "/{instance_id}/sources/context",
    response_model=contracts.SourceContext,
)
def source_context(instance_id: str) -> contracts.SourceContext:
    return playbill_api.playbill_source_context(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/sources/check",
    response_model=contracts.SourceCheckResult,
)
def check_sources(
    instance_id: str,
    req: SourceBundleRequest,
) -> contracts.SourceCheckResult:
    return playbill_api.playbill_check_source_bundle(
        resolve_server_instance_id(instance_id), bundle=req.bundle
    )


@router.post(
    "/{instance_id}/sources/proposals",
    response_model=contracts.ProposalInspection,
)
def propose_sources(
    instance_id: str,
    req: SourceProposeRequest,
) -> contracts.ProposalInspection:
    return playbill_api.playbill_propose_source_bundle(
        resolve_server_instance_id(instance_id),
        bundle=req.bundle,
        source_name=req.source_name,
        proposal_name=req.proposal_name,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post(
    "/{instance_id}/claim-types/proposals",
    response_model=(contracts.ProposalInspection | contracts.ClaimTypeInputProposalResult),
)
def propose_claim_type(
    instance_id: str,
    req: ProposeClaimTypeRequest | ProposeClaimTypeInputRequest,
) -> contracts.ProposalInspection | contracts.ClaimTypeInputProposalResult:
    if isinstance(req, ProposeClaimTypeInputRequest):
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
    "/{instance_id}/claim-types/migrations",
    response_model=contracts.ClaimTypeMigrationResponse,
)
def migrate_claim_type(
    instance_id: str,
    req: ClaimTypeMigrationRequestAny,
) -> contracts.ClaimTypeMigrationResponse:
    return playbill_api.playbill_migrate_claim_type(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/claim-attestations",
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
    "/{instance_id}/claim-attestations/recover",
    response_model=None,
    status_code=204,
)
def recover_claim_attestations(instance_id: str) -> Response:
    playbill_api.playbill_recover_claim_attestations(
        resolve_server_instance_id(instance_id),
    )
    return Response(status_code=204)


@router.post(
    "/{instance_id}/predictions/query",
    response_model=contracts.ResolutionContractsResult,
)
def list_predictions(
    instance_id: str, req: contracts.ResolutionContractsRequest
) -> contracts.ResolutionContractsResult:
    return playbill_api.playbill_prediction_list(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/predictions",
    response_model=contracts.PredictResult,
)
def predict(
    instance_id: str,
    req: contracts.PredictRequest,
) -> contracts.PredictResult:
    return playbill_api.playbill_predict(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/predictions/{prediction_id}/settlements",
    response_model=contracts.SettleResult,
)
def settle_prediction(
    instance_id: str,
    prediction_id: str,
    req: contracts.SettleRequest,
) -> contracts.SettleResult:
    return playbill_api.playbill_settle_prediction(
        resolve_server_instance_id(instance_id),
        prediction_id,
        request=req,
    )


@router.get(
    "/{instance_id}/authoring/intents",
    response_model=contracts.AuthoringIntentListRecord,
)
def list_pending_authoring_intents(
    instance_id: str,
) -> contracts.AuthoringIntentListRecord:
    return playbill_api.playbill_authoring_list(resolve_server_instance_id(instance_id))


@router.post(
    "/{instance_id}/authoring/compile",
    response_model=contracts.AuthoringPreflightResult,
)
def compile_authoring(
    instance_id: str,
    req: (
        PlaybillAuthoringCompileRequest
        | PlaybillAuthoringCompileRequestV3
        | AuthoringInputCompileRequest
    ),
) -> contracts.AuthoringPreflightResult:
    if isinstance(req, AuthoringInputCompileRequest):
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
    return playbill_api.playbill_authoring_compile(
        resolve_server_instance_id(instance_id),
        payload=req.payload,
        intent_id=req.intent_id,
    )


@router.get(
    "/{instance_id}/authoring/intents/{intent_id}",
    response_model=contracts.AuthoringIntentViewRecord,
)
def get_authoring_intent(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    return playbill_api.playbill_authoring_get(resolve_server_instance_id(instance_id), intent_id)


@router.post(
    "/{instance_id}/authoring/intents/{intent_id}/rebase",
    response_model=contracts.AuthoringIntentViewRecord,
)
def rebase_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: AuthoringRebaseRequest,
) -> contracts.AuthoringIntentViewRecord:
    return playbill_api.playbill_authoring_rebase(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/authoring/intents/{intent_id}/preflight",
    response_model=contracts.AuthoringPreflightResult,
)
def preflight_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: AuthoringPreflightRequest,
) -> contracts.AuthoringPreflightResult:
    return playbill_api.playbill_authoring_preflight(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/authoring/submit",
    response_model=contracts.AuthoringSubmitResultRecord | contracts.AuthoringPreflightResult,
)
def compile_and_submit_authoring(
    instance_id: str,
    req: PlaybillAuthoringCompileRequestV3 | AuthoringInputSubmitRequest,
) -> contracts.AuthoringSubmitResultRecord | contracts.AuthoringPreflightResult:
    """Compile and submit one payload; a dry-run input answers with its preflight."""

    if isinstance(req, AuthoringInputSubmitRequest):
        if req.dry_run:
            return playbill_api.playbill_authoring_preview_input(
                resolve_server_instance_id(instance_id), input=req.input
            )
        return playbill_api.playbill_authoring_submit_input(
            resolve_server_instance_id(instance_id),
            input=req.input,
            intent_id=req.intent_id,
        )
    return playbill_api.playbill_authoring_compile_and_submit(
        resolve_server_instance_id(instance_id),
        payload=req.payload,
        reference_expectations=req.reference_expectations,
        program_stamp=req.program_stamp,
        intent_id=req.intent_id,
    )


@router.post(
    "/{instance_id}/authoring/intents/{intent_id}/submit",
    response_model=contracts.AuthoringSubmitResultRecord,
)
def submit_authoring_intent(
    instance_id: str,
    intent_id: str,
    _req: AuthoringSubmitRequest,
) -> contracts.AuthoringSubmitResultRecord:
    return playbill_api.playbill_authoring_submit(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.get(
    "/{instance_id}/authoring/intents/{intent_id}/status",
    response_model=contracts.CandidateStatusRecord,
)
def authoring_intent_status(
    instance_id: str,
    intent_id: str,
) -> contracts.CandidateStatusRecord:
    return playbill_api.playbill_authoring_status(
        resolve_server_instance_id(instance_id), intent_id
    )


@router.post(
    "/{instance_id}/blocks/declare",
    response_model=contracts.BlockDeclareResult,
)
def declare_playbill_block(
    instance_id: str,
    req: BlockDeclareRequest,
) -> contracts.BlockDeclareResult:
    return playbill_api.playbill_block_declare(
        resolve_server_instance_id(instance_id),
        req.stamp,
    )


@router.post(
    "/{instance_id}/blocks/depublish",
    response_model=contracts.BlockDepublishResult,
)
def depublish_playbill_block(
    instance_id: str,
    req: BlockDepublishRequest,
) -> contracts.BlockDepublishResult:
    return playbill_api.playbill_block_depublish(
        resolve_server_instance_id(instance_id),
        req.source_id,
        req.block_id,
        dry_run=req.dry_run,
        at=req.at,
    )


@router.post("/{instance_id}/claims/read-batch", response_model=ClaimReadBatchResult)
def read_claim_batch(instance_id: str, req: ClaimReadBatchRequest) -> ClaimReadBatchResult:
    return playbill_api.playbill_read_claim_batch(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post("/{instance_id}/get", response_model=GetResult)
def get_by_ref(instance_id: str, req: GetRequest) -> GetResult:
    """One governed thing by reference, values first; ``detail`` chooses the depth."""
    return playbill_api.playbill_get(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/get-batch", response_model=GetBatchResult)
def get_batch(instance_id: str, req: GetBatchRequest) -> GetBatchResult:
    """SDK-internal: several references at one coordinate (agents call get per reference)."""
    return playbill_api.playbill_get_batch(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/set", response_model=WriteOutcome)
def set_value(instance_id: str, req: SetRequest) -> WriteOutcome:
    """Put one value in one field of one Subject; a refusal is an outcome, not an error."""
    return playbill_api.playbill_set(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/retire", response_model=WriteOutcome)
def retire(instance_id: str, req: RetireRequest) -> WriteOutcome:
    """End one live Claim, named by ID or by its Subject and field."""
    return playbill_api.playbill_retire(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/write", response_model=WriteOutcome)
def write(instance_id: str, req: WriteRequest) -> WriteOutcome:
    """Apply set, add and retire changes as one change set."""
    return playbill_api.playbill_write(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/claims/backings", response_model=ClaimBackingsResult)
def read_claim_backings(instance_id: str, req: ClaimBackingsRequest) -> ClaimBackingsResult:
    return playbill_api.playbill_read_claim_backings(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/projections/check",
    response_model=contracts.ProjectionCheckResult,
)
def check_projection_blocks(
    instance_id: str, req: contracts.ProjectionCheckRequest
) -> contracts.ProjectionCheckResult:
    return playbill_api.playbill_check_projection_blocks(
        resolve_server_instance_id(instance_id), request=req
    )


@router.post(
    "/{instance_id}/projections/sync-backing",
    response_model=contracts.BlockSyncReadResult,
)
def read_block_sync_backing(
    instance_id: str,
    req: contracts.BlockSyncReadRequest,
) -> contracts.BlockSyncReadResult:
    return playbill_api.playbill_read_block_sync_backing(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post("/{instance_id}/query", response_model=contracts.QueryResultRecord)
def query_playbill(
    instance_id: str,
    req: contracts.QueryRequest,
) -> contracts.QueryResultRecord:
    return playbill_api.playbill_query(resolve_server_instance_id(instance_id), request=req)


@router.post("/{instance_id}/procedures/source/preview", response_model=ProcedureSourcePreview)
def procedure_source_preview(
    instance_id: str, req: ProcedureSourcePreviewRequest
) -> ProcedureSourcePreview:
    return playbill_api.playbill_procedure_source_preview(
        resolve_server_instance_id(instance_id), request=req
    )


@router.get(
    "/{instance_id}/procedures/{name}/readiness",
    response_model=contracts.ProcedureReadiness,
)
def procedure_readiness(
    instance_id: str,
    name: str,
    evaluation_time: datetime,
    git_oid: str | None = None,
    semantic_root: str | None = None,
    generation_root: str | None = None,
    compiler_digest: str | None = None,
) -> contracts.ProcedureReadiness:
    return playbill_api.playbill_procedure_readiness(
        resolve_server_instance_id(instance_id),
        name,
        request=ProcedureReadinessRequestV1(
            at=_coordinate(git_oid, semantic_root, generation_root, compiler_digest),
            evaluation_time=evaluation_time,
        ),
    )


@router.post(
    "/{instance_id}/procedures/{name}/bind",
    response_model=contracts.ProcedureBindResult,
)
def bind_procedure(
    instance_id: str,
    name: str,
    req: ProcedureBindRequest,
) -> contracts.ProcedureBindResult:
    return playbill_api.playbill_procedure_bind(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/procedures/{name}/runs",
    response_model=contracts.ProcedureRunState,
)
def run_procedure(
    instance_id: str,
    name: str,
    req: ProcedureRunRequest,
) -> contracts.ProcedureRunState:
    return playbill_api.playbill_procedure_run(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post("/{instance_id}/lines/{line}/check", response_model=contracts.LineTriggerCheckResult)
def check_line_trigger(
    instance_id: str, line: str, req: contracts.LineTriggerCheckRequest
) -> contracts.LineTriggerCheckResult:
    return playbill_api.playbill_line_check(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post("/{instance_id}/lines/{line}/arm", response_model=contracts.LineArm)
def arm_line(
    instance_id: str, line: str, req: ChangeControlRequest | None = None
) -> contracts.LineArm:
    control = req or ChangeControlRequest()
    return playbill_api.playbill_line_arm(
        resolve_server_instance_id(instance_id), line, dry_run=control.dry_run, at=control.at
    )


@router.post("/{instance_id}/lines/{line}/disarm", response_model=contracts.LineArm)
def disarm_line(
    instance_id: str, line: str, req: ChangeControlRequest | None = None
) -> contracts.LineArm:
    control = req or ChangeControlRequest()
    return playbill_api.playbill_line_disarm(
        resolve_server_instance_id(instance_id), line, dry_run=control.dry_run, at=control.at
    )


@router.get("/{instance_id}/lines/{line}/arm", response_model=contracts.LineArm)
def line_status(instance_id: str, line: str) -> contracts.LineArm:
    return playbill_api.playbill_line_status(resolve_server_instance_id(instance_id), line)


@router.post(
    "/{instance_id}/lines/{line}/evaluate",
    response_model=contracts.LineTriggerCheckResult,
)
def evaluate_line(
    instance_id: str, line: str, req: contracts.LineEvaluateRequest
) -> contracts.LineTriggerCheckResult:
    return playbill_api.playbill_line_evaluate(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post("/{instance_id}/lines/{line}/dispatch", response_model=contracts.LineDispatchResult)
def dispatch_line(
    instance_id: str, line: str, req: contracts.LineDispatchRequest
) -> contracts.LineDispatchResult:
    return playbill_api.playbill_line_dispatch(
        resolve_server_instance_id(instance_id), line, request=req
    )


@router.post(
    "/{instance_id}/lines/{line}/runs",
    response_model=contracts.ProcedureRunState,
)
def run_line(
    instance_id: str,
    line: str,
    req: LineRunRequest,
) -> contracts.ProcedureRunState:
    return playbill_api.playbill_line_run(
        resolve_server_instance_id(instance_id),
        line,
        request=req,
    )


@router.get(
    "/{instance_id}/procedure-runs/{run_id}",
    response_model=contracts.ProcedureRunState,
)
def procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.ProcedureRunState:
    return playbill_api.playbill_procedure_run_status(
        resolve_server_instance_id(instance_id),
        run_id,
    )


@router.post(
    "/{instance_id}/procedures/{name}/measurements",
    response_model=contracts.ProcedureMeasureResult,
)
def procedure_measure(
    instance_id: str,
    name: str,
    req: contracts.ProcedureMeasureRequest,
) -> contracts.ProcedureMeasureResult:
    return playbill_api.playbill_procedure_measure(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/procedures/{name}/readings",
    response_model=contracts.ProcedureReadingsResult,
)
def procedure_readings(
    instance_id: str,
    name: str,
    req: contracts.ProcedureReadingsRequest,
) -> contracts.ProcedureReadingsResult:
    return playbill_api.playbill_procedure_readings(
        resolve_server_instance_id(instance_id),
        name,
        request=req,
    )


@router.post(
    "/{instance_id}/next",
    response_model=contracts.NextResult,
)
def next_work(
    instance_id: str,
    req: NextRequestV1 | NextRequest,
) -> contracts.NextResult:
    return playbill_api.playbill_next(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/curation/list",
    response_model=contracts.CurationListResult,
)
def curation_list(
    instance_id: str,
    req: CurationListRequest,
) -> contracts.CurationListResult:
    return playbill_api.playbill_curation_list(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/audit",
    response_model=contracts.AuditResult,
)
def audit(
    instance_id: str,
    req: AuditRequest,
) -> contracts.AuditResult:
    return playbill_api.playbill_audit(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json", exclude_none=True),
    )


@router.post(
    "/{instance_id}/curation/overrule",
    response_model=contracts.CurationActionResult,
)
def curation_overrule(
    instance_id: str,
    req: CurationOverruleRequest,
) -> contracts.CurationActionResult:
    return playbill_api.playbill_curation_overrule(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/curation/accept-fixed",
    response_model=contracts.CurationActionResult,
)
def curation_accept_fixed(
    instance_id: str,
    req: CurationAcceptFixedRequest,
) -> contracts.CurationActionResult:
    return playbill_api.playbill_curation_accept_fixed(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/curation/suppress",
    response_model=contracts.CurationActionResult,
)
def curation_suppress(
    instance_id: str,
    req: CurationSuppressRequest,
) -> contracts.CurationActionResult:
    return playbill_api.playbill_curation_suppress(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/curation/unsuppress",
    response_model=contracts.CurationActionResult,
)
def curation_unsuppress(
    instance_id: str,
    req: CurationUnsuppressRequest,
) -> contracts.CurationActionResult:
    return playbill_api.playbill_curation_unsuppress(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/curation/observe",
    response_model=contracts.CurationObserveResult,
)
def curation_observe(
    instance_id: str,
    req: CurationObserveRequest,
) -> contracts.CurationObserveResult:
    return playbill_api.playbill_curation_observe(
        resolve_server_instance_id(instance_id),
        request=req.model_dump(mode="json"),
    )


@router.post(
    "/{instance_id}/since",
    response_model=contracts.SinceResult,
)
def since(
    instance_id: str,
    req: contracts.SinceRequest,
) -> contracts.SinceResult:
    return playbill_api.playbill_since(
        resolve_server_instance_id(instance_id),
        request=req,
    )


@router.post(
    "/{instance_id}/coverage/resolve",
    response_model=contracts.CoverageResult,
)
def resolve_coverage(
    instance_id: str,
    req: ResolveCoverageRequest,
) -> contracts.CoverageResult:
    return playbill_api.playbill_resolve_coverage(
        resolve_server_instance_id(instance_id),
        observations=req.observations,
        at=req.at,
        budget=req.budget,
        scan_budget=req.scan_budget,
    )


@router.post(
    "/{instance_id}/floor/export",
    response_model=contracts.FloorExport,
)
async def export_floor(
    instance_id: str,
    req: FloorExportRequest,
) -> contracts.FloorExport:
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

    def export() -> contracts.FloorExport:
        return playbill_api.playbill_export_floor(
            resolved,
            at=req.at,
            include=req.include,
        )

    async with FLOOR_ADMISSION.admit(resolved) as admitted:
        return await run_in_threadpool(admitted.run, export)


@router.post(
    "/{instance_id}/floor/delta",
    response_model=FloorDelta,
)
async def floor_delta(
    instance_id: str,
    req: FloorDeltaRequest,
) -> FloorDelta:
    """What brings the caller's floor at its base generation to the head.

    Admitted exactly like `export_floor`, under the same `FLOOR_ADMISSION`: an
    export, a delta and an in-process floor delivery of one instance never
    render at once, and a delta queued behind them waits on the loop, holding
    no worker thread.
    """

    resolved = await run_in_threadpool(resolve_server_instance_id, instance_id)

    def delta() -> FloorDelta:
        return playbill_api.playbill_floor_delta(
            resolved,
            at=req.at,
            base_generation=req.base_generation,
            base_renderer=req.base_renderer,
        )

    async with FLOOR_ADMISSION.admit(resolved) as admitted:
        return await run_in_threadpool(admitted.run, delta)


__all__ = ["router"]
