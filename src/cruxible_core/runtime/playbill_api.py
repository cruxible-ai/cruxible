"""Playbill-only runtime facade shared by HTTP routes and MCP handlers.

This module is intentionally independent of the legacy graph/config runtime.
Public surfaces translate transport contracts here, then delegate to the
typed Playbill services.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.errors import AuthenticationError, ConfigError, DataValidationError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.candidates import canonical_candidate_timestamp
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_types import ClaimType
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.discovery import (
    DiscoveryBudgetV1,
    ExpandRequestV1,
    ExpansionBudgetV1,
)
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.claim_types import (
    service_get_playbill_claim_type,
    service_list_playbill_claim_types,
    service_propose_playbill_claim_type,
)
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
from cruxible_core.playbill.service.query_definitions import (
    service_get_playbill_query_definition,
    service_list_playbill_query_definitions,
    service_propose_playbill_query_definition,
)
from cruxible_core.playbill.service.review import (
    service_prepare_playbill_approval,
    service_review_playbill_proposal,
)
from cruxible_core.playbill.service.source_catalog import (
    service_check_playbill_source_bundle,
    service_playbill_source_context,
    service_propose_playbill_source_bundle,
)
from cruxible_core.playbill.service.subjects import (
    service_get_playbill_subject,
    service_list_playbill_subjects,
    service_playbill_subject_history,
    service_propose_playbill_subject,
)
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.subjects import SubjectShell
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
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    service_expand_playbill_semantic,
    service_explain_playbill_claim,
    service_get_playbill_claim,
    service_list_playbill_claims,
    service_playbill_claim_history,
    service_propose_playbill_claim,
)
from cruxible_core.service.playbill_coverage import service_resolve_playbill_coverage
from cruxible_core.service.playbill_discovery import service_discover_playbill_semantic
from cruxible_core.service.playbill_floor import MANIFEST_PATH, service_export_playbill_floor
from cruxible_core.service.playbill_query import service_run_playbill_query
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


def _accepted_coordinate(instance_id: str, at: AcceptedCoordinate | None) -> AcceptedCoordinate:
    """Resolve the caller's coordinate, defaulting to the accepted head."""

    if at is not None:
        return at
    instance = get_playbill_manager().get(instance_id)
    return AcceptedCoordinate.from_internal(instance.accepted_coordinate())


def _evaluation_time(value: datetime | None) -> datetime:
    return utc_now() if value is None else value


def _evaluation_timestamp(value: str | None) -> str:
    return canonical_candidate_timestamp(utc_now()) if value is None else value


def playbill_propose_subject(
    instance_id: str,
    *,
    shell: SubjectShell,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_subject(
        get_playbill_manager().get(instance_id),
        shell=shell,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        base=base,
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_list_subjects(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillSubjectList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_subjects(get_playbill_manager().get(instance_id), at=at)
    return contracts.PlaybillSubjectList.model_validate(result.model_dump(mode="json"))


def playbill_get_subject(
    instance_id: str,
    identity: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillSubjectView:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_subject(
        get_playbill_manager().get(instance_id), identity=identity, at=at
    )
    return contracts.PlaybillSubjectView.model_validate(result.model_dump(mode="json"))


def playbill_subject_history(
    instance_id: str,
    identity: str,
) -> contracts.PlaybillSubjectHistory:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_playbill_subject_history(
        get_playbill_manager().get(instance_id), identity=identity
    )
    return contracts.PlaybillSubjectHistory.model_validate(result.model_dump(mode="json"))


def playbill_propose_claim_type(
    instance_id: str,
    *,
    claim_type: ClaimType,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_claim_type(
        get_playbill_manager().get(instance_id),
        claim_type=claim_type,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        base=base,
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_list_claim_types(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillClaimTypeList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_claim_types(get_playbill_manager().get(instance_id), at=at)
    return contracts.PlaybillClaimTypeList.model_validate(result.model_dump(mode="json"))


def playbill_get_claim_type(
    instance_id: str,
    predicate: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillClaimTypeView:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_claim_type(
        get_playbill_manager().get(instance_id), predicate=predicate, at=at
    )
    return contracts.PlaybillClaimTypeView.model_validate(result.model_dump(mode="json"))


def playbill_propose_claim(
    instance_id: str,
    *,
    authoring: DirectClaimAuthoringV1,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillClaimProposal:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_claim(
        get_playbill_manager().get(instance_id),
        authoring=authoring,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        base=base,
    )
    return contracts.PlaybillClaimProposal.model_validate(result.model_dump(mode="json"))


def playbill_list_claims(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
    subject: SemanticAddress | None = None,
    predicate: str | None = None,
    include_retired: bool = False,
) -> contracts.PlaybillClaimList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_claims(
        get_playbill_manager().get(instance_id),
        at=at,
        subject=subject,
        predicate=predicate,
        include_retired=include_retired,
    )
    return contracts.PlaybillClaimList.model_validate(result.model_dump(mode="json"))


def playbill_get_claim(
    instance_id: str,
    identity: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillClaimView:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_claim(
        get_playbill_manager().get(instance_id), identity=identity, at=at
    )
    return contracts.PlaybillClaimView.model_validate(result.model_dump(mode="json"))


def playbill_claim_history(
    instance_id: str,
    identity: str,
) -> contracts.PlaybillClaimHistory:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_playbill_claim_history(
        get_playbill_manager().get(instance_id), identity=identity
    )
    return contracts.PlaybillClaimHistory.model_validate(result.model_dump(mode="json"))


def playbill_explain_claim(
    instance_id: str,
    identity: str,
    *,
    at: AcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
) -> contracts.PlaybillClaimExplanation:
    check_permission("cruxible_playbill_explain", instance_id=instance_id)
    result = service_explain_playbill_claim(
        get_playbill_manager().get(instance_id),
        identity=identity,
        at=at,
        evaluation_time=_evaluation_time(evaluation_time),
    )
    return contracts.PlaybillClaimExplanation.model_validate(result.model_dump(mode="json"))


def playbill_propose_query_definition(
    instance_id: str,
    *,
    query: QueryDefinitionV1,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_propose_playbill_query_definition(
        get_playbill_manager().get(instance_id),
        query=query,
        actor_id=_actor_id(),
        proposal_name=proposal_name,
        timestamp=canonical_candidate_timestamp(utc_now()),
        base=base,
    )
    return contracts.PlaybillProposalInspection.model_validate(result.model_dump(mode="json"))


def playbill_list_query_definitions(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillQueryDefinitionList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_query_definitions(get_playbill_manager().get(instance_id), at=at)
    return contracts.PlaybillQueryDefinitionList.model_validate(result.model_dump(mode="json"))


def playbill_get_query_definition(
    instance_id: str,
    name: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillQueryDefinitionView:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_query_definition(
        get_playbill_manager().get(instance_id), name=name, at=at
    )
    return contracts.PlaybillQueryDefinitionView.model_validate(result.model_dump(mode="json"))


def playbill_run_query(
    instance_id: str,
    name: str,
    *,
    at: AcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
    parameters: Mapping[str, Any] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> contracts.PlaybillQueryRun:
    """Execute one accepted QueryDefinition and return its result and receipt.

    No receipt journal is opened here: the journal backend is caller-owned
    exactly as it is for Procedure exhaust, so ``journal_record_digest`` is
    absent at this surface until PC-G wires a daemon-owned journal.
    """

    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_run_playbill_query(
        get_playbill_manager().get(instance_id),
        name=name,
        evaluation_time=_evaluation_time(evaluation_time),
        parameters=parameters,
        at=at,
        budgets=budgets,
    )
    return contracts.PlaybillQueryRun.model_validate(result.model_dump(mode="json"))


def playbill_discover(
    instance_id: str,
    *,
    query: str | None = None,
    entrypoint: str | None = None,
    at: AcceptedCoordinate | None = None,
    evaluation_time: str | None = None,
    profile: Literal["interfaces", "subjects", "all"] = "interfaces",
    budget: DiscoveryBudgetV1 | None = None,
) -> contracts.PlaybillDiscoveryResult:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_discover_playbill_semantic(
        get_playbill_manager().get(instance_id),
        evaluation_time=_evaluation_timestamp(evaluation_time),
        query=query,
        entrypoint=entrypoint,
        at=at,
        profile=profile,
        budget=budget or DiscoveryBudgetV1(),
    )
    return contracts.PlaybillDiscoveryResult.model_validate(result.model_dump(mode="json"))


def playbill_expand(
    instance_id: str,
    *,
    address: SemanticAddress,
    at: AcceptedCoordinate | None = None,
    evaluation_time: str | None = None,
    facets: tuple[str, ...] = (),
    budget: ExpansionBudgetV1 | None = None,
) -> contracts.PlaybillContextCapsule:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_expand_playbill_semantic(
        get_playbill_manager().get(instance_id),
        request=ExpandRequestV1(
            address=address,
            at=_accepted_coordinate(instance_id, at),
            evaluation_time=_evaluation_timestamp(evaluation_time),
            facets=facets,
            budget=budget or ExpansionBudgetV1(),
        ),
    )
    return contracts.PlaybillContextCapsule.model_validate(result.model_dump(mode="json"))


def playbill_resolve_coverage(
    instance_id: str,
    *,
    observations: tuple[WorkingSourceObservationV1, ...],
    at: AcceptedCoordinate | None = None,
    budget: CoverageCardBudgetV1 | None = None,
    scan_budget: CoverageScanBudgetV1 | None = None,
) -> contracts.PlaybillCoverageResult:
    """Resolve one batch of working-set observations into a `CoverageResultV1`.

    The one vendor-neutral coverage operation of §11.7. Every request form it
    has to serve -- a file read with a line/range selection, a grep result
    batch, a set of changed filesystem paths, an explicit source occurrence, and
    a working-set scope -- arrives here already reduced by the adapter to
    observations and the spans they carry, because an adapter contains no
    semantic logic and this operation reads no filesystem.

    **No receipt is appended, and that is a decision rather than an omission.**
    §11.6 makes coverage delivery semantically side-effect-free: it changes no
    accepted state, no candidate, no permission, no verdict input, and no
    evaluation episode, and it adds no authority to the material it describes.
    Whether ordinary reads append to a daemon-owned journal is PC-G's
    journal-ownership decision -- the same open seam that leaves
    ``journal_record_digest`` absent on query execution -- and settling it here
    would settle it from the wrong end, by making the highest-frequency read in
    the system the first journal writer. A coverage answer stays checkable
    without one: it names the evidence-index digest, the overlay digest, and the
    manifest digest it resolved against, and those three reproduce it exactly.

    The access profile is derived from this surface's read authority and is
    never accepted from the caller, so a request cannot widen its own
    disclosure.
    """

    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_resolve_playbill_coverage(
        get_playbill_manager().get(instance_id),
        instance_id=instance_id,
        observations=observations,
        at=at,
        budget=budget,
        scan_budget=scan_budget,
    )
    return contracts.PlaybillCoverageResult(
        coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(
            result.at.model_dump(mode="json")
        ),
        result=result.model_dump(mode="json"),
    )


def playbill_export_floor(
    instance_id: str,
    *,
    at: AcceptedCoordinate | None = None,
) -> contracts.PlaybillFloorExport:
    """Return the deterministic floor as base64 bytes keyed by floor path.

    The service returns a path-to-bytes map and writes nothing; materializing a
    directory from this contract is the client's act, never the daemon's.
    """

    check_permission("cruxible_playbill_read", instance_id=instance_id)
    files = service_export_playbill_floor(
        get_playbill_manager().get(instance_id),
        at=at,
        access=_access(instance_id, include_body=False),
    )
    manifest = json.loads(files[MANIFEST_PATH])
    return contracts.PlaybillFloorExport(
        coordinate=contracts.PlaybillAcceptedCoordinate.model_validate(manifest["coordinate"]),
        manifest=manifest,
        files=[
            contracts.PlaybillFloorFile(
                path=path,
                content_base64=base64.b64encode(content).decode("ascii"),
            )
            for path, content in files.items()
        ],
    )


__all__ = [name for name in globals() if name.startswith("playbill_")]
