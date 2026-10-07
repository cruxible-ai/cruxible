"""MCP handler implementations."""

from __future__ import annotations

import base64
import hashlib
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
    observe_next_workspace,
)
from cruxible_client.authoring.attestations import (
    append_prepared_claim_attestation,
    local_attestation_signer_from_environment,
)
from cruxible_client.authoring.bind import bind_working_selection_input
from cruxible_client.authoring.blocks import repin_projection_block, sync_projection_blocks
from cruxible_client.authoring.examples import authoring_example, authoring_example_note
from cruxible_client.authoring.inputs import AuthoringInput, ClaimInput
from cruxible_client.authoring.selectors import WorkspaceSources
from cruxible_client.authoring.signing import LocalEd25519ApprovalSigner
from cruxible_client.authoring.sources import (
    compile_client_source_context,
    load_source_catalog,
    mapped_root_aliases,
)
from cruxible_client.authoring.workspace import (
    daemon_floor_delivery,
    floor_export_parts,
    observe_next_workspace_with_coverage,
    workspace_floor_freshness,
    write_workspace_floor,
    write_workspace_floor_delta,
)
from cruxible_client.authoring.write_evidence import observe_changes, observe_evidence
from cruxible_client.contracts.artifacts import parse_artifact_identity
from cruxible_client.contracts.attestations import ApprovalAttestation, ApprovalStatement
from cruxible_client.contracts.authoring.models import BlockDetachResult
from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.change_control import (
    ChangeControlRequest,
    StateCoordinate,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
    ClaimAttestationCaptureReference,
    ClaimStance,
    PreparedClaimAttestationRequest,
)
from cruxible_client.contracts.claim_type_upgrade import (
    ClaimTypeUpgradeRequest,
    ClaimTypeUpgradeResult,
)
from cruxible_client.contracts.declared_blocks import (
    PROJECTION_STAMP_ADAPTER,
    BlockRepinResult,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.evidence_rule_upgrade import (
    EvidenceRuleUpgradeRequest,
    EvidenceRuleUpgradeResult,
)
from cruxible_client.contracts.floor import FloorDelta
from cruxible_client.contracts.get_reads import (
    ByteRange,
    GetRequest,
    GetResult,
)
from cruxible_client.contracts.governance import governance_identifier
from cruxible_client.contracts.kits import (
    KitAddRequest,
    KitBuildRequest,
    KitBuildResult,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog,
    ProviderInstallRequest,
    ProviderInstallResult,
)
from cruxible_client.contracts.query.definitions import QueryDefinitionSpec
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.validation_messages import validation_lines
from cruxible_client.contracts.write import (
    FileEvidence,
    RetireRequest,
    SetRequest,
    WriteOutcome,
    WriteRequest,
)
from cruxible_client.errors import DaemonOperationScopeError as ClientDaemonOperationScopeError
from cruxible_client.errors import ServerUnreachableError
from cruxible_client.transport.http import configured_principal_id
from cruxible_core import __version__
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputRecord,
)
from cruxible_core.claims.claim_type_migrations import ClaimTypeMigrationRequestAny
from cruxible_core.coverage.adapter import WorkingSourceObservation
from cruxible_core.coverage.contracts import CoverageAccessProfile, CoverageCardBudget
from cruxible_core.coverage.indexes import CoverageScanBudget
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
    ApprovalRequest,
    AuthoringInputCompileRequest,
    AuthoringPreflightRequest,
    AuthoringRebaseRequest,
    AuthoringSubmitRequest,
    BlockDepublishRequest,
    CompilerUpgradeRequest,
    CurationAcceptFixedRequest,
    CurationOverruleRequest,
    CurationSuppressRequest,
    InitRequest,
    InsertionAbandonRequest,
    ProposalReadmitRequest,
    ProposalWithdrawRequest,
    ProposeClaimTypeInputRequest,
    ProposeDocumentRequest,
    ProposePrincipalRequest,
    SourceProposeRequest,
    StoreBodyRequest,
)
from cruxible_core.service.change_preview import state_change_scope
from cruxible_core.service.discovery.since import validate_playbill_since_request
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    ProcedureBindRequest,
    ProcedureReadinessRequestV1,
    ProcedureRunRequest,
)

_client_cache: CruxibleClient | None = None
_client_cache_key: tuple[str | None, str | None, str | None, str | None] | None = None
_client_cache_lock = threading.RLock()
ResultT = TypeVar("ResultT")
_AUTHORING_INPUT: TypeAdapter[AuthoringInput] = TypeAdapter(AuthoringInput)
_CLAIM_TYPE_MIGRATION: TypeAdapter[ClaimTypeMigrationRequestAny] = TypeAdapter(
    ClaimTypeMigrationRequestAny
)


class _LocalFloorClient:
    """Give the shared client adapter the same calls in library mode."""

    def activate_proposal(self, instance_id: str, proposal_id: str) -> contracts.ActivationReceipt:
        return playbill_api.playbill_activate(instance_id, proposal_id)

    def export_floor(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        include: Sequence[contracts.FloorExportPart] = (),
    ) -> contracts.FloorExport:
        if at is not None:  # pragma: no cover - shared refresh always asks for current
            raise DataValidationError("local floor adapter accepts only the current coordinate")
        return playbill_api.playbill_export_floor(instance_id, include=tuple(include))

    def floor_delta(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        base_generation: int | None = None,
        base_renderer: str | None = None,
    ) -> FloorDelta:
        return playbill_api.playbill_floor_delta(
            instance_id,
            at=None
            if at is None
            else AcceptedCoordinate.model_validate(
                at if isinstance(at, Mapping) else at.model_dump(mode="json")
            ),
            base_generation=base_generation,
            base_renderer=base_renderer,
        )

    def check_projection_blocks(
        self, instance_id: str, *, request: contracts.ProjectionCheckRequest
    ) -> contracts.ProjectionCheckResult:
        return playbill_api.playbill_check_projection_blocks(instance_id, request=request)

    def read_block_sync_backing(
        self,
        instance_id: str,
        *,
        request: contracts.BlockSyncReadRequest,
    ) -> contracts.BlockSyncReadResult:
        return playbill_api.playbill_read_block_sync_backing(instance_id, request=request)


class _LocalCoverageClient:
    """Serve the shared next-workspace coverage scan in library mode."""

    def resolve_coverage(
        self,
        instance_id: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | None = None,
        budget: Mapping[str, Any] | None = None,
        scan_budget: Mapping[str, Any] | None = None,
    ) -> contracts.CoverageResult:
        return playbill_api.playbill_resolve_coverage(
            instance_id,
            observations=tuple(
                WorkingSourceObservation.model_validate(item) for item in observations
            ),
            at=None if at is None else AcceptedCoordinate.model_validate(_json(at)),
            budget=None if budget is None else CoverageCardBudget.model_validate(budget),
            scan_budget=(
                None if scan_budget is None else CoverageScanBudget.model_validate(scan_budget)
            ),
        )

    def head(self, instance_id: str) -> contracts.Head:
        return playbill_api.playbill_head(instance_id)


def _json(value: contracts.AcceptedCoordinate | Mapping[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else dict(value)


class _LocalSourceContextClient:
    """Supply accepted context to the shared local compiler in library mode."""

    def source_context(self, instance_id: str) -> contracts.SourceContext:
        return playbill_api.playbill_source_context(instance_id)


class _LocalAttestationClient:
    """Expose the client-side signing adapter without giving the daemon a key."""

    def whoami(self, instance_id: str) -> contracts.WhoAmI:
        return playbill_api.playbill_whoami(instance_id)

    def orient(
        self,
        instance_id: str,
        *,
        section: contracts.OrientSection,
        limit: int,
        cursor: str | None = None,
    ) -> contracts.OrientResult:
        return playbill_api.playbill_orient(
            instance_id, section=section, limit=limit, cursor=cursor, surface="sdk"
        )

    def get(self, instance_id: str, *, request: GetRequest) -> GetResult:
        return playbill_api.playbill_get(instance_id, request=request)

    def append_claim_attestation(
        self,
        instance_id: str,
        *,
        request: ClaimAttestationAppendRequest,
    ) -> ClaimAttestationAppendResult:
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
    "cruxible_line_dispatch": TypeAdapter(contracts.LineDispatchRequest),
    "cruxible_line_evaluate": TypeAdapter(contracts.LineEvaluateRequest),
    "cruxible_line_arm": TypeAdapter(ChangeControlRequest),
    "cruxible_line_disarm": TypeAdapter(ChangeControlRequest),
    "cruxible_provider_install": TypeAdapter(ProviderInstallRequest),
    "cruxible_kit_add": TypeAdapter(KitAddRequest),
    "cruxible_kit_remove": TypeAdapter(KitRemoveRequest),
    "cruxible_evidence_rules_upgrade": TypeAdapter(EvidenceRuleUpgradeRequest),
    "cruxible_claim_type_upgrade": TypeAdapter(ClaimTypeUpgradeRequest),
    "cruxible_activate": None,  # path only
    "cruxible_authoring_abandon_insertion": TypeAdapter(InsertionAbandonRequest),
    "cruxible_authoring_bind": TypeAdapter(AuthoringInputCompileRequest),
    "cruxible_authoring_compile": TypeAdapter(AuthoringInputCompileRequest),
    "cruxible_authoring_preflight": TypeAdapter(AuthoringPreflightRequest),
    "cruxible_authoring_rebase": TypeAdapter(AuthoringRebaseRequest),
    "cruxible_authoring_submit": TypeAdapter(AuthoringSubmitRequest),
    "cruxible_block_depublish": TypeAdapter(BlockDepublishRequest),
    "cruxible_claim_attest": None,  # shared preparation helper builds the body
    "cruxible_set": TypeAdapter(SetRequest),
    "cruxible_retire": TypeAdapter(RetireRequest),
    "cruxible_write": TypeAdapter(WriteRequest),
    "cruxible_claim_type_migrate": TypeAdapter(ClaimTypeMigrationRequestAny),
    "cruxible_curation_accept_fixed": TypeAdapter(CurationAcceptFixedRequest),
    "cruxible_curation_overrule": TypeAdapter(CurationOverruleRequest),
    "cruxible_curation_suppress": TypeAdapter(CurationSuppressRequest),
    "cruxible_read_capture": TypeAdapter(CaptureReadRequest),
    "cruxible_init": TypeAdapter(InitRequest),
    "cruxible_predict": TypeAdapter(contracts.PredictRequest),
    "cruxible_procedure_bind": TypeAdapter(ProcedureBindRequest),
    "cruxible_proposal_readmit": TypeAdapter(ProposalReadmitRequest),
    "cruxible_proposal_withdraw": TypeAdapter(ProposalWithdrawRequest),
    "cruxible_propose_claim_type": TypeAdapter(ProposeClaimTypeInputRequest),
    "cruxible_propose_document": TypeAdapter(ProposeDocumentRequest),
    "cruxible_compiler_upgrade": TypeAdapter(CompilerUpgradeRequest),
    "cruxible_propose_principal_change": TypeAdapter(ProposePrincipalRequest),
    "cruxible_propose_source_bundle": TypeAdapter(SourceProposeRequest),
    "cruxible_procedure_measure": TypeAdapter(contracts.ProcedureMeasureRequest),
    "cruxible_settle": TypeAdapter(contracts.SettleRequest),
    "cruxible_store_body": TypeAdapter(StoreBodyRequest),
    "cruxible_submit_approval": TypeAdapter(ApprovalRequest),
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
        raise DataValidationError(
            f"{operation_name}: invalid request", errors=validation_lines(exc)
        ) from exc


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
                lambda client: client.show_host(scope),
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
) -> contracts.InitResult:
    records = tuple(PrincipalRecord.model_validate(item) for item in principals)
    return _dispatch_remote_or_local(
        lambda client: client.init(
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
        operation_name="cruxible_init",
        local_payload={
            "principals": [item.model_dump(mode="json") for item in records],
            "operating_profile": operating_profile,
            "require_independent_approval": require_independent_approval,
            "git_object_format": git_object_format,
        },
    )


def handle_playbill_store_body(instance_id: str, content_base64: str) -> contracts.CasObjectResult:
    try:
        content = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise DataValidationError("Cruxible body is not canonical base64") from exc
    return _dispatch_remote_or_local(
        lambda client: client.store_body(instance_id, content),
        lambda: playbill_api.playbill_store_body(instance_id, content_base64=content_base64),
        operation_name="cruxible_store_body",
        local_payload={"content_base64": content_base64},
    )


def handle_playbill_provider_catalog(instance_id: str) -> ProviderCatalog:
    return _dispatch_remote_or_local(
        lambda client: client.list_provider_packages(instance_id),
        lambda: playbill_api.playbill_provider_catalog(instance_id),
        operation_name="cruxible_provider_catalog",
    )


def handle_playbill_provider_install(
    instance_id: str,
    request: ProviderInstallRequest,
) -> ProviderInstallResult:
    return _dispatch_remote_or_local(
        lambda client: client.install_provider(instance_id, request),
        lambda: playbill_api.playbill_provider_install(instance_id, request),
        operation_name="cruxible_provider_install",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_kit_build(instance_id: str, request: KitBuildRequest) -> KitBuildResult:
    return _dispatch_remote_or_local(
        lambda client: client.build_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_build(instance_id, request),
        operation_name="cruxible_kit_build",
    )


def handle_playbill_kit_status(instance_id: str) -> KitStatus:
    return _dispatch_remote_or_local(
        lambda client: client.kit_status(instance_id),
        lambda: playbill_api.playbill_kit_status(instance_id),
        operation_name="cruxible_kit_status",
    )


def handle_playbill_kit_add(instance_id: str, request: KitAddRequest) -> KitChangeResult:
    return _dispatch_remote_or_local(
        lambda client: client.add_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_add(instance_id, request),
        operation_name="cruxible_kit_add",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_claim_type_upgrade(
    instance_id: str, request: ClaimTypeUpgradeRequest
) -> ClaimTypeUpgradeResult:
    return _dispatch_remote_or_local(
        lambda client: client.upgrade_claim_types(instance_id, request),
        lambda: playbill_api.playbill_claim_type_upgrade(instance_id, request),
        operation_name="cruxible_claim_type_upgrade",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_evidence_rules_upgrade(
    instance_id: str, request: EvidenceRuleUpgradeRequest
) -> EvidenceRuleUpgradeResult:
    return _dispatch_remote_or_local(
        lambda client: client.upgrade_evidence_rules(instance_id, request),
        lambda: playbill_api.playbill_evidence_rules_upgrade(instance_id, request),
        operation_name="cruxible_evidence_rules_upgrade",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_kit_remove(instance_id: str, request: KitRemoveRequest) -> KitChangeResult:
    return _dispatch_remote_or_local(
        lambda client: client.remove_kit(instance_id, request),
        lambda: playbill_api.playbill_kit_remove(instance_id, request),
        operation_name="cruxible_kit_remove",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_propose_document(
    instance_id: str,
    shell: dict[str, Any],
    proposal_name: str,
    source_compilation_digest: str | None,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    document = DocumentShell.model_validate(shell)
    return _dispatch_remote_or_local(
        lambda client: client.propose_document(
            instance_id,
            shell=document.model_dump(mode="json"),
            proposal_name=proposal_name,
            source_compilation_digest=source_compilation_digest,
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_propose_document(
            instance_id,
            shell=document,
            proposal_name=proposal_name,
            source_compilation_digest=source_compilation_digest,
            dry_run=dry_run,
            at=at,
        ),
        operation_name="cruxible_propose_document",
        local_payload={
            "shell": document.model_dump(mode="json"),
            "proposal_name": proposal_name,
            "dry_run": dry_run,
            "at": at,
            "source_compilation_digest": source_compilation_digest,
        },
    )


def handle_playbill_inspect_proposal(
    instance_id: str, proposal_id: str
) -> contracts.ProposalInspection:
    return _dispatch_remote_or_local(
        lambda client: client.inspect_proposal(instance_id, proposal_id),
        lambda: playbill_api.playbill_inspect_proposal(instance_id, proposal_id),
        operation_name="cruxible_inspect_proposal",
    )


def handle_playbill_inspect_refusal(
    instance_id: str, proposal_id: str
) -> contracts.RefusalInspection:
    return _dispatch_remote_or_local(
        lambda client: client.inspect_refusal(instance_id, proposal_id),
        lambda: playbill_api.playbill_inspect_refusal(instance_id, proposal_id),
        operation_name="cruxible_inspect_refusal",
    )


def handle_playbill_review(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool,
) -> contracts.ProposalReview:
    return _dispatch_remote_or_local(
        lambda client: client.review_proposal(instance_id, proposal_id, include_body=include_body),
        lambda: playbill_api.playbill_review_proposal(
            instance_id, proposal_id, include_body=include_body
        ),
        operation_name="cruxible_review",
    )


def handle_playbill_prepare_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool,
) -> contracts.ApprovalChallenge:
    return _dispatch_remote_or_local(
        lambda client: client.prepare_approval(
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
        operation_name="cruxible_prepare_approval",
    )


def handle_playbill_submit_approval(
    instance_id: str,
    proposal_id: str,
    attestation: dict[str, Any],
) -> contracts.ApprovalReceipt:
    public_attestation = ApprovalAttestation.model_validate(attestation)
    return _dispatch_remote_or_local(
        lambda client: client.submit_approval(
            instance_id,
            proposal_id,
            attestation=public_attestation.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_submit_approval(
            instance_id,
            proposal_id,
            attestation=public_attestation,
        ),
        operation_name="cruxible_submit_approval",
        local_payload={"attestation": public_attestation.model_dump(mode="json")},
    )


def handle_playbill_approve(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str | None,
    candidate_digest: str | None,
) -> contracts.ApprovalReceipt:
    """Challenge, sign with the configured local key, and submit, as `proposal approve` does.

    Only the public attestation leaves this process; the key's bytes and path
    never enter a result or a log line.
    """

    key_dir = mcp_approval_key_dir()
    signer = signer_id if signer_id is not None else _sole_approval_signer(key_dir)
    try:
        governance_identifier(signer, label="signer_id")
    except ValueError as exc:
        raise DataValidationError(f"cruxible_approve: {exc}") from exc
    challenge = handle_playbill_prepare_approval(
        instance_id, proposal_id, signer_id=signer, include_body=False
    )
    statement = ApprovalStatement.model_validate(challenge.statement)
    if candidate_digest is not None and statement.payload_digest != candidate_digest:
        raise DataValidationError(
            f"cruxible_approve: proposal {proposal_id} now signs candidate "
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
            "cruxible_approve: pass signer_id; the configured key directory holds "
            f"{len(signers)} approval keys (signers: {found})"
        )
    return signers[0]


def handle_playbill_activate(
    instance_id: str, proposal_id: str
) -> contracts.WorkspaceActivationResult:
    workspace = optional_mcp_git_workspace_root()
    if workspace is None:
        # Activation is a daemon act; the floor refresh and block sync are local
        # conveniences that need a worktree, so their absence must not refuse it.
        activation = _dispatch_remote_or_local(
            lambda client: client.activate_proposal(instance_id, proposal_id),
            lambda: playbill_api.playbill_activate(instance_id, proposal_id),
            operation_name="cruxible_activate",
        )
        return contracts.WorkspaceActivationResult(
            **activation.model_dump(mode="json"),
            floor_refresh=contracts.FloorRefreshResult(
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
        operation_name="cruxible_activate",
    )


def _playbill_whoami(instance_id: str) -> contracts.WhoAmI:
    return _dispatch_remote_or_local(
        lambda client: client.whoami(instance_id),
        lambda: playbill_api.playbill_whoami(instance_id),
        operation_name="cruxible_whoami",
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
    section: contracts.OrientSection | None = None,
    limit: int = contracts.ORIENT_DEFAULT_LIMIT,
    cursor: str | None = None,
    at: str | contracts.AcceptedCoordinate | None = None,
    evaluation_time: str | None = None,
) -> contracts.OrientResult:
    """The orient map, with its follow-up calls rendered as MCP tool calls."""

    from cruxible_core.mcp.curation import session_tool_names

    tools = tuple(sorted(session_tool_names()))
    result = _dispatch_remote_or_local(
        lambda client: client.orient(
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
        operation_name="cruxible_orient",
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
    limit: int = contracts.PROPOSAL_LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> contracts.ProposalList:
    normalized = cast(Any, status)
    return _dispatch_remote_or_local(
        lambda client: client.list_proposals(
            instance_id, status=normalized, limit=limit, cursor=cursor
        ),
        lambda: playbill_api.playbill_list_proposals(
            instance_id, status=normalized, limit=limit, cursor=cursor
        ),
        operation_name="cruxible_proposal_list",
    )


def handle_playbill_readmit_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalReadmitResult:
    return _dispatch_remote_or_local(
        lambda client: client.readmit_proposal(instance_id, proposal_id, dry_run=dry_run, at=at),
        lambda: playbill_api.playbill_readmit_proposal(
            instance_id, proposal_id, dry_run=dry_run, at=at
        ),
        operation_name="cruxible_proposal_readmit",
        local_payload={"dry_run": dry_run, "at": at},
    )


def handle_playbill_withdraw_proposal(
    instance_id: str,
    proposal_id: str,
    reason: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalWithdrawResult:
    return _dispatch_remote_or_local(
        lambda client: client.withdraw_proposal(
            instance_id, proposal_id, reason=reason, dry_run=dry_run, at=at
        ),
        lambda: playbill_api.playbill_withdraw_proposal(
            instance_id, proposal_id, reason, dry_run=dry_run, at=at
        ),
        operation_name="cruxible_proposal_withdraw",
        local_payload={"reason": reason, "dry_run": dry_run, "at": at},
    )


def handle_playbill_read_capture(instance_id: str, request: CaptureReadRequest) -> CaptureRead:
    return _dispatch_remote_or_local(
        lambda client: client.read_capture(instance_id, request),
        lambda: playbill_api.playbill_read_capture(instance_id, request),
        operation_name="cruxible_read_capture",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_source_context(instance_id: str) -> contracts.SourceContext:
    return _dispatch_remote_or_local(
        lambda client: client.source_context(instance_id),
        lambda: playbill_api.playbill_source_context(instance_id),
        operation_name="cruxible_source_context",
    )


def handle_playbill_source_check(
    instance_id: str,
    *,
    bundle: dict[str, Any] | None = None,
    catalog_path: str | None = None,
    repository_root: str = ".",
    local_catalog_path: str | None = None,
    root_aliases: Mapping[str, str] | None = None,
) -> contracts.SourceCheckResult:
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
        lambda client: client.check_source_bundle(
            instance_id, bundle=frozen.model_dump(mode="json")
        ),
        lambda: playbill_api.playbill_check_source_bundle(instance_id, bundle=frozen),
        operation_name="cruxible_source_check",
    )


def handle_playbill_propose_source_bundle(
    instance_id: str,
    bundle: dict[str, Any],
    *,
    source_name: str,
    proposal_name: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    frozen = SourceCompilationBundle.model_validate(bundle)
    return _dispatch_remote_or_local(
        lambda client: client.propose_source_bundle(
            instance_id,
            bundle=frozen.model_dump(mode="json"),
            source_name=source_name,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_propose_source_bundle(
            instance_id,
            bundle=frozen,
            source_name=source_name,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        operation_name="cruxible_propose_source_bundle",
        local_payload={
            "bundle": frozen.model_dump(mode="json"),
            "source_name": source_name,
            "proposal_name": proposal_name,
            "dry_run": dry_run,
            "at": at,
        },
    )


def handle_playbill_compiler_upgrade(
    instance_id: str,
    target_compiler_digest: str,
    base: dict[str, Any],
    proposal_name: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    request = CompilerUpgradeRequest.model_validate(
        {
            "target": {"rule_digest": target_compiler_digest},
            "base": base,
            "proposal_name": proposal_name,
            "dry_run": dry_run,
            "at": at,
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.propose_compiler_upgrade(
            instance_id,
            target=request.target,
            base=request.base,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_propose_compiler_upgrade(
            instance_id,
            target=request.target,
            base=request.base,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        operation_name="cruxible_compiler_upgrade",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_propose_principal_change(
    instance_id: str,
    principal: dict[str, Any],
    proposal_name: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ProposalInspection:
    record = PrincipalRecord.model_validate(principal)
    return _dispatch_remote_or_local(
        lambda client: client.propose_principal_change(
            instance_id,
            principal=record.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_propose_principal_change(
            instance_id,
            principal=record,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        operation_name="cruxible_propose_principal_change",
        local_payload={
            "principal": record.model_dump(mode="json"),
            "proposal_name": proposal_name,
            "dry_run": dry_run,
            "at": at,
        },
    )


def handle_playbill_propose_claim_type(
    instance_id: str,
    input: dict[str, Any],
    proposal_name: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.ClaimTypeInputProposalResult:
    request = ClaimTypeInputRecord.model_validate(input)
    return _dispatch_remote_or_local(
        lambda client: client.propose_claim_type_input(
            instance_id,
            input=request.model_dump(mode="json"),
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_propose_claim_type_input(
            instance_id,
            input=request,
            proposal_name=proposal_name,
            dry_run=dry_run,
            at=at,
        ),
        operation_name="cruxible_propose_claim_type",
        local_payload={
            "input": request.model_dump(mode="json"),
            "proposal_name": proposal_name,
            "dry_run": dry_run,
            "at": at,
        },
    )


def handle_playbill_migrate_claim_type(
    instance_id: str,
    request: dict[str, Any],
) -> contracts.ClaimTypeMigrationResponse:
    migration = _CLAIM_TYPE_MIGRATION.validate_python(request)
    return _dispatch_remote_or_local(
        lambda client: client.migrate_claim_type(
            instance_id,
            request=migration.model_dump(mode="json"),
        ),
        lambda: playbill_api.playbill_migrate_claim_type(instance_id, request=migration),
        operation_name="cruxible_claim_type_migrate",
        local_payload=migration.model_dump(mode="json"),
    )


def _handle_claim_attestation(
    client: Any,
    instance_id: str,
    prepared: PreparedClaimAttestationRequest,
) -> ClaimAttestationAppendResult:
    signer = local_attestation_signer_from_environment(
        client,
        instance_id,
        # A forbidden root for key custody; nothing under it is read.
        workspace_root=mcp_workspace_root(guard=False),
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
) -> ClaimAttestationAppendResult:
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
        prepared = PreparedClaimAttestationRequest(
            claim_id=claim_id.removeprefix("Claim:"),
            attestation_basis="new_capture" if capture_digests else "examined_existing",
            stance=stance,
            capture_references=tuple(
                ClaimAttestationCaptureReference(capture_digest=digest)
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
        raise DataValidationError(
            "cruxible_claim_attest: invalid request", errors=validation_lines(exc)
        ) from exc
    return _dispatch_remote_or_local(
        lambda client: _handle_claim_attestation(client, instance_id, prepared),
        lambda: _handle_claim_attestation(_LocalAttestationClient(), instance_id, prepared),
        operation_name="cruxible_claim_attest",
    )


def handle_playbill_authoring_example(
    name: contracts.AuthoringExampleName,
    *,
    claim_id: str | None = None,
    capture_digest: str | None = None,
) -> contracts.AuthoringExampleResult:
    payload = authoring_example(
        name,
        claim_id=claim_id,
        capture_digest=capture_digest,
    )
    return contracts.AuthoringExampleResult(
        name=name, payload=payload, note=authoring_example_note(name)
    )


def handle_playbill_authoring_get(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    return _dispatch_remote_or_local(
        lambda client: client.get_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_get(instance_id, intent_id),
        operation_name="cruxible_authoring_get",
    )


def handle_playbill_authoring_resume(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    return _dispatch_remote_or_local(
        lambda client: client.resume_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_resume(instance_id, intent_id),
        operation_name="cruxible_authoring_resume",
    )


def handle_playbill_authoring_list_pending(
    instance_id: str,
) -> contracts.AuthoringIntentListRecord:
    return _dispatch_remote_or_local(
        lambda client: client.list_pending_authoring_intents(instance_id),
        lambda: playbill_api.playbill_authoring_list_pending(instance_id),
        operation_name="cruxible_authoring_list_pending",
    )


def handle_playbill_authoring_compile(
    instance_id: str,
    payload: dict[str, Any],
    *,
    intent_id: str | None,
) -> contracts.AuthoringPreflightResult:
    request = _AUTHORING_INPUT.validate_python(payload)
    return _dispatch_remote_or_local(
        lambda client: client.compile_authoring_input(
            instance_id,
            input=request.model_dump(mode="json"),
            intent_id=intent_id,
        ),
        lambda: playbill_api.playbill_authoring_compile_input(
            instance_id,
            input=request,
            intent_id=intent_id,
        ),
        operation_name="cruxible_authoring_compile",
        local_payload={"input": request.model_dump(mode="json"), "intent_id": intent_id},
    )


def handle_playbill_authoring_bind(
    instance_id: str,
    *,
    source_path: str,
    anchor: str,
    payload: ClaimInput,
    window_lines: int | None,
) -> contracts.AuthoringPreflightResult:
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
        lambda client: client.compile_authoring(
            instance_id,
            payload=bound.model_dump(mode="json"),
            intent_id=None,
        ),
        lambda: playbill_api.playbill_authoring_compile(
            instance_id,
            payload=bound,
            intent_id=None,
        ),
        operation_name="cruxible_authoring_bind",
        local_payload={"input": bound.model_dump(mode="json"), "intent_id": None},
    )


def handle_playbill_authoring_preflight(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringPreflightResult:
    return _dispatch_remote_or_local(
        lambda client: client.preflight_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_preflight(instance_id, intent_id),
        operation_name="cruxible_authoring_preflight",
        local_payload={},
    )


def handle_playbill_authoring_rebase(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringIntentViewRecord:
    return _dispatch_remote_or_local(
        lambda client: client.rebase_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_rebase(instance_id, intent_id),
        operation_name="cruxible_authoring_rebase",
        local_payload={},
    )


def handle_playbill_authoring_submit(
    instance_id: str,
    intent_id: str,
) -> contracts.AuthoringSubmitResultRecord:
    return _dispatch_remote_or_local(
        lambda client: client.submit_authoring_intent(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_submit(instance_id, intent_id),
        operation_name="cruxible_authoring_submit",
        local_payload={},
    )


def handle_playbill_authoring_status(
    instance_id: str,
    intent_id: str,
) -> contracts.CandidateStatusRecord:
    return _dispatch_remote_or_local(
        lambda client: client.authoring_intent_status(instance_id, intent_id),
        lambda: playbill_api.playbill_authoring_status(instance_id, intent_id),
        operation_name="cruxible_authoring_status",
    )


def handle_playbill_authoring_abandon_insertion(
    instance_id: str,
    intent_id: str,
    expectation_id: str | None = None,
) -> contracts.InsertionAbandonResultRecord:
    return _dispatch_remote_or_local(
        lambda client: client.abandon_authoring_insertion(
            instance_id,
            intent_id,
            expectation_id=expectation_id,
        ),
        lambda: playbill_api.playbill_authoring_abandon_insertion(
            instance_id,
            intent_id,
            expectation_id=expectation_id,
        ),
        operation_name="cruxible_authoring_abandon_insertion",
        local_payload={"expectation_id": expectation_id},
    )


class _LocalBlockClient(_LocalFloorClient):
    """The reads and the declaration block repin makes, served in library mode."""

    def head(
        self,
        instance_id: str,
        *,
        at: contracts.AcceptedCoordinate | Mapping[str, Any] | str | None = None,
    ) -> contracts.Head:
        return playbill_api.playbill_head(
            instance_id,
            at=at
            if at is None or isinstance(at, str)
            else AcceptedCoordinate.model_validate(_json(at)),
        )

    def get(self, instance_id: str, *, request: GetRequest) -> GetResult:
        return playbill_api.playbill_get(instance_id, request=request)

    def query(
        self, instance_id: str, *, request: contracts.QueryRequest
    ) -> contracts.QueryResultRecord:
        return playbill_api.playbill_query(instance_id, request=request)

    def declare_block(
        self, instance_id: str, stamp: Mapping[str, Any]
    ) -> contracts.BlockDeclareResult:
        return playbill_api.playbill_block_declare(
            instance_id, PROJECTION_STAMP_ADAPTER.validate_python(dict(stamp))
        )


def _block_client() -> CruxibleClient:
    """The daemon client, or the in-process one: the SDK adapter runs on either."""

    return _get_client() or cast(CruxibleClient, _LocalBlockClient())


def handle_playbill_block_repin(
    instance_id: str,
    *,
    block: str,
    file: str | None = None,
    source: str | None = None,
    claims: Sequence[str] | None = None,
    queries: Sequence[tuple[str, Mapping[str, object]]] | None = None,
    artifacts: Sequence[str] | None = None,
    currency_policy: Literal["warn", "require_current"] | None = None,
    backing_digest: str | None = None,
    dry_run: bool | None = None,
) -> BlockRepinResult:
    """Repin one projection block adapter-side: this process computes the stamp (Q17).

    The block is named by its page (``file``, workspace-relative) or its
    catalog source (``source``). The SDK reads the page, reads the backings from
    the instance, rewrites the opening marker and declares the block; a dry run
    stops before the first write.
    """

    if (file is None) == (source is None):
        raise DataValidationError("name the block's page with exactly one of file or source")
    root = mcp_workspace_root()
    sources = WorkspaceSources(root)
    source_id = (
        source
        if source is not None
        else sources.select(
            resolve_workspace_path(cast(str, file), root=root, kind="file")
        ).source_id
    )
    path = sources.path_for_source(source_id)
    stamp = repin_projection_block(
        _block_client(),
        instance_id,
        workspace=root,
        source_id=source_id,
        block_id=block,
        claims=claims,
        queries=queries,
        artifacts=None
        if artifacts is None
        else tuple(parse_artifact_identity(item) for item in artifacts),
        currency_policy=currency_policy,
        backing_digest=backing_digest,
        evaluation_time=datetime.now(UTC),
        dry_run=bool(dry_run),
    )
    return BlockRepinResult(
        status="would_repin" if dry_run else "repinned",
        source_id=source_id,
        block_id=block,
        path=path.relative_to(root).as_posix(),
        declared_generation=stamp.declared_generation,
        stamp=stamp,
    )


def handle_playbill_block_sync(
    instance_id: str,
    *,
    files: Sequence[str] = (),
    all_sources: bool = False,
) -> contracts.BlockSyncResult:
    """Check every block's backings adapter-side; reads only, edits no page.

    Detaching retired blocks edits pages, so it is its own write-tier tool
    (`handle_playbill_block_detach`), never a flag on this read.
    """

    root = mcp_workspace_root()
    return sync_projection_blocks(
        _block_client(),
        instance_id,
        workspace=root,
        paths=tuple(resolve_workspace_path(item, root=root, kind="file") for item in files),
        all_sources=all_sources,
    )


def _pages_state(root: Path, preimages: Mapping[Path, bytes]) -> StateCoordinate:
    """The state coordinate of the pages a detach edits: each one's exact bytes."""

    return StateCoordinate.of(
        "workspace_pages",
        {
            _workspace_relative(root, path): hashlib.sha256(content).hexdigest()
            for path, content in sorted(preimages.items())
        },
    )


def _workspace_relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def handle_playbill_block_detach(
    instance_id: str,
    *,
    files: Sequence[str],
    dry_run: bool | None = None,
    at: str | None = None,
) -> BlockDetachResult:
    """Remove retired blocks' markers from pages, keeping their bodies (R12 previewed).

    A preview reports what the edit would change and edits nothing. The
    outcome is pinned to the exact page bytes the adapter read -- the same
    bytes each replacement compare-and-swaps against -- so a commit carrying
    ``at`` refuses if any page changed since its preview, and a page edited
    after that check is left as it is.
    """

    if not files:
        raise DataValidationError("name at least one page to detach retired blocks from")
    root = mcp_workspace_root()
    pages = tuple(resolve_workspace_path(item, root=root, kind="file") for item in files)
    with state_change_scope(
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.block.detach",
        describe="detaching retired projection blocks",
    ) as change:
        synced = sync_projection_blocks(
            _block_client(),
            instance_id,
            workspace=root,
            check=change.previewing,
            detach_paths=pages,
            observe_preimages=lambda preimages: change.observe(_pages_state(root, preimages)),
        )
        if change.coordinate is None:
            # A refusal before any page was read (an unattached workspace)
            # read no bytes, and pins (and is checked against) the empty set.
            change.observe(_pages_state(root, {}))
    assert change.coordinate is not None
    return BlockDetachResult(
        status="would_detach" if change.previewing else "detached",
        sync=synced,
        coordinate=change.coordinate,
    )


def handle_playbill_block_depublish(
    instance_id: str,
    source_id: str,
    block_id: str,
    *,
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.BlockDepublishResult:
    return _dispatch_remote_or_local(
        lambda client: client.depublish_block(
            instance_id, source_id, block_id, dry_run=dry_run, at=at
        ),
        lambda: playbill_api.playbill_block_depublish(
            instance_id, source_id, block_id, dry_run=dry_run, at=at
        ),
        operation_name="cruxible_block_depublish",
        local_payload={
            "source_id": source_id,
            "block_id": block_id,
            "dry_run": dry_run,
            "at": at,
        },
    )


def handle_playbill_get(
    instance_id: str,
    *,
    ref: str,
    detail: str = "summary",
    range: ByteRange | None = None,
    at: contracts.AcceptedCoordinate | str | None = None,
    evaluation_time: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> GetResult:
    try:
        request = GetRequest.model_validate(
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
        lambda client: client.get(instance_id, request=request),
        lambda: playbill_api.playbill_get(instance_id, request=request),
        operation_name="cruxible_get",
    )


def _playbill_query_request(
    tool: str, evaluation_time: str | None, fields: Mapping[str, Any]
) -> contracts.QueryRequest:
    try:
        return contracts.QueryRequest.model_validate(
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
) -> contracts.QueryResultRecord:
    """Build one typed compact or named ``query`` request and answer it."""
    request = _playbill_query_request("cruxible_query", evaluation_time, fields)
    return _dispatch_remote_or_local(
        lambda client: client.query(instance_id, request=request),
        lambda: playbill_api.playbill_query(instance_id, request=request),
        operation_name="cruxible_query",
    )


_WRITE_EXAMPLES = {
    "cruxible_set": (
        '{"subject": "dev.roadmap_item/tidy-cli", "field": "adoption_state", '
        '"value": "adopted", "because": "Agreed in review."}'
    ),
    "cruxible_retire": (
        '{"target": "CLM-0123456789abcdef0123456789abcdef", "because": "Stated in error."}'
    ),
    "cruxible_write": (
        '{"changes": [{"op": "add", "subject": "dev.card/c1", "field": "governs", '
        '"value": "dev.roadmap_item/tidy-cli"}], "because": "Linked in review."}'
    ),
}

_WriteRequestT = TypeVar("_WriteRequestT", SetRequest, RetireRequest, WriteRequest)


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
            f"Invalid {operation.removeprefix('cruxible_')} request; example: "
            + _WRITE_EXAMPLES[operation],
            errors=[
                f"$.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_url=False)
            ],
        ) from exc
    if isinstance(request, WriteRequest):
        if any(
            isinstance(getattr(change, "evidence", None), FileEvidence)
            for change in request.changes
        ):
            workspace = mcp_workspace_root()
            request = request.model_copy(
                update={"changes": observe_changes(request.changes, workspace=workspace)}
            )
    elif isinstance(request, SetRequest) and isinstance(request.evidence, FileEvidence):
        request = request.model_copy(
            update={"evidence": observe_evidence(request.evidence, workspace=mcp_workspace_root())}
        )
    return request


def handle_playbill_set(instance_id: str, **fields: Any) -> WriteOutcome:
    """Put one value in one field of one Subject."""

    request = _write_request(SetRequest, "cruxible_set", fields)
    return _dispatch_remote_or_local(
        lambda client: client.set(instance_id, request=request),
        lambda: playbill_api.playbill_set(instance_id, request=request),
        operation_name="cruxible_set",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_retire(instance_id: str, **fields: Any) -> WriteOutcome:
    """End one live Claim, by ID or by its Subject and field."""

    request = _write_request(RetireRequest, "cruxible_retire", fields)
    return _dispatch_remote_or_local(
        lambda client: client.retire(instance_id, request=request),
        lambda: playbill_api.playbill_retire(instance_id, request=request),
        operation_name="cruxible_retire",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_write(instance_id: str, **fields: Any) -> WriteOutcome:
    """Apply set, add and retire changes as one change set."""

    request = _write_request(WriteRequest, "cruxible_write", fields)
    return _dispatch_remote_or_local(
        lambda client: client.write(instance_id, request=request),
        lambda: playbill_api.playbill_write(instance_id, request=request),
        operation_name="cruxible_write",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_query_spec(
    instance_id: str,
    *,
    spec: QueryDefinitionSpec,
    evaluation_time: str | None = None,
    **fields: Any,
) -> contracts.QueryResultRecord:
    """Run one full QueryDefinition spec through the same ``query`` request path."""
    request = _playbill_query_request(
        "cruxible_query_spec", evaluation_time, {**fields, "spec": spec}
    )
    return _dispatch_remote_or_local(
        lambda client: client.query(instance_id, request=request),
        lambda: playbill_api.playbill_query(instance_id, request=request),
        operation_name="cruxible_query_spec",
    )


def handle_playbill_procedure_readiness(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str,
) -> contracts.ProcedureReadiness:
    evaluated_at = parse_datetime(evaluation_time)
    if evaluated_at is None:  # pragma: no cover - required public argument
        raise DataValidationError("Procedure readiness requires evaluation_time")
    return _dispatch_remote_or_local(
        lambda client: client.procedure_readiness(
            instance_id,
            name,
            evaluation_time=evaluated_at.isoformat(),
        ),
        lambda: playbill_api.playbill_procedure_readiness(
            instance_id,
            name,
            request=ProcedureReadinessRequestV1(evaluation_time=evaluated_at),
        ),
        operation_name="cruxible_procedure_readiness",
    )


def handle_playbill_procedure_bind(
    instance_id: str,
    name: str,
    *,
    bindings: list[dict[str, Any]],
) -> contracts.ProcedureBindResult:
    request = ProcedureBindRequest.model_validate({"bindings": bindings})
    return _dispatch_remote_or_local(
        lambda client: client.bind_procedure(
            instance_id,
            name,
            bindings=[item.model_dump(mode="json") for item in request.bindings],
        ),
        lambda: playbill_api.playbill_procedure_bind(instance_id, name, request=request),
        operation_name="cruxible_procedure_bind",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_procedure_run(
    instance_id: str,
    name: str,
    *,
    evaluation_time: str | None,
    at: dict[str, Any] | None,
    input: Any,
    resolution_contract: contracts.ResolutionContractReference | None = None,
    trigger_event: contracts.TriggerEventReference | None = None,
) -> contracts.ProcedureRunState:
    evaluated_at = parse_datetime(evaluation_time)
    request = ProcedureRunRequest.model_validate(
        {
            "evaluation_time": evaluated_at,
            "at": at,
            "input": input,
            "resolution_contract": resolution_contract,
            "trigger_event": trigger_event,
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_procedure(
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
        operation_name="cruxible_procedure_run",
    )


def handle_playbill_procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.ProcedureRunState:
    return _dispatch_remote_or_local(
        lambda client: client.get_procedure_run(instance_id, run_id),
        lambda: playbill_api.playbill_procedure_run_status(instance_id, run_id),
        operation_name="cruxible_procedure_run_status",
    )


def handle_playbill_procedure_measure(
    instance_id: str,
    name: str,
    request: contracts.ProcedureMeasureRequest,
) -> contracts.ProcedureMeasureResult:
    return _dispatch_remote_or_local(
        lambda client: client.measure_procedure(instance_id, name, request=request),
        lambda: playbill_api.playbill_procedure_measure(instance_id, name, request=request),
        operation_name="cruxible_procedure_measure",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_procedure_readings(
    instance_id: str,
    name: str,
    request: contracts.ProcedureReadingsRequest,
) -> contracts.ProcedureReadingsResult:
    return _dispatch_remote_or_local(
        lambda client: client.list_procedure_readings(instance_id, name, request=request),
        lambda: playbill_api.playbill_procedure_readings(instance_id, name, request=request),
        operation_name="cruxible_procedure_readings",
    )


def handle_playbill_line_check(
    instance_id: str, line: str, request: contracts.LineTriggerCheckRequest
) -> contracts.LineTriggerCheckResult:
    return _dispatch_remote_or_local(
        lambda client: client.check_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_check(instance_id, line, request=request),
        operation_name="cruxible_line_check",
    )


def handle_playbill_line_arm(
    instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
) -> contracts.LineArm:
    return _dispatch_remote_or_local(
        lambda client: client.arm_line(instance_id, line, dry_run=dry_run, at=at),
        lambda: playbill_api.playbill_line_arm(instance_id, line, dry_run=dry_run, at=at),
        operation_name="cruxible_line_arm",
        local_payload={"dry_run": dry_run, "at": at},
    )


def handle_playbill_line_disarm(
    instance_id: str, line: str, *, dry_run: bool | None = None, at: str | None = None
) -> contracts.LineArm:
    return _dispatch_remote_or_local(
        lambda client: client.disarm_line(instance_id, line, dry_run=dry_run, at=at),
        lambda: playbill_api.playbill_line_disarm(instance_id, line, dry_run=dry_run, at=at),
        operation_name="cruxible_line_disarm",
        local_payload={"dry_run": dry_run, "at": at},
    )


def handle_playbill_line_status(instance_id: str, line: str) -> contracts.LineArm:
    return _dispatch_remote_or_local(
        lambda client: client.line_status(instance_id, line),
        lambda: playbill_api.playbill_line_status(instance_id, line),
        operation_name="cruxible_line_status",
    )


def handle_playbill_line_evaluate(
    instance_id: str, line: str, request: contracts.LineEvaluateRequest
) -> contracts.LineTriggerCheckResult:
    return _dispatch_remote_or_local(
        lambda client: client.evaluate_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_evaluate(instance_id, line, request=request),
        operation_name="cruxible_line_evaluate",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_line_dispatch(
    instance_id: str, line: str, request: contracts.LineDispatchRequest
) -> contracts.LineDispatchResult:
    return _dispatch_remote_or_local(
        lambda client: client.dispatch_line(instance_id, line, request=request),
        lambda: playbill_api.playbill_line_dispatch(instance_id, line, request=request),
        operation_name="cruxible_line_dispatch",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_line_run(
    instance_id: str,
    line: str,
    *,
    occurrence_id: str | None,
    evaluation_time: str | None = None,
    resolution_contract: contracts.ResolutionContractReference | None = None,
    trigger_event: contracts.TriggerEventReference | None = None,
    trigger: str | None = None,
) -> contracts.ProcedureRunState:
    request = LineRunRequest.model_validate(
        {
            "line": line,
            "trigger": trigger,
            "resolution_contract": resolution_contract,
            "trigger_event": trigger_event,
            "occurrence_id": occurrence_id,
            "evaluation_time": (
                None if evaluation_time is None else parse_datetime(evaluation_time)
            ),
        }
    )
    return _dispatch_remote_or_local(
        lambda client: client.run_line(
            instance_id,
            resolution_contract=resolution_contract,
            trigger_event=trigger_event,
            trigger=trigger,
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
        operation_name="cruxible_line_run",
    )


def handle_playbill_resolution_contracts(
    instance_id: str, request: contracts.ResolutionContractsRequest
) -> contracts.ResolutionContractsResult:
    return _dispatch_remote_or_local(
        lambda client: client.resolution_contracts(instance_id, request=request),
        lambda: playbill_api.playbill_resolution_contracts(instance_id, request=request),
        operation_name="cruxible_resolution_contracts",
    )


def handle_playbill_predict(
    instance_id: str,
    request: contracts.PredictRequest,
) -> contracts.PredictResult:
    return _dispatch_remote_or_local(
        lambda client: client.predict(instance_id, request=request),
        lambda: playbill_api.playbill_predict(instance_id, request=request),
        operation_name="cruxible_predict",
        local_payload=request.model_dump(mode="json"),
    )


def handle_playbill_settle_prediction(
    instance_id: str,
    prediction_id: str,
    request: contracts.SettleRequest,
) -> contracts.SettleResult:
    return _dispatch_remote_or_local(
        lambda client: client.settle_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        lambda: playbill_api.playbill_settle_prediction(
            instance_id,
            prediction_id,
            request=request,
        ),
        operation_name="cruxible_settle",
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
) -> contracts.SinceResult:
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
        lambda client: client.since(
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
        operation_name="cruxible_since",
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
) -> contracts.NextResult:
    """Rank outstanding repair work, observing the MCP workspace as `cruxible next` does."""

    stamped = (
        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if evaluation_time is None
        else evaluation_time
    )
    profile = CoverageAccessProfile.model_validate(
        access_profile
        or {"profile_id": "mcp-next", "permitted_access_classes": ["instance", "public"]}
    ).model_dump(mode="json")
    # The workspace observation is optional: a root that is no workspace is observed
    # as no workspace rather than refusing the queue.
    workspace = mcp_workspace_root(guard=False)
    observation = observe_next_workspace(workspace)
    # Rows render as MCP tool calls, and a row whose repair is a tool this
    # session does not advertise keeps its place with the repair withheld and
    # `repair_requires` naming the tool and profile it needs.
    from cruxible_core.mcp.curation import session_tool_names

    tools = tuple(sorted(session_tool_names()))

    def remote(client: CruxibleClient) -> contracts.NextResult:
        observed, coordinate = observe_next_workspace_with_coverage(
            client,
            instance_id,
            workspace,
            observation=observation,
            access_profile=profile,
        )
        return client.next(
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

    def local() -> contracts.NextResult:
        observed, coordinate = observe_next_workspace_with_coverage(
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
        operation_name="cruxible_next",
    )


def handle_playbill_curation_list(
    instance_id: str,
    *,
    evaluation_time: str,
    access_profile: dict[str, Any] | None,
    workspace_observation: dict[str, Any] | None,
    limit: int = contracts.CURATION_LIST_DEFAULT_LIMIT,
    cursor: str | None = None,
) -> contracts.CurationListResult:
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
        lambda client: client.list_curation(
            instance_id,
            evaluation_time=evaluation_time,
            access_profile=profile,
            workspace_observation=workspace_observation,
            limit=limit,
            cursor=cursor,
        ),
        lambda: playbill_api.playbill_curation_list(instance_id, request=request),
        operation_name="cruxible_curation_list",
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
) -> contracts.AuditResult:
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
        lambda client: client.audit(
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
        operation_name="cruxible_audit",
    )


def handle_playbill_curation_overrule(
    instance_id: str,
    *,
    item_id: str,
    expected_latest_event_digest: str,
    reason: str,
    attribution_refs: list[str],
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.CurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.overrule_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=tuple(attribution_refs),
            dry_run=dry_run,
            at=at,
        ),
        lambda: playbill_api.playbill_curation_overrule(
            instance_id,
            request={
                "tag": "playbill-curation-overrule-request-v1",
                "item_id": item_id,
                "expected_latest_event_digest": expected_latest_event_digest,
                "reason": reason,
                "attribution_refs": attribution_refs,
                "dry_run": dry_run,
                "at": at,
            },
        ),
        operation_name="cruxible_curation_overrule",
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "attribution_refs": attribution_refs,
            "dry_run": dry_run,
            "at": at,
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
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.CurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.accept_fixed_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=tuple(attribution_refs),
            dry_run=dry_run,
            at=at,
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
                "dry_run": dry_run,
                "at": at,
            },
        ),
        operation_name="cruxible_curation_accept_fixed",
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "accepted_proposal_id": accepted_proposal_id,
            "accepted_changeset_digest": accepted_changeset_digest,
            "attribution_refs": attribution_refs,
            "dry_run": dry_run,
            "at": at,
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
    dry_run: bool | None = None,
    at: str | None = None,
) -> contracts.CurationActionResult:
    return _dispatch_remote_or_local(
        lambda client: client.suppress_curation(
            instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=tuple(attribution_refs),
            dry_run=dry_run,
            at=at,
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
                "dry_run": dry_run,
                "at": at,
            },
        ),
        operation_name="cruxible_curation_suppress",
        local_payload={
            "item_id": item_id,
            "expected_latest_event_digest": expected_latest_event_digest,
            "reason": reason,
            "scope": scope,
            "until_generation": until_generation,
            "attribution_refs": attribution_refs,
            "dry_run": dry_run,
            "at": at,
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
) -> contracts.CoverageResult:
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
            WorkingSourceObservation.model_validate(item) for item in observations or ()
        )
    cards = None if budget is None else CoverageCardBudget.model_validate(budget)
    scan = None if scan_budget is None else CoverageScanBudget.model_validate(scan_budget)
    return _dispatch_remote_or_local(
        lambda client: client.resolve_coverage(
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
        operation_name="cruxible_coverage",
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
        operation_name="cruxible_workspace_source_compile",
    )


def _workspace_observations(
    bindings: Mapping[str, str],
    *,
    files: tuple[str, ...],
    ranges: tuple[str, ...],
    grep_results_path: str | None,
    whole_working_set: bool,
) -> tuple[WorkingSourceObservation, ...]:
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
    include: tuple[contracts.FloorExportPart, ...] = (),
) -> contracts.FloorExport | contracts.WorkspaceFloorWriteResult | contracts.WorkspaceFloorStatus:
    """Return floor bytes, write them under the MCP workspace, or report that floor's status."""

    if force and mode != "write":
        raise DataValidationError("force applies only to floor export mode 'write'")
    if include and mode == "status":
        raise DataValidationError("include applies only to floor export modes 'bytes' and 'write'")
    if mode == "status":
        head = _dispatch_remote_or_local(
            lambda client: client.head(instance_id),
            lambda: playbill_api.playbill_head(instance_id),
            operation_name="cruxible_floor_export",
        )
        return inspect_workspace_floor(
            mcp_git_workspace_root(),
            current_coordinate=contracts.AcceptedCoordinate.model_validate(
                head.coordinate.model_dump(mode="json")
            ),
        )
    parts = floor_export_parts(include)
    if mode == "bytes":
        return _dispatch_remote_or_local(
            lambda client: client.export_floor(instance_id, **parts),
            lambda: playbill_api.playbill_export_floor(instance_id, **parts),
            operation_name="cruxible_floor_export",
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
        export_floor: Callable[[], contracts.FloorExport],
        delivery: Callable[[], contracts.FloorDeliveryResult | None] | None = None,
    ) -> contracts.WorkspaceFloorWriteResult:
        return write_workspace_floor(
            export_floor,
            delivery=delivery,
            instance_id=instance_id,
            workspace=workspace,
            include=include,
            force=force,
            **transport,
        )[1]

    def write_delta(client: Any) -> contracts.WorkspaceFloorWriteResult:
        # The default floor goes through the one shared apply, as a delta from
        # the floor already there.
        return write_workspace_floor_delta(
            lambda generation, renderer: client.floor_delta(
                instance_id, base_generation=generation, base_renderer=renderer
            ),
            delivery=(lambda: daemon_floor_delivery(client, instance_id, workspace))
            if transport.get("server_socket")
            else None,
            instance_id=instance_id,
            workspace=workspace,
            force=force,
            **transport,
        )[1]

    if not include:
        return _dispatch_remote_or_local(
            write_delta,
            lambda: write_delta(_LocalFloorClient()),
            operation_name="cruxible_floor_export",
        )
    return _dispatch_remote_or_local(
        lambda client: write(
            lambda: client.export_floor(instance_id, **parts),
            (lambda: daemon_floor_delivery(client, instance_id, workspace, include=tuple(include)))
            if transport.get("server_socket")
            else None,
        ),
        lambda: write(lambda: playbill_api.playbill_export_floor(instance_id, **parts)),
        operation_name="cruxible_floor_export",
    )
