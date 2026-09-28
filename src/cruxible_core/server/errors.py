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
    PlaybillInstanceDecommissioned,
    PlaybillObjectFormatConflict,
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
from cruxible_core.authoring.insertions import InsertionProtocolError
from cruxible_core.curation.review_operational import (
    ReviewOperationalConcurrentChangeError,
    ReviewOperationalStoreError,
)
from cruxible_core.derived.derived_runtime import BuildCapacityError
from cruxible_core.errors import (
    AuthenticationError,
    BootstrapClaimRefusedError,
    ConfigError,
    CoreError,
    CustomerCodeExecutionUnsupportedError,
    DaemonOperationScopeError,
    DataValidationError,
    HostedProfileUnknownError,
    InstanceNotFoundError,
    InstanceScopeError,
    PermissionDeniedError,
    RuntimeCredentialNotFoundError,
)
from cruxible_core.evidence.claim_attestation_store import ClaimAttestationStoreError
from cruxible_core.service.discovery.audit import PlaybillAuditError
from cruxible_core.service.discovery.curation import PlaybillCurationError
from cruxible_core.service.discovery.next import PlaybillNextError
from cruxible_core.service.discovery.since import PlaybillSinceError
from cruxible_core.service.procedures.procedure_runs import ProcedureSurfaceError
from cruxible_core.service.refusals import (
    ALL_SERVED_REFUSAL_CODES,
    repair_for_refusal,
)

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
    "cruxible_server_stop": "server stop",
}


CREDENTIAL_REPAIR_OPERATION = "credential.mint"

# A refused bootstrap claim is repaired either by claiming again with the right
# secret (or after a race) or by recovering ADMIN from local state, because the
# instance is already bootstrapped.
_BOOTSTRAP_REPAIR_OPERATIONS = {
    "runtime_bootstrap.secret_invalid": "credential.claim-bootstrap",
    "runtime_bootstrap.secret_already_claimed": "credential.recover-admin",
    "runtime_bootstrap.admin_exists": "credential.recover-admin",
    "runtime_bootstrap.claim_conflict": "credential.claim-bootstrap",
}


def _message_for_error(exc: CoreError) -> str:
    if isinstance(exc, DaemonOperationScopeError):
        operation = _DAEMON_OPERATION_LABELS.get(exc.operation, exc.operation)
        return (
            f"The bearer token is instance-scoped; `{operation}` is a daemon-scope "
            "operation. Use the operator credential (the bootstrap secret or a "
            "daemon-scope token) in CRUXIBLE_SERVER_BEARER_TOKEN. The daemon's "
            "runtime bootstrap secret keeps authorizing daemon-scope operations "
            "after `credential claim-bootstrap` has consumed its one-time claim."
        )
    if exc.args:
        return str(exc.args[0])
    return exc.__class__.__name__


def _repair_for_error(exc: CoreError) -> ServedRepairV1:
    carried = getattr(exc, "repair", None)
    if isinstance(carried, RepairOperationV1):
        return carried
    # Both credential refusals are repaired by minting the credential the
    # operation requires, which is one served CLI leaf; the refused operation
    # and the accepted credential kinds travel as its arguments.
    if isinstance(exc, DaemonOperationScopeError):
        return RepairOperationV1(
            operation=CREDENTIAL_REPAIR_OPERATION,
            arguments={
                "refused_operation": exc.operation,
                "credential_env": "CRUXIBLE_SERVER_BEARER_TOKEN",
                "accepted_credentials": ["bootstrap secret", "daemon-scope token"],
            },
        )
    if isinstance(exc, BootstrapClaimRefusedError):
        return RepairOperationV1(
            operation=_BOOTSTRAP_REPAIR_OPERATIONS[exc.error_code],
            arguments={"instance_id": exc.instance_id},
        )
    if isinstance(exc, AuthenticationError):
        return RepairOperationV1(
            operation=CREDENTIAL_REPAIR_OPERATION,
            arguments={
                "credential_options": [
                    "--server-bearer-token",
                    "CRUXIBLE_SERVER_BEARER_TOKEN",
                    "bootstrap-secret file",
                ]
            },
        )
    # A refusal whose code is a registered member of a closed served vocabulary
    # renders the repair the catalog declares for it, so the wire carries the
    # same runnable operation the refusal detail would have carried.
    error_code = getattr(exc, "error_code", None)
    if isinstance(error_code, str) and error_code in ALL_SERVED_REFUSAL_CODES:
        return repair_for_refusal(error_code)
    code = error_code or getattr(exc, "code", None)
    return hand_edit_repair(str(code or exc.__class__.__name__))


def _status_for_error(exc: CoreError) -> int:
    if isinstance(exc, BuildCapacityError):
        return 503
    if isinstance(exc, ProcedureSurfaceError):
        # A served Procedure/Line surface refusal is a request fault the caller
        # can repair, never a daemon fault: the class declares its own 4xx so
        # the code and the repair the envelope carries are actionable.
        return exc.http_status
    if isinstance(exc, AuthenticationError):
        return 401
    if isinstance(exc, (CustomerCodeExecutionUnsupportedError, HostedProfileUnknownError)):
        # A misconfigured profile is still a refusal to execute, not a request
        # fault and not a crash: the caller must see the same 403 shape.
        return 403
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
        return 400
    if isinstance(exc, (PermissionDeniedError, InstanceScopeError)):
        return 403
    if isinstance(
        exc,
        (
            InstanceNotFoundError,
            RuntimeCredentialNotFoundError,
            DocumentNotFoundError,
            ProposalNotFoundError,
        ),
    ):
        return 404
    if isinstance(
        exc,
        (
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
        return 409
    return 500


def error_to_response(exc: CoreError) -> tuple[int, ErrorResponse]:
    """Convert a CoreError into an HTTP status code and structured payload."""
    context: dict[str, Any] = {}
    if isinstance(exc, BuildCapacityError):
        context["retryable"] = True
    errors: list[str] = []
    error_code = getattr(exc, "error_code", None)
    if error_code is None and isinstance(exc, InsertionProtocolError):
        error_code = exc.code

    if isinstance(exc, ConfigError | DataValidationError):
        errors = list(exc.errors)
    if isinstance(exc, PermissionDeniedError):
        context["tool_name"] = exc.tool_name
        context["current_mode"] = exc.current_mode
        context["required_mode"] = exc.required_mode
        if exc.ceiling_mode is not None:
            context["ceiling_mode"] = exc.ceiling_mode
    if isinstance(exc, HostedProfileUnknownError):
        context["profile"] = exc.profile
    if isinstance(exc, CustomerCodeExecutionUnsupportedError) and exc.detail is not None:
        context["detail"] = exc.detail
    if isinstance(exc, PlaybillObjectFormatConflict):
        if exc.workspace_format is not None:
            context["workspace_format"] = exc.workspace_format
    if isinstance(exc, PlaybillInstanceDecommissioned):
        context["instance_id"] = exc.instance_id
        context["reason"] = exc.reason
        context["decommissioned_at"] = exc.decommissioned_at
    if isinstance(exc, InstanceNotFoundError):
        context["instance_id"] = exc.instance_id
    if isinstance(exc, DaemonOperationScopeError):
        context["operation"] = exc.operation
        context["credential_scope"] = exc.credential_scope
    elif isinstance(exc, InstanceScopeError):
        context["instance_id"] = exc.instance_id
        context["credential_scope"] = exc.credential_scope
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

    body = ErrorResponse(
        error_type=exc.__class__.__name__,
        message=_message_for_error(exc),
        error_code=error_code if isinstance(error_code, str) else None,
        errors=errors,
        context=context,
        mutation_receipt_id=exc.mutation_receipt_id,
        repair=None if isinstance(exc, BuildCapacityError) else _repair_for_error(exc),
    )
    return _status_for_error(exc), body
