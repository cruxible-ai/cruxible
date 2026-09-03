"""Playbill-only MCP handler implementations."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar, cast

from pydantic import TypeAdapter

from cruxible_client import (
    CruxibleClient,
    activate_with_workspace_refresh,
    contracts,
    inspect_workspace_floor,
    materialize_playbill_floor,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
    local_attestation_signer_from_environment,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_client.authoring.seed import SeedPlanResultV1, plan_seed_directory
from cruxible_client.authoring.sources import (
    compile_client_source_context,
    load_source_catalog,
    mapped_root_aliases,
)
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.models import (
    InsertionConfirmationObservationV2,
    PublicationSourceObservationV2,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
    ClaimStance,
    PreparedClaimAttestationRequestV1,
)
from cruxible_client.contracts.claims import ClaimRetireRequestV1
from cruxible_client.contracts.discovery import DiscoveryBudgetV1, ExpansionBudgetV1
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.mcp.workspace import (
    mcp_git_workspace_root,
    mcp_workspace_root,
    resolve_workspace_path,
)
from cruxible_core.playbill.claim_type_inputs import (
    ClaimTypeInputV1,
)
from cruxible_core.playbill.claim_type_migrations import ClaimTypeMigrationRequest
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.coverage.workspace import (
    bindings_from_mapping,
    observe_workspace,
)
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.search import (
    SEARCH_KINDS,
    PlaybillSearchBudgetsV1,
    PlaybillSearchCursorV1,
    SearchKind,
    SearchStatus,
)
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings
from cruxible_core.service.playbill_procedure_runs import (
    LineRunRequestV1,
    ProcedureBindRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
)
from cruxible_core.service.playbill_since import validate_playbill_since_request

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None] | None = None
_client_cache_lock = threading.RLock()
ResultT = TypeVar("ResultT")
_AUTHORING_INPUT: TypeAdapter[AuthoringInputV1] = TypeAdapter(AuthoringInputV1)
_CLAIM_TYPE_MIGRATION: TypeAdapter[ClaimTypeMigrationRequest] = TypeAdapter(
    ClaimTypeMigrationRequest
)
_CLAIM_RETIRE = TypeAdapter(ClaimRetireRequestV1)


class _LocalFloorClient:
    """Give the shared client adapter the same calls in library mode."""

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

    def read_playbill_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: contracts.PlaybillBlockSyncReadRequestV1,
    ) -> contracts.PlaybillBlockSyncReadResultV1:
        return playbill_api.playbill_read_block_sync_backing(instance_id, request=request)


class _LocalSourceContextClient:
    """Supply accepted context to the shared local compiler in library mode."""

    def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext:
        return playbill_api.playbill_source_context(instance_id)


class _LocalAttestationClient:
    """Expose the client-side signing adapter without giving the daemon a key."""

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
        return playbill_api.playbill_whoami(instance_id)

    def list_playbill_principals(self, instance_id: str) -> contracts.PlaybillPrincipalList:
        return playbill_api.playbill_list_principals(instance_id)

    def get_playbill_claim(
        self,
        instance_id: str,
        identity: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate | None = None,
        evaluation_time: str | None = None,
    ) -> contracts.PlaybillClaimViewV2:
        return playbill_api.playbill_get_claim(
            instance_id,
            identity,
            at=(
                None
                if at is None
                else AcceptedCoordinate.model_validate(at.model_dump(mode="json"))
            ),
            evaluation_time=(
                None if evaluation_time is None else datetime.fromisoformat(evaluation_time)
            ),
        )

    def get_playbill_subject(
        self,
        instance_id: str,
        subject_kind: str,
        subject_id: str,
        *,
        at: contracts.PlaybillAcceptedCoordinate,
    ) -> contracts.PlaybillSubjectView:
        return playbill_api.playbill_get_subject(
            instance_id,
            f"Subject:{subject_kind}/{subject_id}",
            at=AcceptedCoordinate.model_validate(at.model_dump(mode="json")),
        )

    def append_playbill_claim_attestation(
        self,
        instance_id: str,
        *,
        request: ClaimAttestationAppendRequestV1,
    ) -> ClaimAttestationAppendResultV1:
        return playbill_api.playbill_append_claim_attestation(instance_id, request=request)

    def server_info(self) -> contracts.ServerInfoResult:
        return host_api.server_info()


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
    require_independent_approval: bool = False,
) -> contracts.PlaybillInitResult:
    records = tuple(PrincipalRecord.model_validate(item) for item in principals)
    return _dispatch_remote_or_local(
        lambda client: client.init_playbill(
            instance_id,
            principals=[item.model_dump(mode="json") for item in records],
            operating_profile=cast(Any, operating_profile),
            require_independent_approval=require_independent_approval,
        ),
        lambda: playbill_api.playbill_init(
            instance_id,
            principals=records,
            operating_profile=cast(Any, operating_profile),
            require_independent_approval=require_independent_approval,
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
    workspace = mcp_git_workspace_root()
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
) -> contracts.PlaybillClaimTypeMigrationResponse:
    migration = _CLAIM_TYPE_MIGRATION.validate_python(request)
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


def handle_playbill_retire_claim(
    instance_id: str,
    claim_id: str,
    request: dict[str, Any],
) -> contracts.PlaybillClaimRetireResponse:
    retirement = _CLAIM_RETIRE.validate_python(request)
    return _dispatch_remote_or_local(
        lambda client: client.retire_playbill_claim(
            instance_id,
            claim_id,
            request=retirement.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_retire_claim(
            instance_id,
            claim_id,
            request=retirement,
        ),
        operation_name="cruxible_playbill_claim_retire",
    )


def _handle_claim_attestation(
    client: Any,
    instance_id: str,
    prepared: PreparedClaimAttestationRequestV1,
) -> ClaimAttestationAppendResultV1:
    signer = local_attestation_signer_from_environment(
        client,
        instance_id,
        workspace_root=mcp_workspace_root(),
    )
    return append_prepared_claim_attestation(
        client,
        instance_id,
        prepared=prepared,
        signer=signer,
    )


def handle_playbill_claim_attest(
    instance_id: str,
    claim_id: str,
    stance: ClaimStance,
    note: str | None,
) -> ClaimAttestationAppendResultV1:
    prepared = PreparedClaimAttestationRequestV1(
        claim_id=claim_id.removeprefix("Claim:"),
        attestation_basis="examined_existing",
        stance=stance,
        attested_at=datetime.now(UTC),
        note=note,
    )
    return _dispatch_remote_or_local(
        lambda client: _handle_claim_attestation(client, instance_id, prepared),
        lambda: _handle_claim_attestation(_LocalAttestationClient(), instance_id, prepared),
        operation_name="cruxible_playbill_claim_attest",
    )


def handle_playbill_claim_attest_new_capture(
    instance_id: str,
    request: PreparedClaimAttestationRequestV1,
) -> ClaimAttestationAppendResultV1:
    if request.attestation_basis != "new_capture":
        raise DataValidationError("new-Capture attestation requires attestation_basis=new_capture")
    return _dispatch_remote_or_local(
        lambda client: _handle_claim_attestation(client, instance_id, request),
        lambda: _handle_claim_attestation(_LocalAttestationClient(), instance_id, request),
        operation_name="cruxible_playbill_claim_attest_new_capture",
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
    *,
    claim_id: str | None = None,
    capture_digest: str | None = None,
) -> contracts.PlaybillAuthoringExampleResult:
    payload = authoring_example(
        name,
        claim_id=claim_id,
        capture_digest=capture_digest,
    )
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
    expectation_id: str | None = None,
) -> contracts.PlaybillInsertionConfirmResultV2:
    request = InsertionConfirmationObservationV2.model_validate(observation)
    return _dispatch_remote_or_local(
        lambda client: client.confirm_playbill_authoring_insertion(
            instance_id,
            intent_id,
            observation=request.model_dump(mode="json"),
            expectation_id=expectation_id,
        ),
        lambda: playbill_api.playbill_authoring_confirm_insertion(
            instance_id,
            intent_id,
            observation=request,
            expectation_id=expectation_id,
        ),
        operation_name="cruxible_playbill_authoring_confirm_insertion",
    )


def handle_playbill_authoring_prepare_publication(
    instance_id: str,
    intent_id: str,
    observation: dict[str, Any],
    expectation_id: str | None = None,
) -> contracts.PlaybillInsertionPrepareResult:
    request = PublicationSourceObservationV2.model_validate(observation)
    return _dispatch_remote_or_local(
        lambda client: client.prepare_playbill_authoring_publication(
            instance_id,
            intent_id,
            observation=request.model_dump(mode="json"),
            expectation_id=expectation_id,
        ),
        lambda: playbill_api.playbill_authoring_prepare_publication(
            instance_id,
            intent_id,
            observation=request,
            expectation_id=expectation_id,
        ),
        operation_name="cruxible_playbill_authoring_prepare_publication",
    )


def handle_playbill_authoring_abandon_insertion(
    instance_id: str,
    intent_id: str,
    expectation_id: str | None = None,
) -> contracts.PlaybillInsertionAbandonResult:
    return _dispatch_remote_or_local(
        lambda client: client.abandon_playbill_authoring_insertion(
            instance_id,
            intent_id,
            expectation_id=expectation_id,
        ),
        lambda: playbill_api.playbill_authoring_abandon_insertion(
            instance_id,
            intent_id,
            expectation_id=expectation_id,
        ),
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
) -> contracts.PlaybillClaimExplanationV2 | contracts.PlaybillClaimExplanationV3:
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


def handle_playbill_policies_in_force(
    instance_id: str,
) -> contracts.PlaybillPolicyInForceList:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_policies_in_force(instance_id),
        lambda: playbill_api.playbill_policies_in_force(instance_id),
        operation_name="cruxible_playbill_policies_in_force",
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


def handle_playbill_procedure_readiness(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str,
) -> contracts.PlaybillProcedureReadiness:
    evaluated_at = parse_datetime(evaluation_time)
    if evaluated_at is None:  # pragma: no cover - required public argument
        raise DataValidationError("Procedure readiness requires evaluation_time")
    return _dispatch_remote_or_local(
        lambda client: client.playbill_procedure_readiness(
            instance_id,
            name,
            evaluation_time=evaluated_at.isoformat(),
        ),
        lambda: playbill_api.playbill_procedure_readiness(
            instance_id,
            name,
            request=ProcedureReadinessRequestV1(evaluation_time=evaluated_at),
        ),
        operation_name="cruxible_playbill_procedure_readiness",
    )


def handle_playbill_procedure_bind(
    instance_id: str,
    name: str,
    *,
    bindings: list[dict[str, Any]],
) -> contracts.PlaybillProcedureBindResult:
    request = ProcedureBindRequestV1.model_validate({"bindings": bindings})
    return _dispatch_remote_or_local(
        lambda client: client.bind_playbill_procedure(
            instance_id,
            name,
            bindings=[item.model_dump(mode="json") for item in request.bindings],
        ),
        lambda: playbill_api.playbill_procedure_bind(instance_id, name, request=request),
        operation_name="cruxible_playbill_procedure_bind",
    )


def handle_playbill_procedure_run(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str | None,
    at: dict[str, Any] | None,
    input: Any,
) -> contracts.PlaybillProcedureRunState:
    evaluated_at = parse_datetime(evaluation_time)
    request = ProcedureRunRequestV2.model_validate(
        {"evaluation_time": evaluated_at, "at": at, "input": input}
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_playbill_procedure(
            instance_id,
            name,
            evaluation_time=(
                None if request.evaluation_time is None else request.evaluation_time.isoformat()
            ),
            at=None if request.at is None else request.at.model_dump(mode="json"),
            input=request.input,
        ),
        lambda: playbill_api.playbill_procedure_run(instance_id, name, request=request),
        operation_name="cruxible_playbill_procedure_run",
    )


def handle_playbill_procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.PlaybillProcedureRunState:
    return _dispatch_remote_or_local(
        lambda client: client.get_playbill_procedure_run(instance_id, run_id),
        lambda: playbill_api.playbill_procedure_run_status(instance_id, run_id),
        operation_name="cruxible_playbill_procedure_run_status",
    )


def handle_playbill_line_run(
    instance_id: str,
    line_identity_digest: str,
    *,
    occurrence_id: str | None,
    evaluation_time: str | None = None,
) -> contracts.PlaybillProcedureRunState:
    request = LineRunRequestV1.model_validate(
        {
            "line_identity_digest": line_identity_digest,
            "occurrence_id": occurrence_id,
            "evaluation_time": (
                None if evaluation_time is None else parse_datetime(evaluation_time)
            ),
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_playbill_line(
            instance_id,
            line_identity_digest,
            occurrence_id=request.occurrence_id,
            evaluation_time=(
                None if request.evaluation_time is None else request.evaluation_time.isoformat()
            ),
        ),
        lambda: playbill_api.playbill_line_run(
            instance_id,
            line_identity_digest,
            request=request,
        ),
        operation_name="cruxible_playbill_line_run",
    )


def handle_playbill_predict(
    instance_id: str,
    request: contracts.PlaybillPredictRequestV1,
) -> contracts.PlaybillPredictResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.predict_playbill(instance_id, request=request),
        lambda: playbill_api.playbill_predict(instance_id, request=request),
        operation_name="cruxible_playbill_predict",
    )


def handle_playbill_settle_prediction(
    instance_id: str,
    prediction_id: str,
    request: contracts.PlaybillSettleRequestV1,
) -> contracts.PlaybillSettleResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.settle_playbill_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        lambda: playbill_api.playbill_settle_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        operation_name="cruxible_playbill_settle",
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


def handle_playbill_since(
    instance_id: str,
    *,
    generation: int,
    at: dict[str, Any] | None,
    access_profile: dict[str, Any] | None,
    max_rows: int,
    max_bytes: int,
    cursor: dict[str, Any] | None,
) -> contracts.PlaybillSinceResult:
    profile = access_profile or {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "mcp-since",
        "permitted_access_classes": ["instance", "public"],
        "disclose_restricted_existence": True,
    }
    request = validate_playbill_since_request(
        {
            "generation": generation,
            "at": at,
            "access_profile": profile,
            "max_rows": max_rows,
            "max_bytes": max_bytes,
            "cursor": cursor,
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.since_playbill(
            instance_id,
            generation=request.generation,
            at=request.at,
            access_profile=request.access_profile,
            max_rows=request.max_rows,
            max_bytes=request.max_bytes,
            cursor=request.cursor,
        ),
        lambda: playbill_api.playbill_since(
            instance_id,
            request=request,
        ),
        operation_name="cruxible_playbill_since",
    )


def handle_playbill_curation_list(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: dict[str, Any] | None,
    workspace_observation: dict[str, Any] | None,
) -> contracts.PlaybillCurationListResult:
    profile = access_profile or {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "mcp-curation",
        "permitted_access_classes": ["instance", "public"],
        "disclose_restricted_existence": True,
    }
    request = {
        "tag": "playbill-curation-list-request-v1",
        "evaluation_time": evaluation_time,
        "access_profile": profile,
        "workspace_observation": workspace_observation,
    }
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_curation(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=profile,
            workspace_observation=workspace_observation,
        ),
        lambda: playbill_api.playbill_curation_list(instance_id, request=request),
        operation_name="cruxible_playbill_curation_list",
    )


def handle_playbill_audit(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: dict[str, Any] | None,
    claim_type_identities: list[str],
    subject_kinds: list[str],
    max_rows: int,
    max_bytes: int,
    cursor: dict[str, Any] | None,
) -> contracts.PlaybillAuditResult:
    profile = access_profile or {
        "tag": "playbill-coverage-access-profile-v1",
        "profile_id": "mcp-audit",
        "permitted_access_classes": ["instance", "public"],
        "disclose_restricted_existence": True,
    }
    ordered_claim_types = tuple(
        sorted(set(claim_type_identities), key=lambda item: item.encode("utf-8"))
    )
    ordered_subject_kinds = tuple(sorted(set(subject_kinds), key=lambda item: item.encode("utf-8")))
    request = {
        "tag": "playbill-audit-request-v1",
        "evaluation_time": evaluation_time,
        "access_profile": profile,
        "scope": {
            "tag": "playbill-audit-scope-v1",
            "claim_type_identities": list(ordered_claim_types),
            "subject_kinds": list(ordered_subject_kinds),
        },
        "budget": {
            "tag": "playbill-audit-budget-v1",
            "max_rows": max_rows,
            "max_bytes": max_bytes,
        },
        "cursor": cursor,
    }
    return _dispatch_remote_or_local(
        lambda client: client.audit_playbill(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=profile,
            claim_type_identities=ordered_claim_types,
            subject_kinds=ordered_subject_kinds,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        ),
        lambda: playbill_api.playbill_audit(instance_id, request=request),
        operation_name="cruxible_playbill_audit",
    )


def handle_playbill_curation_overrule(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: list[str],
) -> contracts.PlaybillCurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.overrule_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=tuple(attribution_refs),
        ),
        lambda: playbill_api.playbill_curation_overrule(
            instance_id,
            request={
                "tag": "playbill-curation-overrule-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "attribution_refs": attribution_refs,
            },
        ),
        operation_name="cruxible_playbill_curation_overrule",
    )


def handle_playbill_curation_accept_fixed(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    accepted_proposal_id: str,
    accepted_changeset_digest: str,
    attribution_refs: list[str],
) -> contracts.PlaybillCurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.accept_fixed_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=tuple(attribution_refs),
        ),
        lambda: playbill_api.playbill_curation_accept_fixed(
            instance_id,
            request={
                "tag": "playbill-curation-accept-fixed-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "accepted_proposal_id": accepted_proposal_id,
                "accepted_changeset_digest": accepted_changeset_digest,
                "attribution_refs": attribution_refs,
            },
        ),
        operation_name="cruxible_playbill_curation_accept_fixed",
    )


def handle_playbill_curation_suppress(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    scope: Literal["item", "pattern", "instance"],
    until_generation: int | None,
    attribution_refs: list[str],
) -> contracts.PlaybillCurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.suppress_playbill_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=tuple(attribution_refs),
        ),
        lambda: playbill_api.playbill_curation_suppress(
            instance_id,
            request={
                "tag": "playbill-curation-suppress-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "scope": scope,
                "until_generation": until_generation,
                "attribution_refs": attribution_refs,
            },
        ),
        operation_name="cruxible_playbill_curation_suppress",
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


def handle_playbill_export_floor(instance_id: str) -> contracts.PlaybillFloorExport:
    return _dispatch_remote_or_local(
        lambda client: client.export_playbill_floor(instance_id),
        lambda: playbill_api.playbill_export_floor(instance_id),
        operation_name="cruxible_playbill_export_floor",
    )


def handle_playbill_workspace_floor_export(
    instance_id: str,
    *,
    force: bool,
) -> contracts.PlaybillWorkspaceFloorWriteResult:
    workspace = mcp_git_workspace_root()
    export = handle_playbill_export_floor(instance_id)
    return materialize_playbill_floor(
        workspace,
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
        mcp_git_workspace_root(),
        current_coordinate=search.coordinate,
    )
