"""Playbill-only MCP handler implementations."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar, cast

from pydantic import TypeAdapter

from cruxible_client import (
    CruxibleClient,
    activate_with_workspace_refresh,
    contracts,
    inspect_workspace_floor,
    materialize_playbill_floor,
)
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.mcp.workspace import mcp_workspace_root, resolve_workspace_path
from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.authoring.bind import bind_working_selection_input
from cruxible_core.playbill.authoring.examples import authoring_example
from cruxible_core.playbill.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_core.playbill.authoring.models import (
    InsertionConfirmationObservationV1,
)
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1, claim_type_input_example
from cruxible_core.playbill.claim_type_migrations import ClaimTypeMigrationRequestV1
from cruxible_core.playbill.claim_types import ClaimType
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.coverage.workspace import (
    bindings_from_mapping,
    observe_workspace,
)
from cruxible_core.playbill.discovery import DiscoveryBudgetV1, ExpansionBudgetV1
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.search import (
    SEARCH_KINDS,
    PlaybillSearchBudgetsV1,
    PlaybillSearchCursorV1,
    SearchKind,
    SearchStatus,
)
from cruxible_core.playbill.seed_client import (
    SeedApplicationResultV1,
    SeedPlanResultV1,
    apply_seed_directory_group,
    plan_seed_directory,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.subjects import SubjectShell
from cruxible_core.playbill.types import PrincipalRecord
from cruxible_core.playbill.workspace_sources import (
    compile_client_source_context,
    load_source_catalog,
    mapped_root_aliases,
)
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1
from cruxible_core.temporal import parse_datetime

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None] | None = None
_client_cache_lock = threading.RLock()
ResultT = TypeVar("ResultT")
_AUTHORING_INPUT: TypeAdapter[AuthoringInputV1] = TypeAdapter(AuthoringInputV1)


class _LocalFloorClient:
    """Give the shared client adapter the same two calls in library mode."""

    def activate_playbill_proposal(
        self, instance_id: str, proposal_id: str
    ) -> contracts.PlaybillActivationReceipt:
        return playbill_api.playbill_activate(instance_id, proposal_id)

    def export_playbill_floor(
        self,
        instance_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillFloorExport:
        if at is not None:  # pragma: no cover - shared refresh always asks for current
            raise DataValidationError("local floor adapter accepts only the current coordinate")
        return playbill_api.playbill_export_floor(instance_id)


class _LocalSourceContextClient:
    """Supply accepted context to the shared local compiler in library mode."""

    def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext:
        return playbill_api.playbill_source_context(instance_id)


class _LocalSeedClient:
    """Map the shared seed coordinator onto library-mode runtime operations."""

    def store_playbill_body(
        self, instance_id: str, content: bytes
    ) -> contracts.PlaybillCasObjectResult:
        return playbill_api.playbill_store_body(
            instance_id,
            content_base64=base64.b64encode(content).decode("ascii"),
        )

    def compile_playbill_authoring_input(
        self,
        instance_id: str,
        *,
        input: Mapping[str, Any],
        intent_id: str | None,
    ) -> contracts.PlaybillAuthoringPreflightResult:
        return playbill_api.playbill_authoring_compile_input(
            instance_id,
            input=_AUTHORING_INPUT.validate_python(input),
            intent_id=intent_id,
        )

    def submit_playbill_authoring_intent(
        self, instance_id: str, intent_id: str
    ) -> contracts.PlaybillAuthoringSubmitResult:
        return playbill_api.playbill_authoring_submit(instance_id, intent_id)

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
        return playbill_api.playbill_whoami(instance_id)

    def list_playbill_proposals(
        self,
        instance_id: str,
        *,
        status: Literal["open", "settled"] | None = None,
    ) -> contracts.PlaybillProposalList:
        return playbill_api.playbill_list_proposals(
            instance_id,
            status=cast(Any, status),
        )

    def propose_playbill_claims(
        self,
        instance_id: str,
        *,
        authorings: list[dict[str, Any]],
        proposal_name: str,
    ) -> contracts.PlaybillClaimBatchProposal:
        return playbill_api.playbill_propose_claims(
            instance_id,
            authorings=tuple(DirectClaimAuthoringV1.model_validate(item) for item in authorings),
            proposal_name=proposal_name,
        )

    def propose_playbill_claim_type(
        self,
        instance_id: str,
        *,
        claim_type: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        return playbill_api.playbill_propose_claim_type(
            instance_id,
            claim_type=ClaimType.model_validate(claim_type),
            proposal_name=proposal_name,
        )

    def propose_playbill_subject(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        return playbill_api.playbill_propose_subject(
            instance_id,
            shell=SubjectShell.model_validate(shell),
            proposal_name=proposal_name,
        )

    def propose_playbill_document(
        self,
        instance_id: str,
        *,
        shell: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        return playbill_api.playbill_propose_document(
            instance_id,
            shell=DocumentShell.model_validate(shell),
            proposal_name=proposal_name,
        )

    def propose_playbill_query_definition(
        self,
        instance_id: str,
        *,
        query: Mapping[str, Any],
        proposal_name: str,
    ) -> contracts.PlaybillProposalInspection:
        return playbill_api.playbill_propose_query_definition(
            instance_id,
            query=QueryDefinitionV1.model_validate(query),
            proposal_name=proposal_name,
        )


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
) -> contracts.PlaybillWorkspaceActivationResult:
    workspace = mcp_workspace_root()
    return _dispatch_remote_or_local(
        lambda client: activate_with_workspace_refresh(
            client, instance_id, proposal_id, workspace=workspace
        ),
        lambda: activate_with_workspace_refresh(
            _LocalFloorClient(), instance_id, proposal_id, workspace=workspace
        ),
        operation_name="cruxible_playbill_activate",
    )


def handle_playbill_whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_whoami(instance_id),
        lambda: playbill_api.playbill_whoami(instance_id),
        operation_name="cruxible_playbill_whoami",
    )


def handle_playbill_list_proposals(
    instance_id: str,
    status: str | None,
) -> contracts.PlaybillProposalList:
    normalized = cast(Any, status)
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_proposals(instance_id, status=normalized),
        lambda: playbill_api.playbill_list_proposals(instance_id, status=normalized),
        operation_name="cruxible_playbill_proposal_list",
    )


def handle_playbill_readmit_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalReadmitResult:
    return _dispatch_remote_or_local(
        lambda client: client.readmit_playbill_proposal(instance_id, proposal_id),
        lambda: playbill_api.playbill_readmit_proposal(instance_id, proposal_id),
        operation_name="cruxible_playbill_proposal_readmit",
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
    input: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillClaimTypeInputProposalResult:
    request = ClaimTypeInputV1.model_validate(input)
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_claim_type_input(
            instance_id,
            input=request.model_dump(mode="json"),
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_claim_type_input(
            instance_id,
            input=request,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_propose_claim_type",
    )


def handle_playbill_migrate_claim_type(
    instance_id: str,
    request: dict[str, Any],
) -> contracts.PlaybillClaimTypeMigrationResult:
    migration = ClaimTypeMigrationRequestV1.model_validate(request)
    return _dispatch_remote_or_local(
        lambda client: client.migrate_playbill_claim_type(
            instance_id,
            request=migration.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_migrate_claim_type(instance_id, request=migration),
        operation_name="cruxible_playbill_claim_type_migrate",
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


def handle_playbill_authoring_create(
    instance_id: str,
    payload: dict[str, Any],
) -> contracts.PlaybillAuthoringIntentView:
    request = _AUTHORING_INPUT.validate_python(payload)
    return _dispatch_remote_or_local(
        lambda client: client.create_playbill_authoring_input(
            instance_id, input=request.model_dump(mode="json")
        ),
        lambda: playbill_api.playbill_authoring_create_input(instance_id, input=request),
        operation_name="cruxible_playbill_authoring_create",
    )


def handle_playbill_authoring_example(
    name: contracts.PlaybillAuthoringExampleName,
) -> contracts.PlaybillAuthoringExampleResult:
    if name == "claim-type":
        payload = claim_type_input_example().model_dump(mode="json")
    else:
        payload = authoring_example(name).model_dump(mode="json")
    return contracts.PlaybillAuthoringExampleResult(name=name, payload=payload)


def handle_playbill_authoring_get(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_get(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_get",
    )


def handle_playbill_authoring_resume(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return _dispatch_remote_or_local(
        lambda client: client.resume_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_resume(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_resume",
    )


def handle_playbill_authoring_list_pending(
    instance_id: str,
) -> contracts.PlaybillAuthoringIntentList:
    return _dispatch_remote_or_local(
        lambda client: client.list_pending_playbill_authoring_intents(instance_id),
        lambda: playbill_api.playbill_authoring_list_pending(instance_id),
        operation_name="cruxible_playbill_authoring_list_pending",
    )


def handle_playbill_authoring_compile(
    instance_id: str,
    payload: dict[str, Any],
    *,
    intent_id: str | None,
) -> contracts.PlaybillAuthoringPreflightResult:
    request = _AUTHORING_INPUT.validate_python(payload)
    return _dispatch_remote_or_local(
        lambda client: client.compile_playbill_authoring_input(
            instance_id,
            input=request.model_dump(mode="json"),
            intent_id=intent_id,
        ),
        lambda: playbill_api.playbill_authoring_compile_input(
            instance_id,
            input=request,
            intent_id=intent_id,
        ),
        operation_name="cruxible_playbill_authoring_compile",
    )


def handle_playbill_authoring_bind(
    instance_id: str,
    *,
    source_path: str,
    anchor: str,
    payload: ClaimInput,
    window_lines: int | None,
) -> contracts.PlaybillAuthoringPreflightResult:
    path = resolve_workspace_path(source_path, kind="file")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DataValidationError(f"could not read workspace source {source_path}: {exc}") from exc
    bound = bind_working_selection_input(
        payload,
        content=content,
        anchor=anchor,
        window_lines=window_lines,
    )
    return _dispatch_remote_or_local(
        lambda client: client.compile_playbill_authoring(
            instance_id,
            payload=bound.model_dump(mode="json"),
            intent_id=None,
        ),
        lambda: playbill_api.playbill_authoring_compile(
            instance_id,
            payload=bound,
            intent_id=None,
        ),
        operation_name="cruxible_playbill_authoring_bind",
    )


def handle_playbill_authoring_preflight(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringPreflightResult:
    return _dispatch_remote_or_local(
        lambda client: client.preflight_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_preflight(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_preflight",
    )


def handle_playbill_authoring_submit(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringSubmitResult:
    return _dispatch_remote_or_local(
        lambda client: client.submit_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_submit(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_submit",
    )


def handle_playbill_authoring_status(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillCandidateStatus:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_authoring_intent_status(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_status(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_status",
    )


def handle_playbill_authoring_confirm_insertion(
    instance_id: str,
    intent_id: str,
    observation: dict[str, Any],
) -> contracts.PlaybillInsertionConfirmResult:
    request = InsertionConfirmationObservationV1.model_validate(observation)
    return _dispatch_remote_or_local(
        lambda client: client.confirm_playbill_authoring_insertion(
            instance_id,
            intent_id,
            observation=request.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_authoring_confirm_insertion(
            instance_id,
            intent_id,
            observation=request,
        ),
        operation_name="cruxible_playbill_authoring_confirm_insertion",
    )


def handle_playbill_authoring_abandon_insertion(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillInsertionAbandonResult:
    return _dispatch_remote_or_local(
        lambda client: client.abandon_playbill_authoring_insertion(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_abandon_insertion(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_abandon_insertion",
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


def handle_playbill_get_claim(
    instance_id: str,
    identity: str,
    *,
    evaluation_time: str | None = None,
) -> contracts.PlaybillClaimViewV2:
    evaluated_at = parse_datetime(evaluation_time)
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_claim(
            instance_id,
            identity,
            evaluation_time=(None if evaluated_at is None else evaluated_at.isoformat()),
        ),
        lambda: playbill_api.playbill_get_claim(
            instance_id,
            identity,
            evaluation_time=evaluated_at,
        ),
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
) -> contracts.PlaybillClaimExplanationV2:
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
) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
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


def handle_playbill_search(
    instance_id: str,
    *,
    mode: str,
    query: str | None,
    kinds: list[SearchKind] | None,
    subject: dict[str, Any] | None,
    statuses: list[SearchStatus] | None,
    cursor: dict[str, Any] | None,
    evaluation_time: str | None,
    budgets: dict[str, Any] | None,
) -> contracts.PlaybillSearchResult:
    parsed_cursor = None if cursor is None else PlaybillSearchCursorV1.model_validate(cursor)
    limits = (
        parsed_cursor.budgets
        if budgets is None and parsed_cursor is not None
        else None
        if budgets is None
        else PlaybillSearchBudgetsV1.model_validate(budgets)
    )
    parsed_subject = None if subject is None else SemanticAddress.model_validate(subject)
    evaluated_at = parse_datetime(evaluation_time)
    selected_kinds = SEARCH_KINDS if kinds is None else tuple(sorted(set(kinds)))
    selected_statuses = () if statuses is None else tuple(sorted(set(statuses)))
    return _dispatch_remote_or_local(
        lambda client: client.search_playbill(
            instance_id,
            mode=cast(Any, mode),
            query=query,
            kinds=selected_kinds,
            subject=(None if parsed_subject is None else parsed_subject.model_dump(mode="json")),
            statuses=selected_statuses,
            cursor=(None if parsed_cursor is None else parsed_cursor.model_dump(mode="json")),
            evaluation_time=(None if evaluated_at is None else evaluated_at.isoformat()),
            budgets=(None if limits is None else limits.model_dump(mode="json")),
        ),
        lambda: playbill_api.playbill_search(
            instance_id,
            mode=cast(Any, mode),
            query=query,
            kinds=cast(Any, selected_kinds),
            subject=parsed_subject,
            statuses=cast(Any, selected_statuses),
            cursor=parsed_cursor,
            evaluation_time=evaluated_at,
            budgets=limits,
        ),
        operation_name="cruxible_playbill_search",
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


def handle_playbill_workspace_source_compile(
    instance_id: str,
    *,
    catalog_path: str,
    repository_root: str,
    local_catalog_path: str | None,
    root_aliases: Mapping[str, str],
) -> SourceCompilationBundle:
    """Compile declared workspace bytes without exposing path or digest plumbing."""

    workspace = mcp_workspace_root()
    catalog = load_source_catalog(
        resolve_workspace_path(catalog_path, root=workspace, kind="file"),
        (
            None
            if local_catalog_path is None
            else resolve_workspace_path(local_catalog_path, root=workspace, kind="file")
        ),
    )
    repository = resolve_workspace_path(repository_root, root=workspace, kind="directory")
    aliases = mapped_root_aliases(
        {
            name: resolve_workspace_path(path, root=workspace, kind="directory")
            for name, path in root_aliases.items()
        }
    )
    return _dispatch_remote_or_local(
        lambda client: compile_client_source_context(
            client,
            instance_id,
            catalog=catalog,
            repository_root=repository,
            aliases=aliases,
        ),
        lambda: compile_client_source_context(
            _LocalSourceContextClient(),
            instance_id,
            catalog=catalog,
            repository_root=repository,
            aliases=aliases,
        ),
        operation_name="cruxible_playbill_workspace_source_compile",
    )


def handle_playbill_workspace_source_check(
    instance_id: str,
    *,
    catalog_path: str,
    repository_root: str,
    local_catalog_path: str | None,
    root_aliases: Mapping[str, str],
) -> contracts.PlaybillSourceCheckResult:
    bundle = handle_playbill_workspace_source_compile(
        instance_id,
        catalog_path=catalog_path,
        repository_root=repository_root,
        local_catalog_path=local_catalog_path,
        root_aliases=root_aliases,
    )
    return _dispatch_remote_or_local(
        lambda client: client.check_playbill_source_bundle(
            instance_id,
            bundle=bundle.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_check_source_bundle(instance_id, bundle=bundle),
        operation_name="cruxible_playbill_workspace_source_check",
    )


def handle_playbill_workspace_coverage_resolve(
    instance_id: str,
    *,
    bindings: Mapping[str, str],
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_results_path: str | None,
    whole_working_set: bool,
    budget: dict[str, Any] | None,
    scan_budget: dict[str, Any] | None,
) -> contracts.PlaybillCoverageResult:
    """Read selected workspace bytes and lower them to existing coverage wire."""

    workspace = mcp_workspace_root()
    grep_text = (
        None
        if grep_results_path is None
        else resolve_workspace_path(
            grep_results_path,
            root=workspace,
            kind="file",
        ).read_text(encoding="utf-8")
    )
    observations = observe_workspace(
        bindings_from_mapping(bindings),
        root=workspace,
        files=files,
        ranges=ranges,
        grep_text=grep_text,
        whole_working_set=whole_working_set,
    )
    return handle_playbill_resolve_coverage(
        instance_id,
        [item.model_dump(mode="json") for item in observations],
        budget=budget,
        scan_budget=scan_budget,
    )


def handle_playbill_workspace_coverage_status(
    instance_id: str,
    *,
    bindings: Mapping[str, str],
    budget: dict[str, Any] | None,
    scan_budget: dict[str, Any] | None,
) -> contracts.PlaybillCoverageResult:
    return handle_playbill_workspace_coverage_resolve(
        instance_id,
        bindings=bindings,
        files=(),
        ranges=(),
        grep_results_path=None,
        whole_working_set=True,
        budget=budget,
        scan_budget=scan_budget,
    )


def handle_playbill_seed_plan(
    *,
    bundle_path: str,
    proposal_name: str,
) -> SeedPlanResultV1:
    root = resolve_workspace_path(
        bundle_path,
        root=mcp_workspace_root(),
        kind="directory",
    )
    return plan_seed_directory(root, proposal_name=proposal_name)


def handle_playbill_seed_apply(
    instance_id: str,
    *,
    bundle_path: str,
    proposal_name: str,
    group_id: str | None,
) -> SeedApplicationResultV1:
    root = resolve_workspace_path(
        bundle_path,
        root=mcp_workspace_root(),
        kind="directory",
    )
    return _dispatch_remote_or_local(
        lambda client: apply_seed_directory_group(
            client,
            instance_id,
            root=root,
            proposal_name=proposal_name,
            group_id=group_id,
        ),
        lambda: apply_seed_directory_group(
            _LocalSeedClient(),
            instance_id,
            root=root,
            proposal_name=proposal_name,
            group_id=group_id,
        ),
        operation_name="cruxible_playbill_seed_apply",
    )


def handle_playbill_export_floor(instance_id: str) -> contracts.PlaybillFloorExport:
    return _dispatch_remote_or_local(
        lambda client: client.export_playbill_floor(instance_id),
        lambda: playbill_api.playbill_export_floor(instance_id),
        operation_name="cruxible_playbill_export_floor",
    )


def handle_playbill_workspace_floor_export(
    instance_id: str,
    output_path: str,
    *,
    force: bool,
) -> contracts.PlaybillWorkspaceFloorWriteResult:
    workspace = mcp_workspace_root()
    export = handle_playbill_export_floor(instance_id)
    return materialize_playbill_floor(
        workspace,
        relative_path=output_path,
        export=export,
        force=force,
    )


def handle_playbill_workspace_floor_status(
    instance_id: str,
) -> contracts.PlaybillWorkspaceFloorStatus:
    search = _dispatch_remote_or_local(
        lambda client: client.search_playbill(instance_id, mode="orient"),
        lambda: playbill_api.playbill_search(instance_id, mode="orient"),
        operation_name="cruxible_playbill_workspace_floor_status",
    )
    return inspect_workspace_floor(
        mcp_workspace_root(),
        current_coordinate=search.coordinate,
    )
