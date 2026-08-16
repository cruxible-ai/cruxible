"""Playbill-only runtime facade shared by HTTP routes and MCP handlers.

This module is intentionally independent of the legacy graph/config runtime.
Public surfaces translate transport contracts here, then delegate to the
typed Playbill services.
"""

from __future__ import annotations

import base64
from typing import Literal

from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.errors import AuthenticationError, ConfigError, DataValidationError
from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.candidates import canonical_candidate_timestamp
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_dereference_playbill_document,
    service_get_playbill_document,
    service_inspect_playbill_proposal,
    service_inspect_playbill_refusal,
    service_list_playbill_documents,
    service_list_playbill_principals,
    service_playbill_document_history,
    service_propose_playbill_document,
    service_propose_playbill_principal_change,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.explain import service_explain_playbill_subject
from cruxible_core.playbill.service.review import (
    service_prepare_playbill_approval,
    service_review_playbill_proposal,
)
from cruxible_core.playbill.service.source_catalog import (
    service_check_playbill_source_bundle,
    service_playbill_source_context,
    service_propose_playbill_source_bundle,
)
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.types import OperatingProfile, PrincipalRecord
from cruxible_core.primitives import new_id
from cruxible_core.runtime.permissions import check_permission
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.actor_identity import local_operator_actor_context
from cruxible_core.server.auth import (
    get_current_auth_context,
    set_current_operation_id,
)
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.temporal import utc_now


def _credential_actor_context() -> GovernedActorContext | None:
    auth_context = get_current_auth_context()
    if auth_context is None or auth_context.credential_type != "runtime_credential":
        return None
    try:
        return GovernedActorContext(
            actor_type="service_account",
            actor_id=auth_context.principal_label,
            org_id=auth_context.instance_scope or "local",
            operation_id=new_id("op", length=16, separator="_"),
            timestamp=utc_now(),
        )
    except ValidationError as exc:
        raise ConfigError("hosted governed actor context is required") from exc


def _actor_context() -> GovernedActorContext | None:
    actor = _credential_actor_context()
    if actor is None and not is_server_auth_enabled():
        actor = local_operator_actor_context()
    if actor is not None:
        set_current_operation_id(actor.operation_id)
    return actor


def _actor_id() -> str:
    """Use credential-derived request identity at every Playbill write boundary."""

    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Playbill writes require an authenticated actor identity")
    return actor.actor_id


def _access(instance_id: str, *, include_body: bool) -> BodyAccessContext:
    actor = _actor_context()
    principal_id = "anonymous" if actor is None else actor.actor_id
    if include_body:
        check_permission("cruxible_playbill_body_read", instance_id=instance_id)
    return BodyAccessContext(principal_id=principal_id, can_read_body=include_body)


def playbill_init(
    instance_id: str,
    *,
    principals: tuple[PrincipalRecord, ...],
    operating_profile: OperatingProfile = "local",
) -> contracts.PlaybillInitResult:
    check_permission("cruxible_playbill_init", instance_id=instance_id)
    actor_id = _actor_id()
    owners = {
        item.principal_id
        for item in principals
        if item.status == "active" and "owner" in item.authority_roles
    }
    if actor_id not in owners:
        raise AuthenticationError(
            "Playbill bootstrap requires an owner principal matching authenticated identity"
        )
    instance = get_playbill_manager().initialize(
        instance_id,
        client_principals=principals,
        operating_profile=operating_profile,
    )
    return contracts.PlaybillInitResult(
        instance_id=instance_id,
        coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
            AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
        ),
        trust_root=instance.trust_root.model_dump(mode="json"),
        recovery_posture=instance.descriptor.recovery_posture,
    )


def playbill_store_body(
    instance_id: str, *, content_base64: str
) -> contracts.PlaybillCasObjectResult:
    check_permission("cruxible_playbill_store_body", instance_id=instance_id)
    try:
        content = base64.b64decode(content_base64, validate=True)
    except ValueError as exc:
        raise DataValidationError("Playbill body is not canonical base64") from exc
    result = service_store_playbill_body(get_playbill_manager().get(instance_id), content=content)
    return contracts.PlaybillCasObjectResult.model_validate(result.model_dump(mode="json"))


def playbill_propose_document(
    instance_id: str,
    *,
    shell: DocumentShell,
    proposal_name: str,
    source_compilation_digest: str | None = None,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_document(
        get_playbill_manager().get(instance_id),
        shell=shell,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        source_compilation_digest=source_compilation_digest,
        base=base,
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_propose_principal_change(
    instance_id: str,
    *,
    principal: PrincipalRecord,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_principal_change", instance_id=instance_id)
    result = service_propose_playbill_principal_change(
        get_playbill_manager().get(instance_id),
        principal=principal,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        base=base,
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_inspect_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_inspect", instance_id=instance_id)
    result = service_inspect_playbill_proposal(
        get_playbill_manager().get(instance_id), proposal_id=proposal_id
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_inspect_refusal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillRefusalInspection:
    check_permission("cruxible_playbill_inspect", instance_id=instance_id)
    result = service_inspect_playbill_refusal(
        get_playbill_manager().get(instance_id), proposal_id=proposal_id
    )
    return contracts.PlaybillRefusalInspection.model_validate(result.model_dump(mode="json"))


def playbill_review_proposal(
    instance_id: str,
    proposal_id: str,
    *,
    include_body: bool = False,
) -> contracts.PlaybillProposalReview:
    check_permission("cruxible_playbill_review", instance_id=instance_id)
    result = service_review_playbill_proposal(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        access=_access(instance_id, include_body=include_body),
    )
    return contracts.PlaybillProposalReview.model_validate(result.model_dump(mode="json"))


def playbill_prepare_approval(
    instance_id: str,
    proposal_id: str,
    *,
    signer_id: str,
    include_body: bool = False,
) -> contracts.PlaybillApprovalChallenge:
    check_permission("cruxible_playbill_review", instance_id=instance_id)
    result = service_prepare_playbill_approval(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        signer_id=signer_id,
        access=_access(instance_id, include_body=include_body),
    )
    return contracts.PlaybillApprovalChallenge.model_validate(result.model_dump(mode="json"))


def playbill_submit_approval(
    instance_id: str,
    proposal_id: str,
    *,
    attestation: ApprovalAttestation,
) -> contracts.PlaybillApprovalReceipt:
    check_permission("cruxible_playbill_submit_approval", instance_id=instance_id)
    result = service_submit_playbill_approval(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        attestation=attestation,
        authenticated_submitter=_actor_id(),
    )
    return contracts.PlaybillApprovalReceipt.model_validate(result.model_dump(mode="json"))


def playbill_activate(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillActivationReceipt:
    check_permission("cruxible_playbill_activate", instance_id=instance_id)
    _actor_id()
    result = service_activate_playbill_proposal(
        get_playbill_manager().get(instance_id), proposal_id=proposal_id
    )
    return contracts.PlaybillActivationReceipt.model_validate(result.model_dump(mode="json"))


def playbill_get_document(
    instance_id: str,
    identity: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillDocumentView:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_document(
        get_playbill_manager().get(instance_id),
        identity=identity,
        access=_access(instance_id, include_body=False),
        at=at,
    )
    return contracts.PlaybillDocumentView.model_validate(result.model_dump(mode="json"))


def playbill_list_documents(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillDocumentList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_documents(
        get_playbill_manager().get(instance_id),
        access=_access(instance_id, include_body=False),
        at=at,
    )
    return contracts.PlaybillDocumentList.model_validate(result.model_dump(mode="json"))


def playbill_list_principals(instance_id: str) -> contracts.PlaybillPrincipalList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_principals(get_playbill_manager().get(instance_id))
    return contracts.PlaybillPrincipalList.model_validate(result.model_dump(mode="json"))


def playbill_dereference_document(
    instance_id: str,
    identity: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillBodyRead:
    check_permission("cruxible_playbill_body_read", instance_id=instance_id)
    result = service_dereference_playbill_document(
        get_playbill_manager().get(instance_id),
        identity=identity,
        access=_access(instance_id, include_body=True),
        at=at,
    )
    return contracts.PlaybillBodyRead.model_validate(result.model_dump(mode="json"))


def playbill_document_history(
    instance_id: str,
    identity: str,
) -> contracts.PlaybillDocumentHistory:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_playbill_document_history(
        get_playbill_manager().get(instance_id), identity=identity
    )
    return contracts.PlaybillDocumentHistory.model_validate(result.model_dump(mode="json"))


def playbill_explain(
    instance_id: str,
    *,
    subject: SemanticAddress,
    at: AcceptedCoordinate,
    detail: Literal["summary", "evidence", "proof"] = "summary",
    include_body: bool = False,
) -> contracts.PlaybillExplainResult | contracts.PlaybillExplainUnsupportedDetail:
    check_permission("cruxible_playbill_explain", instance_id=instance_id)
    result = service_explain_playbill_subject(
        get_playbill_manager().get(instance_id),
        subject=subject,
        at=at,
        detail=detail,
        access=_access(instance_id, include_body=include_body),
    )
    payload = result.model_dump(mode="json")
    if result.tag == "playbill-explain-v1":
        return contracts.PlaybillExplainResult.model_validate(payload)
    return contracts.PlaybillExplainUnsupportedDetail.model_validate(payload)


def playbill_source_context(instance_id: str) -> contracts.PlaybillSourceContext:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_playbill_source_context(get_playbill_manager().get(instance_id))
    return contracts.PlaybillSourceContext.model_validate(result.model_dump(mode="json"))


def playbill_check_source_bundle(
    instance_id: str,
    *,
    bundle: SourceCompilationBundle,
) -> contracts.PlaybillSourceCheckResult:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_check_playbill_source_bundle(
        get_playbill_manager().get(instance_id), bundle=bundle
    )
    return contracts.PlaybillSourceCheckResult.model_validate(result.model_dump(mode="json"))


def playbill_propose_source_bundle(
    instance_id: str,
    *,
    bundle: SourceCompilationBundle,
    source_name: str,
    proposal_name: str,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_source_bundle(
        get_playbill_manager().get(instance_id),
        bundle=bundle,
        source_name=source_name,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


__all__ = [name for name in globals() if name.startswith("playbill_")]
