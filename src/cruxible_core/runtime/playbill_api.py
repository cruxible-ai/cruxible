"""Playbill-only runtime facade shared by HTTP routes and MCP handlers.

This module is intentionally independent of the legacy graph/config runtime.
Public surfaces translate transport contracts here, then delegate to the
typed Playbill services.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, TypeVar, cast

from pydantic import TypeAdapter, ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1
from cruxible_client.contracts.authoring.models import (
    AuthoringPayloadV1,
    AuthoringProgramStampV1,
    AuthoringReferenceExpectationV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    InsertionConfirmationObservationV2,
    PreflightResultV1,
    PublicationSourceObservationV2,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.candidates import canonical_candidate_timestamp
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
)
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import ClaimRetireRequestV1
from cruxible_client.contracts.discovery import (
    DiscoveryBudgetV1,
    ExpandRequestV1,
    ExpansionBudgetV1,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_client.contracts.primitives import new_id
from cruxible_client.contracts.procedures.artifacts import procedure_path
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.types import OperatingProfile, PrincipalRecord
from cruxible_core.errors import AuthenticationError, ConfigError, DataValidationError
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.claim_retirement import (
    ClaimRetireResponse,
    service_retire_claim,
)
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1, lint_claim_type_input
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeMigrationRequest,
    service_migrate_claim_type,
)
from cruxible_core.playbill.consumption import (
    ConsumptionContextV1,
    ConsumptionOperation,
    consumption_artifacts_for_dependency_closure,
    consumption_artifacts_for_paths,
    record_consumption,
)
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.search import (
    SEARCH_KINDS,
    PlaybillSearchBudgetsV1,
    PlaybillSearchCursorV1,
    PlaybillSearchRequestV1,
    SearchKind,
    SearchMode,
    SearchStatus,
)
from cruxible_core.playbill.service.claim_types import (
    service_get_playbill_claim_type,
    service_list_playbill_claim_types,
    service_propose_playbill_claim_type,
    service_propose_playbill_claim_type_input,
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
from cruxible_core.runtime.permissions import check_permission, get_current_mode
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.actor_identity import local_operator_actor_context
from cruxible_core.server.auth import (
    get_current_auth_context,
    set_current_operation_id,
)
from cruxible_core.server.config import is_server_auth_enabled
from cruxible_core.service.playbill_audit import (
    PlaybillAuditRequestV1,
    service_playbill_audit,
    validate_playbill_audit_request,
)
from cruxible_core.service.playbill_claim_attestations import service_append_claim_attestation
from cruxible_core.service.playbill_claims import (
    service_expand_playbill_semantic,
    service_explain_playbill_claim,
    service_get_playbill_claim,
    service_list_playbill_claims,
    service_playbill_claim_history,
)
from cruxible_core.service.playbill_coverage import (
    coverage_access_profile,
    service_resolve_playbill_coverage,
)
from cruxible_core.service.playbill_curation import (
    PlaybillCurationAcceptFixedRequestV1,
    PlaybillCurationError,
    PlaybillCurationListRequestV1,
    PlaybillCurationOverruleRequestV1,
    PlaybillCurationSuppressRequestV1,
    service_accept_fixed_playbill_curation,
    service_list_playbill_curation,
    service_overrule_playbill_curation,
    service_suppress_playbill_curation,
    validate_playbill_curation_list_request,
)
from cruxible_core.service.playbill_discovery import (
    PlaybillDiscoveryResultV1,
    service_discover_playbill_semantic,
)
from cruxible_core.service.playbill_floor import MANIFEST_PATH, service_export_playbill_floor
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    service_playbill_next,
    validate_playbill_next_request,
)
from cruxible_core.service.playbill_procedure_runs import (
    ProcedureBindRequestV1,
    ProcedureReadinessRequestV1,
    ProcedureRunRequestV1,
    service_bind_playbill_procedure,
    service_get_playbill_procedure_run,
    service_playbill_procedure_readiness,
    service_run_playbill_procedure,
)
from cruxible_core.service.playbill_proposals import (
    ProposalInventoryStatus,
    service_list_playbill_proposals,
    service_playbill_whoami,
    service_readmit_playbill_proposal,
)
from cruxible_core.service.playbill_query import service_run_playbill_query
from cruxible_core.service.playbill_search import service_search_playbill
from cruxible_core.service.playbill_since import (
    service_playbill_since,
    validate_playbill_since_request,
)

_ProposalResultT = TypeVar("_ProposalResultT")
_CurationRequestT = TypeVar("_CurationRequestT")


def _proposal_validation_boundary(
    family: str,
    operation: Callable[[], _ProposalResultT],
) -> _ProposalResultT:
    """Map any residual Pydantic proposal-ref failure to the typed HTTP 400 family."""

    try:
        return operation()
    except ValidationError as exc:
        raise DataValidationError(
            f"Playbill {family} proposal reference is invalid",
            errors=[str(exc)],
        ) from exc


def _curation_validation_boundary(
    operation: Callable[[], _CurationRequestT],
) -> _CurationRequestT:
    """Map internal curation request validation to its typed HTTP 400 family."""

    try:
        return operation()
    except ValidationError as exc:
        raise PlaybillCurationError(
            f"{PlaybillCurationError.code}: curation request is malformed: {exc}"
        ) from exc


_CLAIM_TYPE_MIGRATION_RESPONSE: TypeAdapter[contracts.PlaybillClaimTypeMigrationResponse] = (
    TypeAdapter(contracts.PlaybillClaimTypeMigrationResponse)
)
_CLAIM_RETIRE_RESPONSE: TypeAdapter[contracts.PlaybillClaimRetireResponse] = TypeAdapter(
    contracts.PlaybillClaimRetireResponse
)


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


def _consumption_context() -> ConsumptionContextV1 | None:
    actor = _actor_context()
    if actor is None:
        return None
    return ConsumptionContextV1(
        actor_context=actor,
        access_profile_id=coverage_access_profile().profile_id,
    )


def _record_consumed_paths(
    instance_id: str,
    *,
    operation: ConsumptionOperation,
    coordinate: AcceptedCoordinate,
    paths: tuple[str, ...],
) -> None:
    instance = get_playbill_manager().get(instance_id)
    record_consumption(
        instance,
        context=_consumption_context(),
        operation=operation,
        coordinate=coordinate,
        artifacts=consumption_artifacts_for_paths(
            instance.tree_at(coordinate.git_oid),
            paths,
        ),
    )


def playbill_init(
    instance_id: str,
    *,
    principals: tuple[PrincipalRecord, ...],
    operating_profile: OperatingProfile = "local",
    require_independent_approval: bool = False,
) -> contracts.PlaybillInitResult:
    check_permission("cruxible_playbill_init", instance_id=instance_id)
    actor_id = _actor_id()
    if not principals:
        raise PlaybillBootstrapError("bootstrap requires at least one client principal")
    ordinary = {
        item.principal_id
        for item in principals
        if item.status == "active" and item.kind == "ordinary"
    }
    if actor_id not in ordinary:
        raise AuthenticationError(
            "Playbill bootstrap requires an ordinary principal matching authenticated identity"
        )
    instance = get_playbill_manager().initialize(
        instance_id,
        client_principals=principals,
        operating_profile=operating_profile,
        require_independent_approval=require_independent_approval,
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
    result = _proposal_validation_boundary(
        "document",
        lambda: service_propose_playbill_document(
            get_playbill_manager().get(instance_id),
            shell=shell,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
            source_compilation_digest=source_compilation_digest,
            base=base,
        ),
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
    result = _proposal_validation_boundary(
        "principal",
        lambda: service_propose_playbill_principal_change(
            get_playbill_manager().get(instance_id),
            principal=principal,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
            base=base,
        ),
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


def playbill_list_proposals(
    instance_id: str,
    *,
    status: ProposalInventoryStatus | None = None,
) -> contracts.PlaybillProposalList:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_list_playbill_proposals(
        get_playbill_manager().get(instance_id),
        status=status,
    )
    return contracts.PlaybillProposalList.model_validate(result.model_dump(mode="json"))


def playbill_readmit_proposal(
    instance_id: str,
    proposal_id: str,
) -> contracts.PlaybillProposalReadmitResult:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_readmit_playbill_proposal(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        actor_id=_actor_id(),
    )
    return contracts.PlaybillProposalReadmitResult.model_validate(result.model_dump(mode="json"))


def playbill_whoami(instance_id: str) -> contracts.PlaybillWhoAmI:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    auth_context = get_current_auth_context()
    if auth_context is not None and auth_context.credential_type == "runtime_credential":
        actor_id = auth_context.principal_label
        credential_label = auth_context.principal_label
        actor_id_source = "runtime_credential_label"
    else:
        actor = _actor_context()
        if actor is None:
            raise AuthenticationError("Playbill identity requires an authenticated actor")
        actor_id = actor.actor_id
        credential_label = actor.actor_id
        actor_id_source = "local_operator"
    result = service_playbill_whoami(
        get_playbill_manager().get(instance_id),
        actor_id=actor_id,
        credential_label=credential_label,
        actor_id_source=cast(Any, actor_id_source),
        permission_mode=get_current_mode(),
    )
    return contracts.PlaybillWhoAmI.model_validate(result.model_dump(mode="json"))


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
    activated_by = _actor_id()
    result = service_activate_playbill_proposal(
        get_playbill_manager().get(instance_id),
        proposal_id=proposal_id,
        activated_by=activated_by,
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
    result = _proposal_validation_boundary(
        "source bundle",
        lambda: service_propose_playbill_source_bundle(
            get_playbill_manager().get(instance_id),
            bundle=bundle,
            source_name=source_name,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
        ),
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
    result = _proposal_validation_boundary(
        "subject",
        lambda: service_propose_playbill_subject(
            get_playbill_manager().get(instance_id),
            shell=shell,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
            base=base,
        ),
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
    path = result.envelope.get("path")
    if isinstance(path, str):
        _record_consumed_paths(
            instance_id,
            operation="playbill.subject.get",
            coordinate=result.coordinate,
            paths=(path,),
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
    instance = get_playbill_manager().get(instance_id)
    coordinate = instance.accepted_coordinate()
    result = _proposal_validation_boundary(
        "claim type",
        lambda: service_propose_playbill_claim_type(
            instance,
            claim_type=claim_type,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
            base=base,
        ),
    )
    values = result.model_dump(mode="json")
    lint = lint_claim_type_input(instance, claim_type, coordinate=coordinate)
    if lint.warnings:
        values["lint"] = lint.model_dump(mode="json")
    return contracts.PlaybillProposalInspection.model_validate(values)


def playbill_propose_claim_type_input(
    instance_id: str,
    *,
    input: ClaimTypeInputV1,
    proposal_name: str,
) -> contracts.PlaybillClaimTypeInputProposalResult:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = _proposal_validation_boundary(
        "claim type input",
        lambda: service_propose_playbill_claim_type_input(
            get_playbill_manager().get(instance_id),
            input=input,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
        ),
    )
    return contracts.PlaybillClaimTypeInputProposalResult.model_validate(
        result.model_dump(mode="json")
    )


def playbill_migrate_claim_type(
    instance_id: str,
    *,
    request: ClaimTypeMigrationRequest,
) -> contracts.PlaybillClaimTypeMigrationResponse:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = service_migrate_claim_type(
        get_playbill_manager().get(instance_id),
        request=request,
        actor=AuthenticatedActor(actor_id=_actor_id()),
    )
    return _CLAIM_TYPE_MIGRATION_RESPONSE.validate_python(result.model_dump(mode="json"))


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
    _record_consumed_paths(
        instance_id,
        operation="playbill.claim_type.get",
        coordinate=result.coordinate,
        paths=(result.path,),
    )
    return contracts.PlaybillClaimTypeView.model_validate(result.model_dump(mode="json"))


def playbill_retire_claim(
    instance_id: str,
    claim_id: str,
    *,
    request: ClaimRetireRequestV1,
) -> contracts.PlaybillClaimRetireResponse:
    check_permission("cruxible_playbill_claim_retire", instance_id=instance_id)
    result: ClaimRetireResponse = service_retire_claim(
        get_playbill_manager().get(instance_id),
        claim_id=claim_id,
        request=request,
        actor=AuthenticatedActor(actor_id=_actor_id()),
    )
    return _CLAIM_RETIRE_RESPONSE.validate_python(result.model_dump(mode="json"))


def playbill_append_claim_attestation(
    instance_id: str,
    *,
    request: ClaimAttestationAppendRequestV1,
) -> ClaimAttestationAppendResultV1:
    check_permission("cruxible_playbill_claim_attest", instance_id=instance_id)
    return service_append_claim_attestation(
        get_playbill_manager().get(instance_id),
        request=request,
        actor_id=_actor_id(),
    )


def playbill_recover_claim_attestations(instance_id: str) -> None:
    """Synchronously restore the sole replay-valid evidence-ledger head."""

    check_permission("cruxible_playbill_claim_attestation_recover", instance_id=instance_id)
    get_playbill_manager().get(instance_id).claim_attestation_evidence_store().recover()


def _authoring_coordinator(
    instance_id: str,
) -> tuple[AuthoringIntentCoordinator, AuthenticatedActor]:
    actor = AuthenticatedActor(actor_id=_actor_id())
    instance = get_playbill_manager().get(instance_id)
    return AuthoringIntentCoordinator.for_instance(instance), actor


def playbill_authoring_create(
    instance_id: str,
    *,
    payload: AuthoringPayloadV1,
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...] | None = None,
    program_stamp: AuthoringProgramStampV1 | None = None,
) -> contracts.PlaybillAuthoringIntentView:
    check_permission("cruxible_playbill_authoring_create", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        reference_expectations=reference_expectations,
        program_stamp=program_stamp,
    )
    return contracts.PlaybillAuthoringIntentView.model_validate(result.model_dump(mode="json"))


def playbill_authoring_create_input(
    instance_id: str,
    *,
    input: AuthoringInputV1,
) -> contracts.PlaybillAuthoringIntentView:
    check_permission("cruxible_playbill_authoring_create", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.create_input(
        actor=actor,
        input=input,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
    )
    return contracts.PlaybillAuthoringIntentView.model_validate(result.model_dump(mode="json"))


def playbill_authoring_get(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    check_permission("cruxible_playbill_authoring_get", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.get(intent_id, actor=actor)
    return contracts.PlaybillAuthoringIntentView.model_validate(result.model_dump(mode="json"))


def playbill_authoring_resume(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    check_permission("cruxible_playbill_authoring_resume", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.resume(intent_id, actor=actor)
    return contracts.PlaybillAuthoringIntentView.model_validate(result.model_dump(mode="json"))


def playbill_authoring_list_pending(
    instance_id: str,
) -> contracts.PlaybillAuthoringIntentList:
    check_permission("cruxible_playbill_authoring_list_pending", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.list_pending(actor=actor)
    return contracts.PlaybillAuthoringIntentList.model_validate(result.model_dump(mode="json"))


def _authoring_preflight_result(
    coordinator: AuthoringIntentCoordinator,
    *,
    actor: AuthenticatedActor,
    result: PreflightResultV1,
) -> contracts.PlaybillAuthoringPreflightResult:
    values = result.model_dump(mode="json")
    payload = coordinator.store.get(
        result.certificate.intent_id,
        actor_id=actor.actor_id,
    ).payload
    if isinstance(payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayloadV3):
        claim_type = payload.dependency_drafts.claim_type
        if claim_type is not None:
            at = result.certificate.accepted_coordinate
            coordinate = coordinator.instance.resolve_accepted_coordinate(
                git_oid=at.git_oid,
                semantic_root=at.semantic_root,
                generation_root=at.generation_root,
                compiler_digest=at.compiler_digest,
            )
            source_ids = (
                (payload.source.source_id,)
                if isinstance(payload.source, WorkingSelectionObservationV1)
                else ()
            )
            lint = lint_claim_type_input(
                coordinator.instance,
                claim_type,
                coordinate=coordinate,
                anticipated_source_ids=source_ids,
            )
            if lint.warnings:
                values["lint"] = lint.model_dump(mode="json")
    return contracts.PlaybillAuthoringPreflightResult.model_validate(values)


def playbill_authoring_compile(
    instance_id: str,
    *,
    payload: AuthoringPayloadV1,
    intent_id: str | None = None,
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...] | None = None,
    program_stamp: AuthoringProgramStampV1 | None = None,
) -> contracts.PlaybillAuthoringPreflightResult:
    check_permission("cruxible_playbill_authoring_compile", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.compile(
        actor=actor,
        payload=payload,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
        reference_expectations=reference_expectations,
        program_stamp=program_stamp,
    )
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_compile_input(
    instance_id: str,
    *,
    input: AuthoringInputV1,
    intent_id: str | None = None,
) -> contracts.PlaybillAuthoringPreflightResult:
    check_permission("cruxible_playbill_authoring_compile", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.compile_input(
        actor=actor,
        input=input,
        canonical_timestamp=canonical_candidate_timestamp(utc_now()),
        intent_id=intent_id,
    )
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_preflight(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringPreflightResult:
    check_permission("cruxible_playbill_authoring_preflight", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.preflight(intent_id, actor=actor)
    return _authoring_preflight_result(coordinator, actor=actor, result=result)


def playbill_authoring_rebase(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringIntentView:
    check_permission("cruxible_playbill_authoring_preflight", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.rebase(intent_id, actor=actor)
    return contracts.PlaybillAuthoringIntentView.model_validate(result.model_dump(mode="json"))


def playbill_authoring_submit(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillAuthoringSubmitResult:
    check_permission("cruxible_playbill_authoring_submit", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.submit(intent_id, actor=actor)
    return contracts.PlaybillAuthoringSubmitResult.model_validate(result.model_dump(mode="json"))


def playbill_authoring_status(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillCandidateStatus:
    check_permission("cruxible_playbill_authoring_status", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.status(intent_id, actor=actor)
    return contracts.PlaybillCandidateStatus.model_validate(result.model_dump(mode="json"))


def playbill_authoring_confirm_insertion(
    instance_id: str,
    intent_id: str,
    *,
    observation: InsertionConfirmationObservationV2,
) -> contracts.PlaybillInsertionConfirmResultV2:
    check_permission("cruxible_playbill_authoring_confirm_insertion", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.confirm_insertion(intent_id, actor=actor, observation=observation)
    return contracts.PlaybillInsertionConfirmResultV2.model_validate(result.model_dump(mode="json"))


def playbill_authoring_prepare_publication(
    instance_id: str,
    intent_id: str,
    *,
    observation: PublicationSourceObservationV2,
) -> contracts.PlaybillInsertionPrepareResult:
    check_permission("cruxible_playbill_authoring_prepare_publication", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.prepare_publication(intent_id, actor=actor, observation=observation)
    return contracts.PlaybillInsertionPrepareResult.model_validate(result.model_dump(mode="json"))


def playbill_authoring_abandon_insertion(
    instance_id: str,
    intent_id: str,
) -> contracts.PlaybillInsertionAbandonResult:
    check_permission("cruxible_playbill_authoring_abandon_insertion", instance_id=instance_id)
    coordinator, actor = _authoring_coordinator(instance_id)
    result = coordinator.abandon_insertion(intent_id, actor=actor)
    return contracts.PlaybillInsertionAbandonResult.model_validate(result.model_dump(mode="json"))


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
    evaluation_time: datetime | None = None,
) -> contracts.PlaybillClaimViewV2:
    check_permission("cruxible_playbill_read", instance_id=instance_id)
    result = service_get_playbill_claim(
        get_playbill_manager().get(instance_id),
        identity=identity,
        at=at,
        evaluation_time=_evaluation_time(evaluation_time),
    )
    path = result.envelope.get("path")
    if isinstance(path, str):
        _record_consumed_paths(
            instance_id,
            operation="playbill.claim.get",
            coordinate=result.coordinate,
            paths=(path,),
        )
    return contracts.PlaybillClaimViewV2.model_validate(result.model_dump(mode="json"))


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
) -> contracts.PlaybillClaimExplanationV2 | contracts.PlaybillClaimExplanationV3:
    check_permission("cruxible_playbill_explain", instance_id=instance_id)
    result = service_explain_playbill_claim(
        get_playbill_manager().get(instance_id),
        identity=identity,
        at=at,
        evaluation_time=_evaluation_time(evaluation_time),
    )
    payload = result.model_dump(mode="json")
    if payload.get("tag") == "playbill-claim-explanation-v3":
        return contracts.PlaybillClaimExplanationV3.model_validate(payload)
    return contracts.PlaybillClaimExplanationV2.model_validate(payload)


def playbill_propose_query_definition(
    instance_id: str,
    *,
    query: QueryDefinitionV1,
    proposal_name: str,
    base: AcceptedCoordinate | None = None,
) -> contracts.PlaybillProposalInspection:
    check_permission("cruxible_playbill_propose", instance_id=instance_id)
    result = _proposal_validation_boundary(
        "query definition",
        lambda: service_propose_playbill_query_definition(
            get_playbill_manager().get(instance_id),
            query=query,
            actor_id=_actor_id(),
            proposal_name=proposal_name,
            timestamp=canonical_candidate_timestamp(utc_now()),
            base=base,
        ),
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
    _record_consumed_paths(
        instance_id,
        operation="playbill.query_definition.get",
        coordinate=result.coordinate,
        paths=(result.path,),
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
    _record_consumed_paths(
        instance_id,
        operation="playbill.query.run",
        coordinate=result.coordinate,
        paths=(result.definition_path,),
    )
    return contracts.PlaybillQueryRun.model_validate(result.model_dump(mode="json"))


def playbill_procedure_readiness(
    instance_id: str,
    name: str,
    *,
    request: ProcedureReadinessRequestV1,
) -> contracts.PlaybillProcedureReadiness:
    check_permission("cruxible_playbill_procedure_readiness", instance_id=instance_id)
    result = service_playbill_procedure_readiness(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
    )
    return contracts.PlaybillProcedureReadiness.model_validate(result.model_dump(mode="json"))


def playbill_procedure_bind(
    instance_id: str,
    name: str,
    *,
    request: ProcedureBindRequestV1,
) -> contracts.PlaybillProcedureBindResult:
    check_permission("cruxible_playbill_procedure_bind", instance_id=instance_id)
    result = service_bind_playbill_procedure(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
        actor=AuthenticatedActor(actor_id=_actor_id()),
        timestamp=canonical_candidate_timestamp(utc_now()),
    )
    return contracts.PlaybillProcedureBindResult.model_validate(result.model_dump(mode="json"))


def playbill_procedure_run(
    instance_id: str,
    name: str,
    *,
    request: ProcedureRunRequestV1,
) -> contracts.PlaybillProcedureRunState:
    check_permission("cruxible_playbill_procedure_run", instance_id=instance_id)
    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Procedure run requires an authenticated actor identity")
    result = service_run_playbill_procedure(
        get_playbill_manager().get(instance_id),
        name=name,
        request=request,
        actor_context=actor,
    )
    instance = get_playbill_manager().get(instance_id)
    record_consumption(
        instance,
        context=ConsumptionContextV1(
            actor_context=actor,
            access_profile_id=coverage_access_profile().profile_id,
        ),
        operation="playbill.procedure.run.resolve",
        coordinate=result.coordinate,
        artifacts=consumption_artifacts_for_dependency_closure(
            instance.tree_at(result.coordinate.git_oid),
            procedure_path(name),
        ),
    )
    return contracts.PlaybillProcedureRunState.model_validate(result.model_dump(mode="json"))


def playbill_procedure_run_status(
    instance_id: str,
    run_id: str,
) -> contracts.PlaybillProcedureRunState:
    check_permission("cruxible_playbill_procedure_run_status", instance_id=instance_id)
    result = service_get_playbill_procedure_run(
        get_playbill_manager().get(instance_id),
        run_id=run_id,
    )
    return contracts.PlaybillProcedureRunState.model_validate(result.model_dump(mode="json"))


def playbill_next(
    instance_id: str,
    *,
    request: PlaybillNextRequestV1 | Mapping[str, object],
) -> contracts.PlaybillNextResult:
    check_permission("cruxible_playbill_next", instance_id=instance_id)
    result = service_playbill_next(
        get_playbill_manager().get(instance_id),
        request=validate_playbill_next_request(request),
    )
    return contracts.PlaybillNextResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_list(
    instance_id: str,
    *,
    request: PlaybillCurationListRequestV1 | Mapping[str, object],
) -> contracts.PlaybillCurationListResult:
    check_permission("cruxible_playbill_curation_list", instance_id=instance_id)
    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Playbill curation reads require an attributed actor")
    parsed = validate_playbill_curation_list_request(request)
    result = service_list_playbill_curation(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=actor,
    )
    return contracts.PlaybillCurationListResult.model_validate(result.model_dump(mode="json"))


def playbill_audit(
    instance_id: str,
    *,
    request: PlaybillAuditRequestV1 | Mapping[str, object],
) -> contracts.PlaybillAuditResult:
    check_permission("cruxible_playbill_audit", instance_id=instance_id)
    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Playbill audit reads require an attributed actor")
    parsed = validate_playbill_audit_request(request)
    result = service_playbill_audit(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=actor,
    )
    return contracts.PlaybillAuditResult.model_validate(result.model_dump(mode="json"))


def _curation_actor() -> GovernedActorContext:
    actor = _actor_context()
    if actor is None:
        raise AuthenticationError("Playbill curation actions require an attributed actor")
    return actor


def playbill_curation_overrule(
    instance_id: str,
    *,
    request: PlaybillCurationOverruleRequestV1 | Mapping[str, object],
) -> contracts.PlaybillCurationActionResult:
    check_permission("cruxible_playbill_curation_overrule", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationOverruleRequestV1)
            else PlaybillCurationOverruleRequestV1.model_validate(request)
        )
    )
    result = service_overrule_playbill_curation(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=_curation_actor(),
    )
    return contracts.PlaybillCurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_accept_fixed(
    instance_id: str,
    *,
    request: PlaybillCurationAcceptFixedRequestV1 | Mapping[str, object],
) -> contracts.PlaybillCurationActionResult:
    check_permission("cruxible_playbill_curation_accept_fixed", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationAcceptFixedRequestV1)
            else PlaybillCurationAcceptFixedRequestV1.model_validate(request)
        )
    )
    result = service_accept_fixed_playbill_curation(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=_curation_actor(),
    )
    return contracts.PlaybillCurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_curation_suppress(
    instance_id: str,
    *,
    request: PlaybillCurationSuppressRequestV1 | Mapping[str, object],
) -> contracts.PlaybillCurationActionResult:
    check_permission("cruxible_playbill_curation_suppress", instance_id=instance_id)
    parsed = _curation_validation_boundary(
        lambda: (
            request
            if isinstance(request, PlaybillCurationSuppressRequestV1)
            else PlaybillCurationSuppressRequestV1.model_validate(request)
        )
    )
    result = service_suppress_playbill_curation(
        get_playbill_manager().get(instance_id),
        request=parsed,
        actor_context=_curation_actor(),
    )
    return contracts.PlaybillCurationActionResult.model_validate(result.model_dump(mode="json"))


def playbill_since(
    instance_id: str,
    *,
    request: contracts.PlaybillSinceRequest | Mapping[str, object],
) -> contracts.PlaybillSinceResult:
    check_permission("cruxible_playbill_since", instance_id=instance_id)
    parsed = validate_playbill_since_request(request)
    return service_playbill_since(get_playbill_manager().get(instance_id), request=parsed)


def playbill_discover(
    instance_id: str,
    *,
    query: str | None = None,
    entrypoint: str | None = None,
    at: AcceptedCoordinate | None = None,
    evaluation_time: str | None = None,
    profile: Literal["interfaces", "subjects", "all"] = "interfaces",
    budget: DiscoveryBudgetV1 | None = None,
) -> contracts.PlaybillDiscoveryResult | contracts.PlaybillInterfaceInventory:
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
    if isinstance(result, PlaybillDiscoveryResultV1):
        _record_consumed_paths(
            instance_id,
            operation="playbill.discover.match",
            coordinate=result.coordinate,
            paths=tuple(hit.address.artifact_path for hit in result.page.hits),
        )
    payload = result.model_dump(mode="json")
    if payload.get("tag") == "playbill-interface-inventory-v1":
        return contracts.PlaybillInterfaceInventory.model_validate(payload)
    return contracts.PlaybillDiscoveryResult.model_validate(payload)


def playbill_search(
    instance_id: str,
    *,
    mode: SearchMode,
    query: str | None = None,
    kinds: tuple[SearchKind, ...] = SEARCH_KINDS,
    subject: SemanticAddress | None = None,
    statuses: tuple[SearchStatus, ...] = (),
    cursor: PlaybillSearchCursorV1 | None = None,
    at: AcceptedCoordinate | None = None,
    evaluation_time: datetime | None = None,
    budgets: PlaybillSearchBudgetsV1 | None = None,
) -> contracts.PlaybillSearchResult:
    check_permission("cruxible_playbill_search", instance_id=instance_id)
    instance = get_playbill_manager().get(instance_id)
    result = service_search_playbill(
        instance,
        request=PlaybillSearchRequestV1(
            mode=mode,
            accepted_coordinate=_accepted_coordinate(instance_id, at),
            evaluation_time=_evaluation_time(evaluation_time),
            access_profile=coverage_access_profile(),
            # An empty kind filter reads as "no kind restriction", exactly as the
            # empty `statuses` filter beside it already does. The frozen request
            # model keeps its nonempty invariant, so the selection-basis digest
            # still commits to the concrete kinds searched; only the caller's
            # shorthand is expanded here.
            kinds=kinds or SEARCH_KINDS,
            query=query,
            subject=subject,
            statuses=statuses,
            cursor=cursor,
            budgets=budgets
            or (cursor.budgets if cursor is not None else PlaybillSearchBudgetsV1()),
        ),
    )
    if result.mode == "search":
        _record_consumed_paths(
            instance_id,
            operation="playbill.search.match",
            coordinate=result.coordinate,
            paths=tuple(row.address.artifact_path for row in result.rows),
        )
    return contracts.PlaybillSearchResult.model_validate(result.model_dump(mode="json"))


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
    if isinstance(result.at, AcceptedCoordinate):
        _record_consumed_paths(
            instance_id,
            operation="playbill.expand",
            coordinate=result.at,
            paths=(address.artifact_path,),
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
    """Resolve one batch of working-set observations into the current coverage result.

    The one vendor-neutral coverage operation of §11.7. Every request form it
    has to serve -- a file read with a line/range selection, a grep result
    batch, a set of changed filesystem paths, an explicit source occurrence, and
    a working-set scope -- arrives here already reduced by the adapter to
    observations and the spans they carry, because an adapter contains no
    semantic logic and this operation reads no filesystem.

    A successful outer call appends local, idempotent per-artifact consumption
    receipts.  They add no authority and enter no accepted-state or answer
    digest; they only account for which accepted artifacts this service
    actually delivered.

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
    claim_paths = tuple(
        address.artifact_path
        for span in result.spans
        for card in span.cards
        for address in card.claim_addresses
    )
    _record_consumed_paths(
        instance_id,
        operation="playbill.coverage.resolve",
        coordinate=result.at,
        paths=claim_paths,
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
