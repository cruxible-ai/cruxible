"""Serialize server-side CoreError instances across the HTTP boundary."""

from __future__ import annotations

from typing import Any

from cruxible_client.errors import ErrorResponse, response_to_error
from cruxible_core.errors import (
    AuthenticationError,
    ConfigError,
    ConstraintViolationError,
    CoreError,
    DataValidationError,
    EntityNotFoundError,
    EntityTypeNotFoundError,
    GroupNotFoundError,
    IngestionError,
    InstanceNotFoundError,
    InstanceScopeError,
    MutationError,
    OutcomeNotFoundError,
    OwnershipError,
    PermissionDeniedError,
    QueryExecutionError,
    QueryNotFoundError,
    ReceiptNotFoundError,
    RelationshipAmbiguityError,
    RelationshipNotFoundError,
    TraceNotFoundError,
)

__all__ = ["ErrorResponse", "error_to_response", "response_to_error"]


def _message_for_error(exc: CoreError) -> str:
    if exc.args:
        return str(exc.args[0])
    return exc.__class__.__name__


def _status_for_error(exc: CoreError) -> int:
    if isinstance(exc, AuthenticationError):
        return 401
    if isinstance(exc, (ConfigError, DataValidationError, QueryExecutionError, IngestionError)):
        return 400
    if isinstance(exc, (PermissionDeniedError, OwnershipError, InstanceScopeError)):
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
        ),
    ):
        return 404
    if isinstance(exc, RelationshipAmbiguityError):
        return 409
    if isinstance(exc, ConstraintViolationError):
        return 422
    if isinstance(exc, MutationError):
        return 500
    return 500


def error_to_response(exc: CoreError) -> tuple[int, ErrorResponse]:
    """Convert a CoreError into an HTTP status code and structured payload."""
    context: dict[str, Any] = {}
    errors: list[str] = []

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
    if isinstance(exc, EntityTypeNotFoundError):
        context["entity_type"] = exc.entity_type
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
        context["relationship"] = exc.relationship_type
    if isinstance(exc, ReceiptNotFoundError | OutcomeNotFoundError):
        context["receipt_id"] = exc.receipt_id
    if isinstance(exc, TraceNotFoundError):
        context["trace_id"] = exc.trace_id
    if isinstance(exc, InstanceNotFoundError):
        context["instance_id"] = exc.instance_id
    if isinstance(exc, InstanceScopeError):
        context["instance_id"] = exc.instance_id
        context["credential_scope"] = exc.credential_scope
    if isinstance(exc, GroupNotFoundError):
        context["group_id"] = exc.group_id

    body = ErrorResponse(
        error_type=exc.__class__.__name__,
        message=_message_for_error(exc),
        errors=errors,
        context=context,
        mutation_receipt_id=exc.mutation_receipt_id,
    )
    return _status_for_error(exc), body
