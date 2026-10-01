"""Playbill-only MCP handler implementations."""

from __future__ import annotations

import base64
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from cruxible_client import (
    CruxibleClient,
    activate_with_workspace_refresh,
    contracts,
    inspect_workspace_floor,
    observe_playbill_next_workspace,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
    local_attestation_signer_from_environment,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import AuthoringInputV1, ClaimInput
from cruxible_client.authoring.signing import LocalEd25519ApprovalSigner
from cruxible_client.authoring.sources import (
    compile_client_source_context,
    load_source_catalog,
    mapped_root_aliases,
)
from cruxible_client.authoring.workspace import (
    floor_export_parts,
    observe_playbill_next_workspace_with_coverage,
    workspace_floor_freshness,
    write_workspace_floor,
)
from cruxible_client.authoring.write_evidence import observe_changes, observe_evidence
from cruxible_client.contracts.attestations import ApprovalAttestation, ApprovalStatement
from cruxible_client.contracts.capture_reads import CaptureReadRequestV1, CaptureReadV1
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
    ClaimAttestationCaptureReferenceV1,
    ClaimStance,
    PreparedClaimAttestationRequestV1,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequestV1,
    ClaimTypeUpgradeResultV1,
)
from cruxible_client.contracts.declared_blocks import PROJECTION_STAMP_ADAPTER
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.evidence_rule_upgrade import EvidenceRuleUpgradeResultV1
from cruxible_client.contracts.get_reads import (
    PlaybillByteRangeV1,
    PlaybillGetRequestV1,
    PlaybillGetResultV1,
)
from cruxible_client.contracts.governance import governance_identifier
from cruxible_client.contracts.kits import (
    PlaybillKitAddRequestV1,
    PlaybillKitBuildRequestV1,
    PlaybillKitBuildResultV1,
    PlaybillKitChangeResultV1,
    PlaybillKitRemoveRequestV1,
    PlaybillKitStatusV1,
)
from cruxible_client.contracts.provider_installation import (
    PlaybillProviderCatalogV1,
    PlaybillProviderInstallRequestV1,
    PlaybillProviderInstallResultV1,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionSpecV1
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.write import (
    FileEvidence,
    PlaybillRetireRequestV1,
    PlaybillSetRequestV1,
    PlaybillWriteRequestV1,
    WriteOutcome,
)
from cruxible_client.errors import DaemonOperationScopeError as ClientDaemonOperationScopeError
from cruxible_client.errors import ServerUnreachableError
from cruxible_client.transport.http import configured_principal_id
from cruxible_core import __version__
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputV1,
)
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequest
from cruxible_core.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.coverage.contracts import CoverageAccessProfileV1, CoverageCardBudgetV1
from cruxible_core.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.coverage.workspace import (
    bindings_from_mapping,
    observe_workspace,
)
from cruxible_core.errors import ConfigError, DaemonOperationScopeError, DataValidationError
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.mcp.results import McpServerInfoResult, McpWhoAmIResult
from cruxible_core.mcp.target import configured_instance_id
from cruxible_core.mcp.workspace import (
    mcp_approval_key_dir,
    mcp_custody_forbidden_roots,
    mcp_git_workspace_root,
    mcp_workspace_root,
    optional_mcp_git_workspace_root,
    resolve_workspace_path,
)
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings
from cruxible_core.server.playbill_request_models import (
    PlaybillApprovalRequest,
    PlaybillAuthoringInputCompileRequest,
    PlaybillAuthoringInputCreateRequest,
    PlaybillAuthoringPreflightRequest,
    PlaybillAuthoringRebaseRequest,
    PlaybillAuthoringSubmitRequest,
    PlaybillBlockDeclareRequest,
    PlaybillBlockDepublishRequest,
    PlaybillCompilerUpgradeRequest,
    PlaybillCurationAcceptFixedRequest,
    PlaybillCurationOverruleRequest,
    PlaybillCurationSuppressRequest,
    PlaybillInitRequest,
    PlaybillInsertionAbandonRequest,
    PlaybillProposalReadmitRequest,
    PlaybillProposalWithdrawRequest,
    PlaybillProposeClaimTypeInputRequest,
    PlaybillProposeDocumentRequest,
    PlaybillProposePrincipalRequest,
    PlaybillSourceProposeRequest,
    PlaybillStoreBodyRequest,
)
from cruxible_core.service.discovery.since import validate_playbill_since_request
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequestV1,
    ProcedureBindRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV2,
)

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None, str | None] | None = None
_client_cache_lock = threading.RLock()
ResultT = TypeVar("ResultT")
_AUTHORING_INPUT: TypeAdapter[AuthoringInputV1] = TypeAdapter(AuthoringInputV1)
_CLAIM_TYPE_MIGRATION: TypeAdapter[ClaimTypeMigrationRequest] = TypeAdapter(
    ClaimTypeMigrationRequest
)


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
        include: Sequence[contracts.PlaybillFloorExportPart] = (),
    ) -> contracts.PlaybillFloorExport:
        if at is not None:  # pragma: no cover - shared refresh always asks for current
            raise DataValidationError("local floor adapter accepts only the current coordinate")
        return playbill_api.playbill_export_floor(instance_id, include=tuple(include))

    def check_playbill_projection_blocks(
        self, instance_id: str, *, request: contracts.PlaybillProjectionCheckRequestV1
    ) -> contracts.PlaybillProjectionCheckResultV1:
        return playbill_api.playbill_check_projection_blocks(instance_id, request=request)

    def read_playbill_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: contracts.PlaybillBlockSyncReadRequestV1,
    ) -> contracts.PlaybillBlockSyncReadResultV1:
        return playbill_api.playbill_read_block_sync_backing(instance_id, request=request)


class _LocalCoverageClient:
    """Serve the shared next-workspace coverage scan in library mode."""

    def resolve_playbill_coverage(
        self,
        instance_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        at: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
        scan_budget: Mapping[str, Any] | None = None,
    ) -> contracts.PlaybillCoverageResult:
        return playbill_api.playbill_resolve_coverage(
            instance_id,
            observations=tuple(
                WorkingSourceObservationV1.model_validate(item) for item in observations
            ),
            at=None if at is None else AcceptedCoordinate.model_validate(_json(at)),
            budget=None if budget is None else CoverageCardBudgetV1.model_validate(budget),
            scan_budget=(
                None if scan_budget is None else CoverageScanBudgetV1.model_validate(scan_budget)
            ),
        )

    def playbill_head(self, instance_id: str) -> contracts.PlaybillHeadV1:
        return playbill_api.playbill_head(instance_id)


def _json(value: contracts.PlaybillAcceptedCoordinate | Mapping[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)


class _LocalSourceContextClient:
    """Supply accepted context to the shared local compiler in library mode."""

    def playbill_source_context(self, instance_id: str) -> contracts.PlaybillSourceContext:
        return playbill_api.playbill_source_context(instance_id)


class _LocalAttestationClient:
    """Expose the client-side signing adapter without giving the daemon a key."""

    def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
        return playbill_api.playbill_whoami(instance_id)

    def orient_playbill(
        self,
        instance_id: str,
        *,
        section: contracts.PlaybillOrientSection,
        limit: int,
        cursor: str | None = None,
    ) -> contracts.PlaybillOrientResultV1:
        return playbill_api.playbill_orient(
            instance_id, section=section, limit=limit, cursor=cursor, surface="sdk"
        )

    def playbill_get(
        self, instance_id: str, *, request: PlaybillGetRequestV1
    ) -> PlaybillGetResultV1:
        return playbill_api.playbill_get(instance_id, request=request)

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
    principal_id = configured_principal_id()
    cache_key = (settings.server_url, settings.server_socket, token, principal_id)
    with _client_cache_lock:
        if _client_cache is None or _client_cache_key != cache_key:
            reset_client_cache()
            _client_cache = CruxibleClient(
                base_url=settings.server_url,
                socket_path=settings.server_socket,
                token=token,
                principal_id=principal_id,
            )
            _client_cache_key = cache_key
        return _client_cache


#: The served request model each mutating MCP operation is validated through
#: when it runs in process. An MCP client with no daemon reaches the facade
#: directly, which used to mean it skipped whatever the HTTP route's request
#: model checks beyond the facade's own signature -- a control character in a
#: decommission reason passed the MCP door and then raised the raw pydantic
#: error from inside the write, where it renders as an untyped failure rather
#: than a refusal the caller can read. One seam, so the local door and the
#: served door reach the same DECISION -- the same model, the same validators,
#: on the same payload. Not the same rendering: over HTTP the route answers 422
#: with pydantic's structured error body, and in process the local door raises
#: a typed `DataValidationError` naming the operation. Same verdict, two
#: shapes, because in process there is no HTTP envelope to put the other one
#: in.
#:
#: `None` is a declaration, not an omission: that route carries no request body,
#: so there is no second model to agree with. The guardrail in
#: `tests/test_architecture/test_mcp_validation_seam.py` requires every mutating
#: operation to appear here and every entry with a model to be given a payload.
MCP_LOCAL_REQUEST_MODELS: dict[str, TypeAdapter[Any] | None] = {
    "cruxible_playbill_line_dispatch": TypeAdapter(contracts.LineDispatchRequestV1),
    "cruxible_playbill_line_evaluate": TypeAdapter(contracts.LineEvaluateRequestV1),
    "cruxible_playbill_line_arm": None,  # path only
    "cruxible_playbill_line_disarm": None,  # path only
    "cruxible_playbill_provider_install": TypeAdapter(PlaybillProviderInstallRequestV1),
    "cruxible_playbill_kit_add": TypeAdapter(PlaybillKitAddRequestV1),
    "cruxible_playbill_kit_remove": TypeAdapter(PlaybillKitRemoveRequestV1),
    "cruxible_playbill_evidence_rules_upgrade": None,  # path only
    "cruxible_playbill_claim_type_upgrade": TypeAdapter(ClaimTypeUpgradeRequestV1),
    "cruxible_playbill_activate": None,  # path only
    "cruxible_playbill_authoring_abandon_insertion": TypeAdapter(PlaybillInsertionAbandonRequest),
    "cruxible_playbill_authoring_bind": TypeAdapter(PlaybillAuthoringInputCompileRequest),
    "cruxible_playbill_authoring_compile": TypeAdapter(PlaybillAuthoringInputCompileRequest),
    "cruxible_playbill_authoring_create": TypeAdapter(PlaybillAuthoringInputCreateRequest),
    "cruxible_playbill_authoring_preflight": TypeAdapter(PlaybillAuthoringPreflightRequest),
    "cruxible_playbill_authoring_rebase": TypeAdapter(PlaybillAuthoringRebaseRequest),
    "cruxible_playbill_authoring_submit": TypeAdapter(PlaybillAuthoringSubmitRequest),
    "cruxible_playbill_block_declare": TypeAdapter(PlaybillBlockDeclareRequest),
    "cruxible_playbill_block_depublish": TypeAdapter(PlaybillBlockDepublishRequest),
    "cruxible_playbill_claim_attest": None,  # shared preparation helper builds the body
    "cruxible_playbill_set": TypeAdapter(PlaybillSetRequestV1),
    "cruxible_playbill_retire": TypeAdapter(PlaybillRetireRequestV1),
    "cruxible_playbill_write": TypeAdapter(PlaybillWriteRequestV1),
    "cruxible_playbill_claim_type_migrate": TypeAdapter(ClaimTypeMigrationRequest),
    "cruxible_playbill_curation_accept_fixed": TypeAdapter(PlaybillCurationAcceptFixedRequest),
    "cruxible_playbill_curation_overrule": TypeAdapter(PlaybillCurationOverruleRequest),
    "cruxible_playbill_curation_suppress": TypeAdapter(PlaybillCurationSuppressRequest),
    "cruxible_playbill_read_capture": TypeAdapter(CaptureReadRequestV1),
    "cruxible_playbill_init": TypeAdapter(PlaybillInitRequest),
    "cruxible_playbill_predict": TypeAdapter(contracts.PlaybillPredictRequestV2),
    "cruxible_playbill_procedure_bind": TypeAdapter(ProcedureBindRequestV1),
    "cruxible_playbill_proposal_readmit": TypeAdapter(PlaybillProposalReadmitRequest),
    "cruxible_playbill_proposal_withdraw": TypeAdapter(PlaybillProposalWithdrawRequest),
    "cruxible_playbill_propose_claim_type": TypeAdapter(PlaybillProposeClaimTypeInputRequest),
    "cruxible_playbill_propose_document": TypeAdapter(PlaybillProposeDocumentRequest),
    "cruxible_playbill_compiler_upgrade": TypeAdapter(PlaybillCompilerUpgradeRequest),
    "cruxible_playbill_propose_principal_change": TypeAdapter(PlaybillProposePrincipalRequest),
    "cruxible_playbill_propose_source_bundle": TypeAdapter(PlaybillSourceProposeRequest),
    "cruxible_playbill_procedure_measure": TypeAdapter(contracts.PlaybillProcedureMeasureRequestV1),
    "cruxible_playbill_settle": TypeAdapter(contracts.PlaybillSettleRequestV2),
    "cruxible_playbill_store_body": TypeAdapter(PlaybillStoreBodyRequest),
    "cruxible_playbill_submit_approval": TypeAdapter(PlaybillApprovalRequest),
}


def _validate_local_request(operation_name: str, payload: Mapping[str, Any]) -> None:
    """Refuse locally whatever the served route's request model refuses.

    The decision is the served one, byte for byte: the same model object the
    route binds, so a payload the route rejects is rejected here for the same
    reason. What differs is the shape it comes back in -- a typed
    `DataValidationError` naming the operation rather than the route's 422 --
    because a library caller has no HTTP response to read. It is not the served
    refusal; it is the served refusal's verdict, rendered for the door it came
    through.
    """

    model = MCP_LOCAL_REQUEST_MODELS.get(operation_name)
    if model is None:
        return
    try:
        model.validate_python(dict(payload))
    except ValidationError as exc:
        raise DataValidationError(f"{operation_name}: {exc}") from exc


def _dispatch_remote_or_local(
    remote_call: Callable[[CruxibleClient], ResultT],
    local_call: Callable[[], ResultT],
    *,
    allow_local: bool = True,
    operation_name: str,
    local_payload: Mapping[str, Any] | None = None,
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
    if local_payload is not None:
        _validate_local_request(operation_name, local_payload)
    return local_call()


def _daemon_version() -> str:
    """The daemon's public version probe; in library mode this process is the daemon."""

    return _dispatch_remote_or_local(
        lambda client: client.version(),
        lambda: __version__,
        operation_name="daemon version probe (GET /version)",
    )


def handle_server_info() -> McpServerInfoResult:
    """Answer a daemon-scope caller with daemon metadata, a scoped one with its instance."""

    try:
        daemon = _dispatch_remote_or_local(
            lambda client: client.server_info(),
            host_api.server_info,
            operation_name="cruxible_server_info",
        )
    except (ClientDaemonOperationScopeError, DaemonOperationScopeError) as exc:
        scope = exc.credential_scope
        return McpServerInfoResult(
            scope="instance",
            instance_id=scope,
            adapter_version=__version__,
            daemon_version=_daemon_version(),
            host=_dispatch_remote_or_local(
                lambda client: client.show_playbill_host(scope),
                lambda: host_api.show_playbill_host(scope),
                operation_name="cruxible_server_info",
            ),
            identity=_playbill_whoami(scope),
        )
    return McpServerInfoResult(
        scope="daemon",
        instance_id=configured_instance_id(),
        adapter_version=__version__,
        daemon_version=daemon.version,
        daemon=daemon,
    )


def handle_playbill_init(
    instance_id: str,
    principals: list[dict[str, Any]],
    operating_profile: str,
    require_independent_approval: bool = False,
    *,
    git_object_format: str | None = None,
) -> contracts.PlaybillInitResult:
    records = tuple(PrincipalRecord.model_validate(item) for item in principals)
    return _dispatch_remote_or_local(
        lambda client: client.init_playbill(
            instance_id,
            principals=[item.model_dump(mode="json") for item in records],
            operating_profile=cast(Any, operating_profile),
            require_independent_approval=require_independent_approval,
            git_object_format=cast(Any, git_object_format),
        ),
        lambda: playbill_api.playbill_init(
            instance_id,
            principals=records,
            operating_profile=cast(Any, operating_profile),
            require_independent_approval=require_independent_approval,
            git_object_format=cast(Any, git_object_format),
        ),
        operation_name="cruxible_playbill_init",
        local_payload={
            "principals": [item.model_dump(mode="json") for item in records],
            "operating_profile": operating_profile,
            "require_independent_approval": require_independent_approval,
            "git_object_format": git_object_format,
        },
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
        local_payload={"content_base64": content_base64},
    )


def handle_playbill_provider_catalog(instance_id: str) -> PlaybillProviderCatalogV1:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_provider_packages(instance_id),
        lambda: playbill_api.playbill_provider_catalog(instance_id),
        operation_name="cruxible_playbill_provider_catalog",
    )


def handle_playbill_provider_install(
    instance_id: str,
    request: PlaybillProviderInstallRequestV1,
) -> PlaybillProviderInstallResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.install_playbill_provider(instance_id, request),
        lambda: playbill_api.playbill_provider_install(instance_id, request),
        operation_name="cruxible_playbill_provider_install",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_kit_build(
    instance_id: str, request: PlaybillKitBuildRequestV1
) -> PlaybillKitBuildResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.build_playbill_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_build(instance_id, request),
        operation_name="cruxible_playbill_kit_build",
    )


def handle_playbill_kit_status(instance_id: str) -> PlaybillKitStatusV1:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_kit_status(instance_id),
        lambda: playbill_api.playbill_kit_status(instance_id),
        operation_name="cruxible_playbill_kit_status",
    )


def handle_playbill_kit_add(
    instance_id: str, request: PlaybillKitAddRequestV1
) -> PlaybillKitChangeResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.add_playbill_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_add(instance_id, request),
        operation_name="cruxible_playbill_kit_add",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_claim_type_upgrade(
    instance_id: str, request: ClaimTypeUpgradeRequestV1
) -> ClaimTypeUpgradeResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.upgrade_playbill_claim_types(instance_id, request),
        lambda: playbill_api.playbill_claim_type_upgrade(instance_id, request),
        operation_name="cruxible_playbill_claim_type_upgrade",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_evidence_rules_upgrade(instance_id: str) -> EvidenceRuleUpgradeResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.upgrade_playbill_evidence_rules(instance_id),
        lambda: playbill_api.playbill_evidence_rules_upgrade(instance_id),
        operation_name="cruxible_playbill_evidence_rules_upgrade",
    )


def handle_playbill_kit_remove(
    instance_id: str, request: PlaybillKitRemoveRequestV1
) -> PlaybillKitChangeResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.remove_playbill_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_remove(instance_id, request),
        operation_name="cruxible_playbill_kit_remove",
        local_payload=request.model_dump(mode="json"),
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
        local_payload={
            "shell": document.model_dump(mode="json"),
            "proposal_name": proposal_name,
            "source_compilation_digest": source_compilation_digest,
        },
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
        local_payload={"attestation": public_attestation.model_dump(mode="json")},
    )


def handle_playbill_approve(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str | None,
    candidate_digest: str | None,
) -> contracts.PlaybillApprovalReceipt:
    """Challenge, sign with the configured local key, and submit, as `proposal approve` does.

    Only the public attestation leaves this process; the key's bytes and path
    never enter a result or a log line.
    """

    key_dir = mcp_approval_key_dir()
    signer = signer_id if signer_id is not None else _sole_approval_signer(key_dir)
    try:
        governance_identifier(signer, label="signer_id")
    except ValueError as exc:
        raise DataValidationError(f"cruxible_playbill_approve: {exc}") from exc
    challenge = handle_playbill_prepare_approval(
        instance_id, proposal_id, signer_id=signer, include_body=False
    )
    statement = ApprovalStatement.model_validate(challenge.statement)
    if candidate_digest is not None and statement.payload_digest != candidate_digest:
        raise DataValidationError(
            f"cruxible_playbill_approve: proposal {proposal_id} now signs candidate "
            f"{statement.payload_digest}, not the reviewed {candidate_digest}; review it again"
        )
    principal = PrincipalRecord.model_validate(challenge.signer_principal)
    key_signer = LocalEd25519ApprovalSigner.open(
        signer_id=signer,
        private_key_path=key_dir / f"{signer}.ed25519",
        expected_public_key=principal.public_key,
        forbidden_roots=mcp_custody_forbidden_roots(),
    )
    attestation = key_signer.sign(statement)
    return handle_playbill_submit_approval(
        instance_id, proposal_id, attestation.model_dump(mode="json")
    )


def _sole_approval_signer(key_dir: Path) -> str:
    signers = sorted(path.name.removesuffix(".ed25519") for path in key_dir.glob("*.ed25519"))
    if len(signers) != 1:
        found = ", ".join(signers) if signers else "none"
        raise DataValidationError(
            "cruxible_playbill_approve: pass signer_id; the configured key directory holds "
            f"{len(signers)} approval keys (signers: {found})"
        )
    return signers[0]


def handle_playbill_activate(
    instance_id: str, proposal_id: str
) -> contracts.PlaybillWorkspaceActivationResult:
    workspace = optional_mcp_git_workspace_root()
    if workspace is None:
        # Activation is a daemon act; the floor refresh and block sync are local
        # conveniences that need a worktree, so their absence must not refuse it.
        activation = _dispatch_remote_or_local(
            lambda client: client.activate_playbill_proposal(instance_id, proposal_id),
            lambda: playbill_api.playbill_activate(instance_id, proposal_id),
            operation_name="cruxible_playbill_activate",
        )
        return contracts.PlaybillWorkspaceActivationResult(
            **activation.model_dump(mode="json"),
            floor_refresh=contracts.PlaybillFloorRefreshResult(
                status="not_configured",
                message=(
                    "floor refresh skipped: the MCP workspace root is not inside a Git "
                    "worktree (set CRUXIBLE_MCP_WORKSPACE_ROOT to one to refresh the floor)"
                ),
            ),
        )
    return _dispatch_remote_or_local(
        lambda client: activate_with_workspace_refresh(
            client, instance_id, proposal_id, workspace=workspace
        ),
        lambda: activate_with_workspace_refresh(
            _LocalFloorClient(), instance_id, proposal_id, workspace=workspace
        ),
        operation_name="cruxible_playbill_activate",
    )


def _playbill_whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_whoami(instance_id),
        lambda: playbill_api.playbill_whoami(instance_id),
        operation_name="cruxible_playbill_whoami",
    )


def handle_playbill_whoami(instance_id: str) -> McpWhoAmIResult:
    return McpWhoAmIResult(
        instance_id=instance_id,
        adapter_version=__version__,
        daemon_version=_daemon_version(),
        identity=_playbill_whoami(instance_id),
    )


def handle_playbill_orient(
    instance_id: str,
    *,
    kind: str | None = None,
    section: contracts.PlaybillOrientSection | None = None,
    limit: int = contracts.PLAYBILL_ORIENT_DEFAULT_LIMIT,
    cursor: str | None = None,
    at: str | contracts.PlaybillAcceptedCoordinate | None = None,
    evaluation_time: str | None = None,
) -> contracts.PlaybillOrientResultV1:
    """The orient map, with its follow-up calls rendered as MCP tool calls."""

    from cruxible_core.mcp.curation import session_tool_names

    tools = tuple(sorted(session_tool_names()))
    result = _dispatch_remote_or_local(
        lambda client: client.orient_playbill(
            instance_id,
            kind=kind,
            section=section,
            limit=limit,
            cursor=cursor,
            at=at,
            evaluation_time=evaluation_time,
            surface="mcp",
            caller_tools=tools,
        ),
        lambda: playbill_api.playbill_orient(
            instance_id,
            kind=kind,
            section=section,
            limit=limit,
            cursor=cursor,
            at=at
            if at is None or isinstance(at, str)
            else AcceptedCoordinate.model_validate(_json(at)),
            evaluation_time=parse_datetime(evaluation_time),
            surface="mcp",
            caller_tools=tools,
        ),
        operation_name="cruxible_playbill_orient",
    )
    try:
        workspace = optional_mcp_git_workspace_root()
    except ConfigError:
        workspace = None
    return result if workspace is None else workspace_floor_freshness(workspace, result)


def handle_playbill_list_proposals(
    instance_id: str,
    status: str | None,
    *,
    limit: int = contracts.PLAYBILL_PROPOSAL_LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> contracts.PlaybillProposalList:
    normalized = cast(Any, status)
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_proposals(
            instance_id, status=normalized, limit=limit, cursor=cursor
        ),
        lambda: playbill_api.playbill_list_proposals(
            instance_id, status=normalized, limit=limit, cursor=cursor
        ),
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
        local_payload={},
    )


def handle_playbill_withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    reason: str,
) -> contracts.PlaybillProposalWithdrawResult:
    return _dispatch_remote_or_local(
        lambda client: client.withdraw_playbill_proposal(
            instance_id,
            proposal_id,
            reason=reason,
        ),
        lambda: playbill_api.playbill_withdraw_proposal(instance_id, proposal_id, reason),
        operation_name="cruxible_playbill_proposal_withdraw",
        local_payload={"reason": reason},
    )


def handle_playbill_read_capture(instance_id: str, request: CaptureReadRequestV1) -> CaptureReadV1:
    return _dispatch_remote_or_local(
        lambda client: client.read_playbill_capture(instance_id, request),
        lambda: playbill_api.playbill_read_capture(instance_id, request),
        operation_name="cruxible_playbill_read_capture",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_source_context(instance_id: str) -> contracts.PlaybillSourceContext:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_source_context(instance_id),
        lambda: playbill_api.playbill_source_context(instance_id),
        operation_name="cruxible_playbill_source_context",
    )


def handle_playbill_source_check(
    instance_id: str,
    *,
    bundle: dict[str, Any] | None = None,
    catalog_path: str | None = None,
    repository_root: str = ".",
    local_catalog_path: str | None = None,
    root_aliases: Mapping[str, str] | None = None,
) -> contracts.PlaybillSourceCheckResult:
    """Check a compiled bundle, or compile catalog-declared workspace sources first."""

    if (bundle is None) == (catalog_path is None):
        raise DataValidationError(
            "source check takes exactly one of bundle or catalog_path (workspace sources)"
        )
    if bundle is not None and (
        repository_root != "." or local_catalog_path is not None or root_aliases
    ):
        raise DataValidationError(
            "repository_root, local_catalog_path, and root_aliases apply only with catalog_path"
        )
    frozen = (
        SourceCompilationBundle.model_validate(bundle)
        if catalog_path is None
        else handle_playbill_workspace_source_compile(
            instance_id,
            catalog_path=catalog_path,
            repository_root=repository_root,
            local_catalog_path=local_catalog_path,
            root_aliases=root_aliases or {},
        )
    )
    return _dispatch_remote_or_local(
        lambda client: client.check_playbill_source_bundle(
            instance_id, bundle=frozen.model_dump(mode="json")
        ),
        lambda: playbill_api.playbill_check_source_bundle(instance_id, bundle=frozen),
        operation_name="cruxible_playbill_source_check",
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
        local_payload={
            "bundle": frozen.model_dump(mode="json"),
            "source_name": source_name,
            "proposal_name": proposal_name,
        },
    )


def handle_playbill_compiler_upgrade(
    instance_id: str,
    target_compiler_digest: str,
    base: dict[str, Any],
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    request = PlaybillCompilerUpgradeRequest.model_validate(
        {
            "target": {"rule_digest": target_compiler_digest},
            "base": base,
            "proposal_name": proposal_name,
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.propose_playbill_compiler_upgrade(
            instance_id,
            target=request.target,
            base=request.base,
            proposal_name=proposal_name,
        ),
        lambda: playbill_api.playbill_propose_compiler_upgrade(
            instance_id,
            target=request.target,
            base=request.base,
            proposal_name=proposal_name,
        ),
        operation_name="cruxible_playbill_compiler_upgrade",
        local_payload=request.model_dump(mode="json"),
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
        local_payload={
            "principal": record.model_dump(mode="json"),
            "proposal_name": proposal_name,
        },
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
        local_payload={
            "input": request.model_dump(mode="json"),
            "proposal_name": proposal_name,
        },
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
        local_payload=migration.model_dump(mode="json"),
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
    valid_until: datetime | None = None,
    *,
    capture_digests: list[str] | None = None,
    referent_coordinate: Mapping[str, Any] | None = None,
    attested_at: datetime | None = None,
) -> ClaimAttestationAppendResultV1:
    """Attest the examined Claim, or a new Capture of it when capture digests are given."""

    if capture_digests is not None and not capture_digests:
        raise DataValidationError(
            "capture_digests must name at least one new Capture; omit it to attest the "
            "Claim's own citations"
        )
    if referent_coordinate is not None and not capture_digests:
        raise DataValidationError("referent_coordinate applies only with capture_digests")
    if attested_at is not None and not capture_digests:
        raise DataValidationError(
            "attested_at applies only with capture_digests; an examined-Claim attestation "
            "is signed at the time of the call"
        )
    try:
        prepared = PreparedClaimAttestationRequestV1(
            claim_id=claim_id.removeprefix("Claim:"),
            attestation_basis="new_capture" if capture_digests else "examined_existing",
            stance=stance,
            capture_references=tuple(
                ClaimAttestationCaptureReferenceV1(capture_digest=digest)
                for digest in sorted(set(capture_digests or ()), key=lambda d: d.encode())
            ),
            referent_coordinate=(
                None
                if referent_coordinate is None
                else AcceptedCoordinate.model_validate(referent_coordinate)
            ),
            attested_at=datetime.now(UTC) if attested_at is None else attested_at,
            valid_until=valid_until,
            note=note,
        )
    except ValidationError as exc:
        raise DataValidationError(f"cruxible_playbill_claim_attest: {exc}") from exc
    return _dispatch_remote_or_local(
        lambda client: _handle_claim_attestation(client, instance_id, prepared),
        lambda: _handle_claim_attestation(_LocalAttestationClient(), instance_id, prepared),
        operation_name="cruxible_playbill_claim_attest",
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
        local_payload={"input": request.model_dump(mode="json")},
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
        local_payload={"input": request.model_dump(mode="json"), "intent_id": intent_id},
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
        local_payload={"input": bound.model_dump(mode="json"), "intent_id": None},
    )


def handle_playbill_authoring_preflight(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringPreflightResult:
    return _dispatch_remote_or_local(
        lambda client: client.preflight_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_preflight(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_preflight",
        local_payload={},
    )


def handle_playbill_authoring_rebase(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    return _dispatch_remote_or_local(
        lambda client: client.rebase_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_rebase(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_rebase",
        local_payload={},
    )


def handle_playbill_authoring_submit(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringSubmitResult:
    return _dispatch_remote_or_local(
        lambda client: client.submit_playbill_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_submit(instance_id, intent_id),
        operation_name="cruxible_playbill_authoring_submit",
        local_payload={},
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
        local_payload={"expectation_id": expectation_id},
    )


def handle_playbill_block_declare(
    instance_id: str,
    stamp: Mapping[str, Any],
) -> contracts.PlaybillBlockDeclareResultV1:
    parsed = PROJECTION_STAMP_ADAPTER.validate_python(dict(stamp))
    return _dispatch_remote_or_local(
        lambda client: client.declare_playbill_block(instance_id, parsed.model_dump(mode="json")),
        lambda: playbill_api.playbill_block_declare(instance_id, parsed),
        operation_name="cruxible_playbill_block_declare",
        local_payload={"stamp": parsed.model_dump(mode="json")},
    )


def handle_playbill_block_depublish(
    instance_id: str,
    source_id: str,
    block_id: str,
) -> contracts.PlaybillBlockDepublishResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.depublish_playbill_block(instance_id, source_id, block_id),
        lambda: playbill_api.playbill_block_depublish(instance_id, source_id, block_id),
        operation_name="cruxible_playbill_block_depublish",
        local_payload={"source_id": source_id, "block_id": block_id},
    )


def handle_playbill_get(
    instance_id: str,
    *,
    ref: str,
    detail: str = "summary",
    range: PlaybillByteRangeV1 | None = None,
    at: contracts.PlaybillAcceptedCoordinate | str | None = None,
    evaluation_time: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> PlaybillGetResultV1:
    try:
        request = PlaybillGetRequestV1.model_validate(
            {
                "ref": ref,
                "detail": detail,
                "range": range,
                "at": at,
                "evaluation_time": evaluation_time,
                "surface": "mcp",
                "limit": limit,
                "cursor": cursor,
            }
        )
    except ValidationError as exc:
        raise DataValidationError(
            "Invalid get request; example: "
            '{"ref": "CLM-0123abcd", "detail": "evidence"} or '
            '{"ref": "Document:design", "detail": "body", "range": {"start": 0, "end": 4096}}',
            errors=[
                f"$.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_url=False)
            ],
        ) from exc
    return _dispatch_remote_or_local(
        lambda client: client.playbill_get(instance_id, request=request),
        lambda: playbill_api.playbill_get(instance_id, request=request),
        operation_name="cruxible_playbill_get",
    )


def _playbill_query_request(
    tool: str, evaluation_time: str | None, fields: Mapping[str, Any]
) -> contracts.PlaybillQueryRequestV1:
    try:
        return contracts.PlaybillQueryRequestV1.model_validate(
            {
                **{name: value for name, value in fields.items() if value is not None},
                "evaluation_time": parse_datetime(evaluation_time),
            }
        )
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'request'}: {error['msg']}"
            for error in exc.errors()
        )
        raise DataValidationError(f"{tool}: {problems}") from exc


def handle_playbill_query(
    instance_id: str,
    *,
    evaluation_time: str | None = None,
    **fields: Any,
) -> contracts.PlaybillQueryResult:
    """Build one typed compact or named ``query`` request and answer it."""
    request = _playbill_query_request("cruxible_playbill_query", evaluation_time, fields)
    return _dispatch_remote_or_local(
        lambda client: client.query_playbill(instance_id, request=request),
        lambda: playbill_api.playbill_query(instance_id, request=request),
        operation_name="cruxible_playbill_query",
    )


_WRITE_EXAMPLES = {
    "cruxible_playbill_set": (
        '{"subject": "dev.roadmap_item/tidy-cli", "field": "adoption_state", '
        '"value": "adopted", "because": "Agreed in review."}'
    ),
    "cruxible_playbill_retire": (
        '{"target": "CLM-0123456789abcdef0123456789abcdef", "because": "Stated in error."}'
    ),
    "cruxible_playbill_write": (
        '{"changes": [{"op": "add", "subject": "dev.card/c1", "field": "governs", '
        '"value": "dev.roadmap_item/tidy-cli"}], "because": "Linked in review."}'
    ),
}

_WriteRequestT = TypeVar(
    "_WriteRequestT", PlaybillSetRequestV1, PlaybillRetireRequestV1, PlaybillWriteRequestV1
)


def _write_request(
    model: type[_WriteRequestT], operation: str, fields: Mapping[str, Any]
) -> _WriteRequestT:
    """One typed write request for this surface, with its file evidence read here.

    The daemon never reads workspace files, so a ``file`` evidence is observed
    from the MCP workspace before the request leaves this adapter.
    """

    try:
        request = model.model_validate({**fields, "surface": "mcp"})
    except ValidationError as exc:
        raise DataValidationError(
            f"Invalid {operation.removeprefix('cruxible_playbill_')} request; example: "
            + _WRITE_EXAMPLES[operation],
            errors=[
                f"$.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_url=False)
            ],
        ) from exc
    if isinstance(request, PlaybillWriteRequestV1):
        if any(
            isinstance(getattr(change, "evidence", None), FileEvidence)
            for change in request.changes
        ):
            workspace = mcp_workspace_root()
            request = request.model_copy(
                update={"changes": observe_changes(request.changes, workspace=workspace)}
            )
    elif isinstance(request, PlaybillSetRequestV1) and isinstance(request.evidence, FileEvidence):
        request = request.model_copy(
            update={"evidence": observe_evidence(request.evidence, workspace=mcp_workspace_root())}
        )
    return request


def handle_playbill_set(instance_id: str, **fields: Any) -> WriteOutcome:
    """Put one value in one field of one Subject."""

    request = _write_request(PlaybillSetRequestV1, "cruxible_playbill_set", fields)
    return _dispatch_remote_or_local(
        lambda client: client.playbill_set(instance_id, request=request),
        lambda: playbill_api.playbill_set(instance_id, request=request),
        operation_name="cruxible_playbill_set",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_retire(instance_id: str, **fields: Any) -> WriteOutcome:
    """End one live Claim, by ID or by its Subject and field."""

    request = _write_request(PlaybillRetireRequestV1, "cruxible_playbill_retire", fields)
    return _dispatch_remote_or_local(
        lambda client: client.playbill_retire(instance_id, request=request),
        lambda: playbill_api.playbill_retire(instance_id, request=request),
        operation_name="cruxible_playbill_retire",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_write(instance_id: str, **fields: Any) -> WriteOutcome:
    """Apply set, add and retire changes as one change set."""

    request = _write_request(PlaybillWriteRequestV1, "cruxible_playbill_write", fields)
    return _dispatch_remote_or_local(
        lambda client: client.playbill_write(instance_id, request=request),
        lambda: playbill_api.playbill_write(instance_id, request=request),
        operation_name="cruxible_playbill_write",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_query_spec(
    instance_id: str,
    *,
    spec: QueryDefinitionSpecV1,
    evaluation_time: str | None = None,
    **fields: Any,
) -> contracts.PlaybillQueryResult:
    """Run one full QueryDefinition spec through the same ``query`` request path."""
    request = _playbill_query_request(
        "cruxible_playbill_query_spec", evaluation_time, {**fields, "spec": spec}
    )
    return _dispatch_remote_or_local(
        lambda client: client.query_playbill(instance_id, request=request),
        lambda: playbill_api.playbill_query(instance_id, request=request),
        operation_name="cruxible_playbill_query_spec",
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
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_procedure_run(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str | None,
    at: dict[str, Any] | None,
    input: Any,
    resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
    trigger_event: contracts.TriggerEventReferenceV1 | None = None,
) -> contracts.PlaybillProcedureRunState:
    evaluated_at = parse_datetime(evaluation_time)
    request = ProcedureRunRequestV2.model_validate(
        {
            "evaluation_time": evaluated_at,
            "at": at,
            "input": input,
            "resolution_contract": resolution_contract,
            "trigger_event": trigger_event,
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_playbill_procedure(
            instance_id,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            name=name,
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


def handle_playbill_procedure_measure(
    instance_id: str,
    name: str,
    request: contracts.PlaybillProcedureMeasureRequestV1,
) -> contracts.PlaybillProcedureMeasureResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.measure_playbill_procedure(instance_id, name, request=request),
        lambda: playbill_api.playbill_procedure_measure(instance_id, name, request=request),
        operation_name="cruxible_playbill_procedure_measure",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_procedure_readings(
    instance_id: str,
    name: str,
    request: contracts.PlaybillProcedureReadingsRequestV1,
) -> contracts.PlaybillProcedureReadingsResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_procedure_readings(instance_id, name, request=request),
        lambda: playbill_api.playbill_procedure_readings(instance_id, name, request=request),
        operation_name="cruxible_playbill_procedure_readings",
    )


def handle_playbill_line_check(
    instance_id: str, line: str, request: contracts.LineTriggerCheckRequestV1
) -> contracts.LineTriggerCheckResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.check_playbill_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_check(instance_id, line, request=request),
        operation_name="cruxible_playbill_line_check",
    )


def handle_playbill_line_arm(instance_id: str, line: str) -> contracts.LineArmV1:
    return _dispatch_remote_or_local(
        lambda client: client.arm_playbill_line(instance_id, line),
        lambda: playbill_api.playbill_line_arm(instance_id, line),
        operation_name="cruxible_playbill_line_arm",
    )


def handle_playbill_line_disarm(instance_id: str, line: str) -> contracts.LineArmV1:
    return _dispatch_remote_or_local(
        lambda client: client.disarm_playbill_line(instance_id, line),
        lambda: playbill_api.playbill_line_disarm(instance_id, line),
        operation_name="cruxible_playbill_line_disarm",
    )


def handle_playbill_line_status(instance_id: str, line: str) -> contracts.LineArmV1:
    return _dispatch_remote_or_local(
        lambda client: client.playbill_line_status(instance_id, line),
        lambda: playbill_api.playbill_line_status(instance_id, line),
        operation_name="cruxible_playbill_line_status",
    )


def handle_playbill_line_evaluate(
    instance_id: str, line: str, request: contracts.LineEvaluateRequestV1
) -> contracts.LineTriggerCheckResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.evaluate_playbill_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_evaluate(instance_id, line, request=request),
        operation_name="cruxible_playbill_line_evaluate",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_line_dispatch(
    instance_id: str, line: str, request: contracts.LineDispatchRequestV1
) -> contracts.LineDispatchResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.dispatch_playbill_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_dispatch(instance_id, line, request=request),
        operation_name="cruxible_playbill_line_dispatch",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_line_run(
    instance_id: str,
    line: str,
    *,
    occurrence_id: str | None,
    evaluation_time: str | None = None,
    resolution_contract: contracts.ResolutionContractReferenceV1 | None = None,
    trigger_event: contracts.TriggerEventReferenceV1 | None = None,
) -> contracts.PlaybillProcedureRunState:
    request = LineRunRequestV1.model_validate(
        {
            "line": line,
            "resolution_contract": resolution_contract,
            "trigger_event": trigger_event,
            "occurrence_id": occurrence_id,
            "evaluation_time": (
                None if evaluation_time is None else parse_datetime(evaluation_time)
            ),
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_playbill_line(
            instance_id,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            line=line,
            occurrence_id=request.occurrence_id,
            evaluation_time=(
                None if request.evaluation_time is None else request.evaluation_time.isoformat()
            ),
        ),
        lambda: playbill_api.playbill_line_run(
            instance_id,
            line,
            request=request,
        ),
        operation_name="cruxible_playbill_line_run",
    )


def handle_playbill_resolution_contracts(
    instance_id: str, request: contracts.ResolutionContractsRequestV1
) -> contracts.ResolutionContractsResultV1:
    return _dispatch_remote_or_local(
        lambda client: client.resolution_contracts(instance_id, request=request),
        lambda: playbill_api.playbill_resolution_contracts(instance_id, request=request),
        operation_name="cruxible_playbill_resolution_contracts",
    )


def handle_playbill_predict(
    instance_id: str,
    request: contracts.PlaybillPredictRequestV2,
) -> contracts.PlaybillPredictResultV2:
    return _dispatch_remote_or_local(
        lambda client: client.predict_playbill(instance_id, request=request),
        lambda: playbill_api.playbill_predict(instance_id, request=request),
        operation_name="cruxible_playbill_predict",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_settle_prediction(
    instance_id: str,
    prediction_id: str,
    request: contracts.PlaybillSettleRequestV2,
) -> contracts.PlaybillSettleResultV2:
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
        local_payload=request.model_dump(mode="json"),
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


def handle_playbill_next(
    instance_id: str,
    *,
    evaluation_time: str | None = None,
    access_profile: Mapping[str, Any] | None = None,
    expiring_within: Mapping[str, Any] | None = None,
    since_result_digest: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> contracts.PlaybillNextResult:
    """Rank outstanding repair work, observing the MCP workspace as `playbill next` does."""

    stamped = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if evaluation_time is None
        else evaluation_time
    )
    profile = CoverageAccessProfileV1.model_validate(
        access_profile
        or {"profile_id": "mcp-next", "permitted_access_classes": ["instance", "public"]}
    ).model_dump(mode="json")
    workspace = mcp_workspace_root()
    observation = observe_playbill_next_workspace(workspace)
    # Rows render as MCP tool calls, and a row whose repair is a tool this
    # session does not advertise keeps its place with the repair withheld and
    # `repair_requires` naming the tool and profile it needs.
    from cruxible_core.mcp.curation import session_tool_names

    tools = tuple(sorted(session_tool_names()))

    def remote(client: CruxibleClient) -> contracts.PlaybillNextResult:
        observed, coordinate = observe_playbill_next_workspace_with_coverage(
            client,
            instance_id,
            workspace,
            observation=observation,
            access_profile=profile,
        )
        return client.next_playbill(
            instance_id,
            evaluation_time=stamped,
            access_profile=profile,
            at=coordinate,
            expiring_within=expiring_within,
            workspace_observation=observed,
            since_result_digest=since_result_digest,
            limit=limit,
            cursor=cursor,
            caller_surface="mcp",
            caller_tools=tools,
        )

    def local() -> contracts.PlaybillNextResult:
        observed, coordinate = observe_playbill_next_workspace_with_coverage(
            _LocalCoverageClient(),
            instance_id,
            workspace,
            observation=observation,
            access_profile=profile,
        )
        request: dict[str, Any] = {
            "tag": "playbill-next-request-v2",
            "at": None if coordinate is None else coordinate.model_dump(mode="json"),
            "evaluation_time": stamped,
            "access_profile": profile,
            "workspace_observation": observed,
            "expiring_within": None if expiring_within is None else dict(expiring_within),
            "since_result_digest": since_result_digest,
            "limit": limit,
            "cursor": cursor,
            "caller_surface": "mcp",
            "caller_tools": list(tools),
        }
        return playbill_api.playbill_next(
            instance_id,
            request={key: value for key, value in request.items() if value is not None},
        )

    return _dispatch_remote_or_local(
        remote,
        local,
        operation_name="cruxible_playbill_next",
    )


def handle_playbill_curation_list(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: dict[str, Any] | None,
    workspace_observation: dict[str, Any] | None,
    limit: int = contracts.PLAYBILL_CURATION_LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
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
        "limit": limit,
        "cursor": cursor,
    }
    return _dispatch_remote_or_local(
        lambda client: client.list_playbill_curation(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=profile,
            workspace_observation=workspace_observation,
            limit=limit,
            cursor=cursor,
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
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "attribution_refs": attribution_refs,
        },
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
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "accepted_proposal_id": accepted_proposal_id,
            "accepted_changeset_digest": accepted_changeset_digest,
            "attribution_refs": attribution_refs,
        },
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
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "scope": scope,
            "until_generation": until_generation,
            "attribution_refs": attribution_refs,
        },
    )


def handle_playbill_coverage(
    instance_id: str,
    *,
    observations: list[dict[str, Any]] | None = None,
    bindings: Mapping[str, str] | None = None,
    files: tuple[str, ...] = (),
    ranges: tuple[str, ...] = (),
    grep_results_path: str | None = None,
    whole_working_set: bool = False,
    budget: dict[str, Any] | None = None,
    scan_budget: dict[str, Any] | None = None,
) -> contracts.PlaybillCoverageResult:
    """Resolve caller observations, or ones the adapter derives from workspace files."""

    if (observations is None) == (bindings is None):
        raise DataValidationError(
            "coverage takes exactly one of observations or bindings (workspace files)"
        )
    if bindings is not None:
        observed = _workspace_observations(
            bindings,
            files=files,
            ranges=ranges,
            grep_results_path=grep_results_path,
            whole_working_set=whole_working_set,
        )
    elif files or ranges or grep_results_path is not None or whole_working_set:
        raise DataValidationError(
            "files, ranges, grep_results_path, and whole_working_set apply only with bindings"
        )
    else:
        observed = tuple(
            WorkingSourceObservationV1.model_validate(item) for item in observations or ()
        )
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
        operation_name="cruxible_playbill_coverage",
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


def _workspace_observations(
    bindings: Mapping[str, str],
    *,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_results_path: str | None,
    whole_working_set: bool,
) -> tuple[WorkingSourceObservationV1, ...]:
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
    return tuple(
        observe_workspace(
            bindings_from_mapping(bindings),
            root=workspace,
            files=files,
            ranges=ranges,
            grep_text=grep_text,
            whole_working_set=whole_working_set,
        )
    )


FloorExportMode = Literal["bytes", "write", "status"]


def handle_playbill_floor_export(
    instance_id: str,
    *,
    mode: FloorExportMode,
    force: bool = False,
    include: tuple[contracts.PlaybillFloorExportPart, ...] = (),
) -> (
    contracts.PlaybillFloorExport
    | contracts.PlaybillWorkspaceFloorWriteResult
    | contracts.PlaybillWorkspaceFloorStatus
):
    """Return floor bytes, write them under the MCP workspace, or report that floor's status."""

    if force and mode != "write":
        raise DataValidationError("force applies only to floor export mode 'write'")
    if include and mode == "status":
        raise DataValidationError("include applies only to floor export modes 'bytes' and 'write'")
    if mode == "status":
        head = _dispatch_remote_or_local(
            lambda client: client.playbill_head(instance_id),
            lambda: playbill_api.playbill_head(instance_id),
            operation_name="cruxible_playbill_floor_export",
        )
        return inspect_workspace_floor(
            mcp_git_workspace_root(),
            current_coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
        )
    parts = floor_export_parts(include)
    if mode == "bytes":
        return _dispatch_remote_or_local(
            lambda client: client.export_playbill_floor(instance_id, **parts),
            lambda: playbill_api.playbill_export_floor(instance_id, **parts),
            operation_name="cruxible_playbill_floor_export",
        )
    # The CLI's write path: export, write, and record the refresh profile with
    # its opt-in parts, naming the daemon this MCP server talks to.
    workspace = mcp_git_workspace_root()
    settings = resolve_server_settings()
    transport = (
        {"server_socket": settings.server_socket}
        if settings.enabled and settings.server_socket
        else {"server_url": settings.server_url}
        if settings.enabled and settings.server_url
        else {}
    )

    def write(
        export_floor: Callable[[], contracts.PlaybillFloorExport],
    ) -> contracts.PlaybillWorkspaceFloorWriteResult:
        return write_workspace_floor(
            export_floor,
            instance_id=instance_id,
            workspace=workspace,
            include=include,
            force=force,
            **transport,
        )[1]

    return _dispatch_remote_or_local(
        lambda client: write(lambda: client.export_playbill_floor(instance_id, **parts)),
        lambda: write(lambda: playbill_api.playbill_export_floor(instance_id, **parts)),
        operation_name="cruxible_playbill_floor_export",
    )
