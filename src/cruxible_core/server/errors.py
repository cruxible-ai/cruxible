"""Serialize server-side CoreError instances across the HTTP boundary."""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.errors import (
    ApprovalIntegrityError,
    CanonicalEncodingError,
    DocumentFormatError,
    DocumentNotFoundError,
    PlaybillBootstrapError,
    PlaybillFormatError,
    PlaybillSinceRequestInvalid,
    PrincipalIntegrityError,
    ProjectionCoordinateError,
    ProposalAdmissionError,
    ProposalEvaluationIntegrityError,
    ProposalIntegrityError,
    ProposalNotFoundError,
    ProposalSelectorAmbiguousError,
    SettlementIntegrityError,
)
from cruxible_client.contracts.repairs import RepairOperationV1, ServedRepairV1, hand_edit_repair
from cruxible_client.errors import ErrorResponse, response_to_error
from cruxible_core.errors import (
    AuthenticationError,
    BindingNotFoundError,
    CitationHandleResolutionError,
    ConcurrentStateDriftError,
    ConfigError,
    ConstraintViolationError,
    CoreError,
    CustomerCodeExecutionUnsupportedError,
    DaemonOperationScopeError,
    DataValidationError,
    DirectWriteRefusedError,
    EntityNotFoundError,
    EntityTypeNotFoundError,
    GroupNotFoundError,
    IngestionError,
    InstallNotFoundError,
    InstallOwnershipCollisionError,
    InstallPhaseRequirementError,
    InstallPhaseTransitionError,
    InstanceNotFoundError,
    InstanceScopeError,
    InvalidContinuationError,
    MutationError,
    OutcomeNotFoundError,
    OwnershipError,
    PendingEdgeWriteRefusedError,
    PermissionDeniedError,
    ProcedureNotFoundError,
    ProcedureWithdrawalRefusedError,
    QueryExecutionError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    RelationshipAmbiguityError,
    RelationshipNotFoundError,
    RuntimeCredentialNotFoundError,
    SlotAlreadyBoundError,
    SlotBindingRefusedError,
    StaleContinuationError,
    TerminalLifecycleWriteRefusedError,
    TraceNotFoundError,
)
from cruxible_core.playbill.authoring.insertions import InsertionProtocolError
from cruxible_core.playbill.claim_attestation_store import ClaimAttestationStoreError
from cruxible_core.playbill.review_operational import (
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStoreError,
)
from cruxible_core.service.playbill_audit import PlaybillAuditError
from cruxible_core.service.playbill_curation import PlaybillCurationError
from cruxible_core.service.playbill_next import PlaybillNextError
from cruxible_core.service.playbill_since import PlaybillSinceError

STANDARD_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Bad request error envelope"},
    401: {"model": ErrorResponse, "description": "Authentication error envelope"},
    403: {"model": ErrorResponse, "description": "Permission error envelope"},
    404: {"model": ErrorResponse, "description": "Not found error envelope"},
    409: {"model": ErrorResponse, "description": "Conflict error envelope"},
    422: {"model": ErrorResponse, "description": "Validation error envelope"},
    500: {"model": ErrorResponse, "description": "Internal server error envelope"},
}

__all__ = [
    "ErrorResponse",
    "STANDARD_ERROR_RESPONSES",
    "error_to_response",
    "response_to_error",
]

_DAEMON_OPERATION_LABELS = {
    "cruxible_playbill_host_create": "playbill host create",
    "cruxible_server_info": "server status",
    "cruxible_server_restart": "server restart",
}


def _message_for_error(exc: CoreError) -> str:
    if isinstance(exc, DaemonOperationScopeError):
        operation = _DAEMON_OPERATION_LABELS.get(exc.operation, exc.operation)
        return (
            f"The bearer token is instance-scoped; `{operation}` is a daemon-scope "
            "operation. Use the operator credential (the bootstrap secret or a "
            "daemon-scope token) in CRUXIBLE_SERVER_BEARER_TOKEN."
        )
    if exc.args:
        return str(exc.args[0])
    return exc.__class__.__name__


def _repair_for_error(exc: CoreError) -> ServedRepairV1:
    if isinstance(exc, DaemonOperationScopeError):
        return RepairOperationV1(
            operation=exc.operation,
            arguments={
                "credential_env": "CRUXIBLE_SERVER_BEARER_TOKEN",
                "accepted_credentials": ["bootstrap secret", "daemon-scope token"],
            },
        )
    if isinstance(exc, AuthenticationError):
        return RepairOperationV1(
            operation="server.authenticate",
            arguments={
                "credential_options": [
                    "--server-bearer-token",
                    "CRUXIBLE_SERVER_BEARER_TOKEN",
                    "bootstrap-secret file",
                ]
            },
        )
    code = getattr(exc, "code", exc.__class__.__name__)
    return hand_edit_repair(str(code))


def _status_for_error(exc: CoreError) -> int:
    if isinstance(exc, AuthenticationError):
        return 401
    if isinstance(exc, CustomerCodeExecutionUnsupportedError):
        return 403
    if isinstance(exc, CitationHandleResolutionError):
        return 409 if exc.failure_kind == "stale" else 400
    if isinstance(exc, ClaimAttestationStoreError):
        if exc.error_code in {
            "playbill.claim_attestation.attestation_head_unknown",
            "playbill.claim_attestation.idempotency_payload_mismatch",
        }:
            return 400
        return 500
    if isinstance(exc, ProposalEvaluationIntegrityError):
        return 500
    if isinstance(
        exc,
        (
            ConfigError,
            DataValidationError,
            QueryExecutionError,
            IngestionError,
            SlotBindingRefusedError,
            CanonicalEncodingError,
            DocumentFormatError,
            PlaybillFormatError,
            PlaybillAuditError,
            PlaybillCurationError,
            PlaybillNextError,
            PlaybillSinceError,
            ReviewOperationalStoreError,
            ProposalAdmissionError,
            InsertionProtocolError,
        ),
    ):
        # A refused bind is a bad REQUEST, not a denied tier: the caller offered
        # a provider that does not fit the slot, and the fix is a different
        # provider (or recorded consent), never a higher permission mode.
        return 400
    if isinstance(
        exc,
        (
            PermissionDeniedError,
            OwnershipError,
            InstanceScopeError,
            DirectWriteRefusedError,
            TerminalLifecycleWriteRefusedError,
            ProcedureWithdrawalRefusedError,
        ),
    ):
        return 403
    if isinstance(
        exc,
        (
            EntityTypeNotFoundError,
            RelationshipNotFoundError,
            QueryNotFoundError,
            EntityNotFoundError,
            ReceiptNotFoundError,
            OutcomeNotFoundError,
            TraceNotFoundError,
            InstanceNotFoundError,
            GroupNotFoundError,
            ProcedureNotFoundError,
            RuntimeCredentialNotFoundError,
            InstallNotFoundError,
            BindingNotFoundError,
            DocumentNotFoundError,
            ProposalNotFoundError,
        ),
    ):
        return 404
    if isinstance(
        exc,
        (
            RelationshipAmbiguityError,
            StaleContinuationError,
            PendingEdgeWriteRefusedError,
            ConcurrentStateDriftError,
            # An install refusal is a STATE conflict, not a policy denial: the
            # same advance succeeds once the install reaches the phase it needs,
            # and the same name installs once its current owner releases it.
            InstallPhaseTransitionError,
            InstallPhaseRequirementError,
            InstallOwnershipCollisionError,
            SlotAlreadyBoundError,
            ApprovalIntegrityError,
            PlaybillBootstrapError,
            PrincipalIntegrityError,
            ProjectionCoordinateError,
            SettlementIntegrityError,
            ProposalIntegrityError,
            ReviewOperationalConcurrentChangeError,
            ProposalSelectorAmbiguousError,
        ),
    ):
        # 409: a pending-edge refusal is a STATE conflict, not a policy or tier
        # denial -- the same write succeeds once the proposal is resolved.
        return 409
    if isinstance(exc, (ConstraintViolationError, InvalidContinuationError)):
        return 422
    if isinstance(exc, MutationError):
        return 500
    return 500


def error_to_response(exc: CoreError) -> tuple[int, ErrorResponse]:
    """Convert a CoreError into an HTTP status code and structured payload."""
    context: dict[str, Any] = {}
    errors: list[str] = []
    error_code = getattr(exc, "error_code", None)
    if error_code is None and isinstance(exc, InsertionProtocolError):
        error_code = exc.code

    if isinstance(exc, ConfigError | DataValidationError):
        errors = list(exc.errors)
    if isinstance(exc, ConstraintViolationError):
        context["violations"] = list(exc.violations)
    if isinstance(exc, OwnershipError):
        context["blocked_types"] = exc.blocked_types
    if isinstance(exc, PermissionDeniedError):
        context["tool_name"] = exc.tool_name
        context["current_mode"] = exc.current_mode
        context["required_mode"] = exc.required_mode
        if exc.ceiling_mode is not None:
            context["ceiling_mode"] = exc.ceiling_mode
    if isinstance(exc, DirectWriteRefusedError):
        context["kind"] = exc.kind
        context["type_name"] = exc.type_name
        context["source"] = exc.source
    if isinstance(exc, TerminalLifecycleWriteRefusedError):
        context["kind"] = exc.kind
        context["status"] = exc.status
        context["writable"] = exc.writable
    if isinstance(exc, PendingEdgeWriteRefusedError):
        context["relationship_type"] = exc.relationship_type
        context["from_type"] = exc.from_type
        context["from_id"] = exc.from_id
        context["to_type"] = exc.to_type
        context["to_id"] = exc.to_id
    if isinstance(exc, EntityTypeNotFoundError):
        context["entity_type"] = exc.entity_type
        context["known_entity_types"] = exc.known_entity_types
    if isinstance(exc, RelationshipNotFoundError):
        context["relationship_name"] = exc.relationship_name
    if isinstance(exc, QueryNotFoundError):
        context["query_name"] = exc.query_name
    if isinstance(exc, EntityNotFoundError):
        context["entity_type"] = exc.entity_type
        context["entity_id"] = exc.entity_id
    if isinstance(exc, RelationshipAmbiguityError):
        context["from_type"] = exc.from_type
        context["from_id"] = exc.from_id
        context["to_type"] = exc.to_type
        context["to_id"] = exc.to_id
        context["relationship_type"] = exc.relationship_type
    if isinstance(exc, ReceiptNotFoundError | OutcomeNotFoundError):
        context["receipt_id"] = exc.receipt_id
    if isinstance(exc, TraceNotFoundError):
        context["trace_id"] = exc.trace_id
    if isinstance(exc, InstanceNotFoundError):
        context["instance_id"] = exc.instance_id
    if isinstance(exc, DaemonOperationScopeError):
        context["operation"] = exc.operation
        context["credential_scope"] = exc.credential_scope
    elif isinstance(exc, InstanceScopeError):
        context["instance_id"] = exc.instance_id
        context["credential_scope"] = exc.credential_scope
    if isinstance(exc, GroupNotFoundError):
        context["group_id"] = exc.group_id
    if isinstance(exc, ProcedureNotFoundError):
        context["procedure_id"] = exc.procedure_id
    if isinstance(exc, ProcedureWithdrawalRefusedError):
        context["procedure_id"] = exc.procedure_id
        context["current_mode"] = exc.current_mode
        context["required_mode"] = exc.required_mode
    if isinstance(exc, InstallNotFoundError):
        context["install_id"] = exc.install_id
    if isinstance(exc, InstallPhaseTransitionError):
        context["install_id"] = exc.install_id
        context["actual_phase"] = exc.actual_phase
        context["requested_phase"] = exc.requested_phase
        context["legal_phases"] = exc.legal_phases
    if isinstance(exc, InstallPhaseRequirementError):
        context["install_id"] = exc.install_id
        context["operation"] = exc.operation
        context["actual_phase"] = exc.actual_phase
        context["required_phases"] = exc.required_phases
    if isinstance(exc, InstallOwnershipCollisionError):
        context["object_kind"] = exc.object_kind
        context["object_name"] = exc.object_name
        context["owning_install_id"] = exc.owning_install_id
        context["owning_install_phase"] = exc.owning_install_phase
    if isinstance(exc, BindingNotFoundError):
        context["install_id"] = exc.install_id
        context["slot_name"] = exc.slot_name
        context["binding_id"] = exc.binding_id
    if isinstance(exc, SlotAlreadyBoundError):
        context["install_id"] = exc.install_id
        context["slot_name"] = exc.slot_name
        context["binding_id"] = exc.binding_id
        context["provider_name"] = exc.provider_name
    if isinstance(exc, SlotBindingRefusedError):
        context["install_id"] = getattr(exc, "install_id", None)
        context["slot_name"] = getattr(exc, "slot_name", None)
        near_matches = getattr(exc, "near_matches", None)
        if near_matches:
            # The ranked report is the whole value of the refusal; dropping it
            # at the HTTP boundary would leave the caller the message text and
            # nothing machine-readable to choose a provider from.
            context["near_matches"] = near_matches
        allowed = getattr(exc, "allowed_billing_modes", None)
        if allowed:
            context["allowed_billing_modes"] = allowed
    if isinstance(exc, CitationHandleResolutionError):
        context["handle"] = exc.handle
        context["failure_kind"] = exc.failure_kind
        context["detail"] = exc.detail
    if isinstance(exc, PlaybillSinceRequestInvalid):
        context["field_path"] = exc.field_path
    if isinstance(exc, RuntimeCredentialNotFoundError):
        context["credential_id"] = exc.credential_id
    if isinstance(exc, ProposalNotFoundError):
        context["selector"] = exc.selector
        context["accepted_forms"] = list(exc.accepted_forms)
        context["repair_commands"] = list(exc.repair_commands)
    if isinstance(exc, ProposalSelectorAmbiguousError):
        context["selector"] = exc.selector
        context["candidates"] = list(exc.candidates)
        context["repair_commands"] = list(exc.repair_commands)
    if isinstance(exc, InvalidContinuationError):
        context["reason"] = exc.reason
    if isinstance(exc, ConcurrentStateDriftError):
        context["opening_revision"] = exc.opening_revision
        context["closing_revision"] = exc.closing_revision
    if isinstance(exc, StaleContinuationError):
        context["reason"] = exc.reason
        context["token_read_revision"] = exc.token_read_revision
        context["current_read_revision"] = exc.current_read_revision

    body = ErrorResponse(
        error_type=exc.__class__.__name__,
        message=_message_for_error(exc),
        error_code=error_code if isinstance(error_code, str) else None,
        errors=errors,
        context=context,
        mutation_receipt_id=exc.mutation_receipt_id,
        repair=_repair_for_error(exc),
    )
    return _status_for_error(exc), body
