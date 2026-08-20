"""Playbill-only MCP handler implementations."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

from cruxible_client import CruxibleClient, contracts
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.claim_types import ClaimType
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.discovery import DiscoveryBudgetV1, ExpansionBudgetV1
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.subjects import SubjectShell
from cruxible_core.playbill.types import PrincipalRecord
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1
from cruxible_core.temporal import parse_datetime

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None] | None = None
_client_cache_lock = threading.RLock()
ResultT = TypeVar("ResultT")


def reset_client_cache() -> None:
    global _client_cache, _client_cache_key
    with _client_cache_lock:
        if _client_cache is not None:
            _client_cache.close()
        _client_cache = None
        _client_cache_key = None


def _get_client() -> CruxibleClient | None:
    global _client_cache, _client_cache_key
    settings = resolve_server_settings()
    if not settings.enabled:
        reset_client_cache()
        return None
    token = get_runtime_bearer_token()
    cache_key = (settings.server_url, settings.server_socket, token)
    with _client_cache_lock:
        if _client_cache is None or _client_cache_key != cache_key:
            reset_client_cache()
            _client_cache = CruxibleClient(
                base_url=settings.server_url,
                socket_path=settings.server_socket,
                token=token,
            )
            _client_cache_key = cache_key
        return _client_cache


def _dispatch_remote_or_local(
    remote_call: Callable[[CruxibleClient], ResultT],
    local_call: Callable[[], ResultT],
    *,
    allow_local: bool = True,
    operation_name: str,
) -> ResultT:
    try:
        client = _get_client()
    except ConfigError as exc:
        raise ConfigError(
            f"{exc} Required by {operation_name}; configure CRUXIBLE_SERVER_URL "
            "or CRUXIBLE_SERVER_SOCKET."
        ) from exc
    if client is not None:
        try:
            return remote_call(client)
        except ServerUnreachableError as exc:
            raise ServerUnreachableError(
                exc.target,
                f"{exc.reason} (needed by {operation_name})",
            ) from exc
    if not allow_local:
        raise ConfigError(f"Local execution disabled for {operation_name}; configure a daemon.")
    return local_call()


def handle_server_info() -> contracts.ServerInfoResult:
    return _dispatch_remote_or_local(
        lambda client: client.server_info(),
        host_api.server_info,
        operation_name="cruxible_server_info",
    )


def handle_playbill_host_create(instance_id: str | None) -> contracts.PlaybillHostResult:
    return _dispatch_remote_or_local(
        lambda client: client.create_playbill_host(instance_id=instance_id),
        lambda: host_api.create_playbill_host(instance_id=instance_id),
        operation_name="cruxible_playbill_host_create",
    )


def handle_playbill_init(
    instance_id: str,
    principals: list[dict[str, Any]],
    operating_profile: str,
) -> contracts.PlaybillInitResult:
    records = tuple(PrincipalRecord.model_validate(item) for item in principals)
    return _dispatch_remote_or_local(
        lambda client: client.init_playbill(
            instance_id,
            principals=[item.model_dump(mode="json") for item in records],
            operating_profile=cast(Any, operating_profile),
        ),
        lambda: playbill_api.playbill_init(
            instance_id,
            principals=records,
            operating_profile=cast(Any, operating_profile),
        ),
        operation_name="cruxible_playbill_init",
    )


def handle_playbill_store_body(
    instance_id: str, content_base64: str
) -> contracts.PlaybillCasObjectResult:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise DataValidationError("Playbill body is not canonical base64") from exc
    return _dispatch_remote_or_local(
        lambda client: client.store_playbill_body(instance_id, content),
        lambda: playbill_api.playbill_store_body(instance_id, content_base64=content_base64),
        operation_name="cruxible_playbill_store_body",
    )


def handle_playbill_propose_document(
    instance_id: str,
    shell: dict[str, Any],
    proposal_name: str,
    source_compilation_digest: str | None,
) -> contracts.PlaybillProposalInspection:
    document = DocumentShell.model_validate(shell)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_document(
            instance_id,
            shell=document.model_dump(mode="json"),
            proposal_name=proposal_name,
            source_compilation_digest=source_compilation_digest,
        ),
        lambda: playbill_api.playbill_propose_document(
            instance_id,
            shell=document,
            proposal_name=proposal_name,
            source_compilation_digest=source_compilation_digest,
        ),
        operation_name="cruxible_playbill_propose_document",
    )


def handle_playbill_inspect_proposal(
    instance_id: str, proposal_id: str
) -> contracts.PlaybillProposalInspection:
    return _dispatch_remote_or_local(
        lambda client: client.inspect_playbill_proposal(instance_id, proposal_id),
        lambda: playbill_api.playbill_inspect_proposal(instance_id, proposal_id),
        operation_name="cruxible_playbill_inspect_proposal",
    )


def handle_playbill_inspect_refusal(
    instance_id: str, proposal_id: str
) -> contracts.PlaybillRefusalInspection:
    return _dispatch_remote_or_local(
        lambda client: client.inspect_playbill_refusal(instance_id, proposal_id),
        lambda: playbill_api.playbill_inspect_refusal(instance_id, proposal_id),
        operation_name="cruxible_playbill_inspect_refusal",
    )


def handle_playbill_review(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool,
) -> contracts.PlaybillProposalReview:
    return _dispatch_remote_or_local(
        lambda client: client.review_playbill_proposal(
            instance_id, proposal_id, include_body=include_body
        ),
        lambda: playbill_api.playbill_review_proposal(
            instance_id, proposal_id, include_body=include_body
        ),
        operation_name="cruxible_playbill_review",
    )


def handle_playbill_prepare_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool,
) -> contracts.PlaybillApprovalChallenge:
    return _dispatch_remote_or_local(
        lambda client: client.prepare_playbill_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        ),
        lambda: playbill_api.playbill_prepare_approval(
            instance_id,
            proposal_id,
            signer_id=signer_id,
            include_body=include_body,
        ),
        operation_name="cruxible_playbill_prepare_approval",
    )


def handle_playbill_submit_approval(
    instance_id: str,
    proposal_id: str,
    attestation: dict[str, Any],
) -> contracts.PlaybillApprovalReceipt:
    public_attestation = ApprovalAttestation.model_validate(attestation)
    return _dispatch_remote_or_local(
        lambda client: client.submit_playbill_approval(
            instance_id,
            proposal_id,
            attestation=public_attestation.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_submit_approval(
            instance_id,
            proposal_id,
            attestation=public_attestation,
        ),
        operation_name="cruxible_playbill_submit_approval",
    )


def handle_playbill_activate(
    instance_id: str, proposal_id: str
) -> contracts.PlaybillActivationReceipt:
    return _dispatch_remote_or_local(
        lambda client: client.activate_playbill_proposal(instance_id, proposal_id),
        lambda: playbill_api.playbill_activate(instance_id, proposal_id),
        operation_name="cruxible_playbill_activate",
    )


def handle_playbill_list_documents(instance_id: str) -> contracts.PlaybillDocumentList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_documents(instance_id),
        lambda: playbill_api.playbill_list_documents(instance_id),
        operation_name="cruxible_playbill_list_documents",
    )


def handle_playbill_get_document(instance_id: str, identity: str) -> contracts.PlaybillDocumentView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_document(instance_id, identity),
        lambda: playbill_api.playbill_get_document(instance_id, identity),
        operation_name="cruxible_playbill_get_document",
    )


def handle_playbill_dereference(instance_id: str, identity: str) -> contracts.PlaybillBodyRead:
    return _dispatch_remote_or_local(
        lambda client: client.dereference_playbill_document(instance_id, identity),
        lambda: playbill_api.playbill_dereference_document(instance_id, identity),
        operation_name="cruxible_playbill_dereference",
    )


def handle_playbill_history(instance_id: str, identity: str) -> contracts.PlaybillDocumentHistory:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_document_history(instance_id, identity),
        lambda: playbill_api.playbill_document_history(instance_id, identity),
        operation_name="cruxible_playbill_history",
    )


def handle_playbill_explain(
    instance_id: str,
    subject: dict[str, Any],
    at: dict[str, Any],
    *,
    detail: str,
    include_body: bool,
) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
    semantic_subject = SemanticAddress.model_validate(subject)
    coordinate = AcceptedCoordinate.model_validate(at)
    return _dispatch_remote_or_local(
        lambda client: client.explain_playbill_subject(
            instance_id,
            subject=semantic_subject.model_dump(mode="json"),
            at=coordinate.model_dump(mode="json"),
            detail=cast(Any, detail),
            include_body=include_body,
        ),
        lambda: playbill_api.playbill_explain(
            instance_id,
            subject=semantic_subject,
            at=coordinate,
            detail=cast(Any, detail),
            include_body=include_body,
        ),
        operation_name="cruxible_playbill_explain",
    )


def handle_playbill_source_context(instance_id: str) -> contracts.PlaybillSourceContext:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_source_context(instance_id),
        lambda: playbill_api.playbill_source_context(instance_id),
        operation_name="cruxible_playbill_source_context",
    )


def handle_playbill_check_source_bundle(
    instance_id: str, bundle: dict[str, Any]
) -> contracts.PlaybillSourceCheckResult:
    frozen = SourceCompilationBundle.model_validate(bundle)
    return _dispatch_remote_or_local(
        lambda client: client.check_playbill_source_bundle(
            instance_id, bundle=frozen.model_dump(mode="json")
        ),
        lambda: playbill_api.playbill_check_source_bundle(instance_id, bundle=frozen),
        operation_name="cruxible_playbill_check_source_bundle",
    )


def handle_playbill_propose_source_bundle(
    instance_id: str,
    bundle: dict[str, Any],
    *,
    source_name: str,
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    frozen = SourceCompilationBundle.model_validate(bundle)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_source_bundle(
            instance_id,
            bundle=frozen.model_dump(mode="json"),
            source_name=source_name,
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_source_bundle(
            instance_id,
            bundle=frozen,
            source_name=source_name,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_source_bundle",
    )


def handle_playbill_list_principals(instance_id: str) -> contracts.PlaybillPrincipalList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_principals(instance_id),
        lambda: playbill_api.playbill_list_principals(instance_id),
        operation_name="cruxible_playbill_list_principals",
    )


def handle_playbill_propose_principal_change(
    instance_id: str,
    principal: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    record = PrincipalRecord.model_validate(principal)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_principal_change(
            instance_id,
            principal=record.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_principal_change(
            instance_id,
            principal=record,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_principal_change",
    )


def handle_playbill_propose_subject(
    instance_id: str,
    shell: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    subject = SubjectShell.model_validate(shell)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_subject(
            instance_id,
            shell=subject.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_subject(
            instance_id,
            shell=subject,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_subject",
    )


def handle_playbill_list_subjects(instance_id: str) -> contracts.PlaybillSubjectList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_subjects(instance_id),
        lambda: playbill_api.playbill_list_subjects(instance_id),
        operation_name="cruxible_playbill_list_subjects",
    )


def handle_playbill_get_subject(
    instance_id: str, subject_kind: str, subject_id: str
) -> contracts.PlaybillSubjectView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_subject(instance_id, subject_kind, subject_id),
        lambda: playbill_api.playbill_get_subject(
            instance_id, f"Subject:{subject_kind}/{subject_id}"
        ),
        operation_name="cruxible_playbill_get_subject",
    )


def handle_playbill_subject_history(
    instance_id: str, subject_kind: str, subject_id: str
) -> contracts.PlaybillSubjectHistory:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_subject_history(instance_id, subject_kind, subject_id),
        lambda: playbill_api.playbill_subject_history(
            instance_id, f"Subject:{subject_kind}/{subject_id}"
        ),
        operation_name="cruxible_playbill_subject_history",
    )


def handle_playbill_propose_claim_type(
    instance_id: str,
    claim_type: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    contract = ClaimType.model_validate(claim_type)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_claim_type(
            instance_id,
            claim_type=contract.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_claim_type(
            instance_id,
            claim_type=contract,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_claim_type",
    )


def handle_playbill_list_claim_types(instance_id: str) -> contracts.PlaybillClaimTypeList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_claim_types(instance_id),
        lambda: playbill_api.playbill_list_claim_types(instance_id),
        operation_name="cruxible_playbill_list_claim_types",
    )


def handle_playbill_get_claim_type(
    instance_id: str, predicate: str
) -> contracts.PlaybillClaimTypeView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_claim_type(instance_id, predicate),
        lambda: playbill_api.playbill_get_claim_type(instance_id, predicate),
        operation_name="cruxible_playbill_get_claim_type",
    )


def handle_playbill_propose_claim(
    instance_id: str,
    authoring: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillClaimProposal:
    request = DirectClaimAuthoringV1.model_validate(authoring)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_claim(
            instance_id,
            authoring=request.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_claim(
            instance_id,
            authoring=request,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_claim",
    )


def handle_playbill_propose_claims(
    instance_id: str,
    authorings: list[dict[str, Any]],
    proposal_name: str,
) -> contracts.PlaybillClaimBatchProposal:
    requests = tuple(DirectClaimAuthoringV1.model_validate(item) for item in authorings)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_claims(
            instance_id,
            authorings=[item.model_dump(mode="json") for item in requests],
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_claims(
            instance_id,
            authorings=requests,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_claims",
    )


def handle_playbill_list_claims(
    instance_id: str,
    *,
    subject_path: str | None,
    predicate: str | None,
    include_retired: bool,
) -> contracts.PlaybillClaimList:
    subject = None if subject_path is None else SemanticAddress.whole_artifact(subject_path)
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_claims(
            instance_id,
            subject_path=subject_path,
            predicate=predicate,
            include_retired=include_retired,
        ),
        lambda: playbill_api.playbill_list_claims(
            instance_id,
            subject=subject,
            predicate=predicate,
            include_retired=include_retired,
        ),
        operation_name="cruxible_playbill_list_claims",
    )


def handle_playbill_get_claim(instance_id: str, identity: str) -> contracts.PlaybillClaimView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_claim(instance_id, identity),
        lambda: playbill_api.playbill_get_claim(instance_id, identity),
        operation_name="cruxible_playbill_get_claim",
    )


def handle_playbill_claim_history(
    instance_id: str, identity: str
) -> contracts.PlaybillClaimHistory:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_claim_history(instance_id, identity),
        lambda: playbill_api.playbill_claim_history(instance_id, identity),
        operation_name="cruxible_playbill_claim_history",
    )


def handle_playbill_explain_claim(
    instance_id: str,
    identity: str,
    *,
    evaluation_time: str | None,
) -> contracts.PlaybillClaimExplanation:
    evaluated_at = parse_datetime(evaluation_time)
    return _dispatch_remote_or_local(
        lambda client: client.explain_playbill_claim(
            instance_id,
            identity,
            evaluation_time=(None if evaluated_at is None else evaluated_at.isoformat()),
        ),
        lambda: playbill_api.playbill_explain_claim(
            instance_id,
            identity,
            evaluation_time=evaluated_at,
        ),
        operation_name="cruxible_playbill_explain_claim",
    )


def handle_playbill_propose_query_definition(
    instance_id: str,
    query: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    definition = QueryDefinitionV1.model_validate(query)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_query_definition(
            instance_id,
            query=definition.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_query_definition(
            instance_id,
            query=definition,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_query_definition",
    )


def handle_playbill_list_query_definitions(
    instance_id: str,
) -> contracts.PlaybillQueryDefinitionList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_query_definitions(instance_id),
        lambda: playbill_api.playbill_list_query_definitions(instance_id),
        operation_name="cruxible_playbill_list_query_definitions",
    )


def handle_playbill_get_query_definition(
    instance_id: str, name: str
) -> contracts.PlaybillQueryDefinitionView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_query_definition(instance_id, name),
        lambda: playbill_api.playbill_get_query_definition(instance_id, name),
        operation_name="cruxible_playbill_get_query_definition",
    )


def handle_playbill_run_query(
    instance_id: str,
    name: str,
    *,
    parameters: dict[str, Any] | None,
    evaluation_time: str | None,
    budgets: dict[str, Any] | None,
) -> contracts.PlaybillQueryRun:
    evaluated_at = parse_datetime(evaluation_time)
    limits = None if budgets is None else QueryBudgetsV1.model_validate(budgets)
    return _dispatch_remote_or_local(
        lambda client: client.run_playbill_query(
            instance_id,
            name,
            evaluation_time=(None if evaluated_at is None else evaluated_at.isoformat()),
            parameters=parameters,
            budgets=(None if limits is None else limits.model_dump(mode="json")),
        ),
        lambda: playbill_api.playbill_run_query(
            instance_id,
            name,
            evaluation_time=evaluated_at,
            parameters=parameters,
            budgets=limits,
        ),
        operation_name="cruxible_playbill_run_query",
    )


def handle_playbill_discover(
    instance_id: str,
    *,
    query: str | None,
    entrypoint: str | None,
    evaluation_time: str | None,
    profile: str,
    budget: dict[str, Any] | None,
) -> contracts.PlaybillDiscoveryResult:
    limits = None if budget is None else DiscoveryBudgetV1.model_validate(budget)
    return _dispatch_remote_or_local(
        lambda client: client.discover_playbill(
            instance_id,
            query=query,
            entrypoint=entrypoint,
            evaluation_time=evaluation_time,
            profile=cast(Any, profile),
            budget=(None if limits is None else limits.model_dump(mode="json")),
        ),
        lambda: playbill_api.playbill_discover(
            instance_id,
            query=query,
            entrypoint=entrypoint,
            evaluation_time=evaluation_time,
            profile=cast(Any, profile),
            budget=limits,
        ),
        operation_name="cruxible_playbill_discover",
    )


def handle_playbill_expand(
    instance_id: str,
    address: dict[str, Any],
    *,
    evaluation_time: str | None,
    facets: list[str],
    budget: dict[str, Any] | None,
) -> contracts.PlaybillContextCapsule:
    semantic_address = SemanticAddress.model_validate(address)
    limits = None if budget is None else ExpansionBudgetV1.model_validate(budget)
    return _dispatch_remote_or_local(
        lambda client: client.expand_playbill(
            instance_id,
            address=semantic_address.model_dump(mode="json"),
            evaluation_time=evaluation_time,
            facets=tuple(facets),
            budget=(None if limits is None else limits.model_dump(mode="json")),
        ),
        lambda: playbill_api.playbill_expand(
            instance_id,
            address=semantic_address,
            evaluation_time=evaluation_time,
            facets=tuple(facets),
            budget=limits,
        ),
        operation_name="cruxible_playbill_expand",
    )


def handle_playbill_resolve_coverage(
    instance_id: str,
    observations: list[dict[str, Any]],
    *,
    budget: dict[str, Any] | None = None,
    scan_budget: dict[str, Any] | None = None,
) -> contracts.PlaybillCoverageResult:
    observed = tuple(WorkingSourceObservationV1.model_validate(item) for item in observations)
    cards = None if budget is None else CoverageCardBudgetV1.model_validate(budget)
    scan = None if scan_budget is None else CoverageScanBudgetV1.model_validate(scan_budget)
    return _dispatch_remote_or_local(
        lambda client: client.resolve_playbill_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observed],
            budget=(None if cards is None else cards.model_dump(mode="json")),
            scan_budget=(None if scan is None else scan.model_dump(mode="json")),
        ),
        lambda: playbill_api.playbill_resolve_coverage(
            instance_id,
            observations=observed,
            budget=cards,
            scan_budget=scan,
        ),
        operation_name="cruxible_playbill_resolve_coverage",
    )


def handle_playbill_export_floor(instance_id: str) -> contracts.PlaybillFloorExport:
    return _dispatch_remote_or_local(
        lambda client: client.export_playbill_floor(instance_id),
        lambda: playbill_api.playbill_export_floor(instance_id),
        operation_name="cruxible_playbill_export_floor",
    )
