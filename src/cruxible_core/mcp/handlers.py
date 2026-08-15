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
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.types import PrincipalRecord
from cruxible_core.runtime import host_api, playbill_api
from cruxible_core.server.config import get_runtime_bearer_token, resolve_server_settings

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
