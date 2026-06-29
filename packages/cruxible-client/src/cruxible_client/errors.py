"""Client-side error hierarchy and HTTP error decoding."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_MAX_DISPLAY_ERRORS = 10


class CoreError(Exception):
    """Base exception for all Cruxible client errors."""

    def __init__(self, message: str, *, mutation_receipt_id: str | None = None) -> None:
        self.mutation_receipt_id = mutation_receipt_id
        super().__init__(message)

    def _receipt_suffix(self) -> str:
        if self.mutation_receipt_id:
            return f" (receipt: {self.mutation_receipt_id})"
        return ""

    def __str__(self) -> str:
        return super().__str__() + self._receipt_suffix()


class SchemaError(CoreError):
    """Base for errors in public schema/config definitions."""


class ConfigError(SchemaError):
    """Client-side config or validation error."""

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ) -> None:
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        shown = self.errors[:_MAX_DISPLAY_ERRORS]
        detail = "; ".join(shown)
        suffix = ""
        if len(self.errors) > _MAX_DISPLAY_ERRORS:
            suffix = f" ... and {len(self.errors) - _MAX_DISPLAY_ERRORS} more error(s)"
        return f"{self.summary}: {detail}{suffix}" + self._receipt_suffix()


class EntityTypeNotFoundError(SchemaError):
    def __init__(self, entity_type: str, *, known_entity_types: list[str] | None = None):
        self.entity_type = entity_type
        self.known_entity_types = sorted(known_entity_types or [])
        message = f"Entity type '{entity_type}' not found in schema"
        if self.known_entity_types:
            message += f". Known entity types: {', '.join(self.known_entity_types)}"
        super().__init__(message)


class RelationshipNotFoundError(SchemaError):
    def __init__(self, relationship_name: str):
        self.relationship_name = relationship_name
        super().__init__(f"Relationship '{relationship_name}' not found in schema")


class QueryNotFoundError(SchemaError):
    def __init__(self, query_name: str):
        self.query_name = query_name
        super().__init__(f"Named query '{query_name}' not found in schema")


class GraphError(CoreError):
    """Base for errors in graph data visible to the client."""


class EntityNotFoundError(GraphError):
    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} '{entity_id}' not found in graph")


class DataValidationError(GraphError):
    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ) -> None:
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        shown = self.errors[:_MAX_DISPLAY_ERRORS]
        detail = "; ".join(shown)
        suffix = ""
        if len(self.errors) > _MAX_DISPLAY_ERRORS:
            suffix = f" ... and {len(self.errors) - _MAX_DISPLAY_ERRORS} more error(s)"
        return f"{self.summary}: {detail}{suffix}" + self._receipt_suffix()


class RelationshipAmbiguityError(GraphError):
    def __init__(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ) -> None:
        self.from_type = from_type
        self.from_id = from_id
        self.to_type = to_type
        self.to_id = to_id
        self.relationship_type = relationship_type
        super().__init__(
            "Ambiguous edge target for "
            f"{from_type}:{from_id}:{relationship_type}:{to_type}:{to_id}; "
            "specify edge_key to target a single edge"
        )


class ConstraintViolationError(GraphError):
    def __init__(
        self,
        message: str,
        violations: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ) -> None:
        self.summary = message
        self.violations = violations or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.violations:
            return self.summary + self._receipt_suffix()
        detail = "; ".join(self.violations)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


class ExecutionError(CoreError):
    """Base for operation failures visible to the client."""


class IngestionError(ExecutionError):
    pass


class MutationError(ExecutionError):
    pass


class QueryExecutionError(ExecutionError):
    pass


class CustomerCodeExecutionUnsupportedError(ExecutionError):
    error_code = "customer_code_execution_unsupported"

    def __init__(self) -> None:
        super().__init__("Customer code execution is not supported in this hosted runtime profile.")


class OwnershipError(CoreError):
    def __init__(self, message: str, *, blocked_types: list[str] | None = None) -> None:
        self.blocked_types = blocked_types or []
        super().__init__(message)


class ReceiptNotFoundError(CoreError):
    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"Receipt '{receipt_id}' not found")


class TraceNotFoundError(CoreError):
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        super().__init__(f"Trace '{trace_id}' not found")


class OutcomeNotFoundError(CoreError):
    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"No outcome found for receipt '{receipt_id}'")


class InstanceNotFoundError(CoreError):
    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        super().__init__(f"Instance '{instance_id}' not found")


class GroupNotFoundError(CoreError):
    def __init__(self, group_id: str):
        self.group_id = group_id
        super().__init__(f"Group '{group_id}' not found")


class RuntimeCredentialNotFoundError(CoreError):
    def __init__(self, credential_id: str):
        self.credential_id = credential_id
        super().__init__(f"Runtime credential '{credential_id}' not found")


class ServerUnreachableError(CoreError):
    """The Cruxible daemon could not be reached over the transport.

    Wraps httpx transport-level failures (connection refused, timeout, DNS)
    so callers get a friendly single-line message naming the target instead
    of a raw httpx traceback.
    """

    def __init__(self, target: str, reason: str) -> None:
        self.target = target
        self.reason = reason
        super().__init__(f"could not reach Cruxible server at {target}: {reason}")


class AuthenticationError(CoreError):
    pass


class InstanceScopeError(CoreError):
    def __init__(self, instance_id: str, credential_scope: str):
        self.instance_id = instance_id
        self.credential_scope = credential_scope
        super().__init__(
            f"Credential scoped to instance '{credential_scope}' cannot access "
            f"instance '{instance_id}'"
        )


class PermissionDeniedError(CoreError):
    def __init__(self, tool_name: str, current_mode: str, required_mode: str):
        self.tool_name = tool_name
        self.current_mode = current_mode
        self.required_mode = required_mode
        super().__init__(
            f"Tool '{tool_name}' requires {required_mode} mode, "
            f"but server is running in {current_mode} mode"
        )


class DirectWriteRefusedError(CoreError):
    """Direct graph write refused because the target is governed proposal_only."""

    error_code = "direct_write_refused"

    def __init__(self, kind: str, type_name: str, source: str, message: str | None = None):
        self.kind = kind
        self.type_name = type_name
        self.source = source
        super().__init__(
            message
            or (f"Direct write to {kind} '{type_name}' is refused (write_policy=proposal_only).")
        )


class ErrorResponse(BaseModel):
    """Structured error payload returned by the HTTP server."""

    error_type: str
    message: str
    error_code: str | None = None
    errors: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    mutation_receipt_id: str | None = None


def response_to_error(_status: int, body: ErrorResponse) -> CoreError:
    """Reconstruct a client-side error from an HTTP error response."""
    context = body.context

    if body.error_type == "ConfigError":
        exc: CoreError = ConfigError(body.message, errors=body.errors)
    elif body.error_type == "DataValidationError":
        exc = DataValidationError(body.message, errors=body.errors)
    elif body.error_type == "RequestValidationError":
        # Server-side FastAPI request validation; field-level details ride in
        # errors just like data validation failures.
        exc = DataValidationError(body.message, errors=body.errors)
    elif body.error_type == "ConstraintViolationError":
        exc = ConstraintViolationError(body.message, violations=context.get("violations", []))
    elif body.error_type == "OwnershipError":
        exc = OwnershipError(body.message, blocked_types=context.get("blocked_types", []))
    elif body.error_type == "PermissionDeniedError":
        exc = PermissionDeniedError(
            context.get("tool_name", "unknown"),
            context.get("current_mode", "unknown"),
            context.get("required_mode", "unknown"),
        )
    elif body.error_type == "DirectWriteRefusedError":
        exc = DirectWriteRefusedError(
            context.get("kind", "unknown"),
            context.get("type_name", "unknown"),
            context.get("source", "unknown"),
            message=body.message,
        )
    elif body.error_type == "EntityTypeNotFoundError":
        exc = EntityTypeNotFoundError(
            context.get("entity_type", body.message),
            known_entity_types=context.get("known_entity_types", []),
        )
    elif body.error_type == "RelationshipNotFoundError":
        exc = RelationshipNotFoundError(context.get("relationship_name", body.message))
    elif body.error_type == "QueryNotFoundError":
        exc = QueryNotFoundError(context.get("query_name", body.message))
    elif body.error_type == "EntityNotFoundError":
        exc = EntityNotFoundError(
            context.get("entity_type", "unknown"),
            context.get("entity_id", "unknown"),
        )
    elif body.error_type == "RelationshipAmbiguityError":
        exc = RelationshipAmbiguityError(
            from_type=context.get("from_type", "unknown"),
            from_id=context.get("from_id", "unknown"),
            to_type=context.get("to_type", "unknown"),
            to_id=context.get("to_id", "unknown"),
            relationship_type=context.get("relationship_type", "unknown"),
        )
    elif body.error_type == "ReceiptNotFoundError":
        exc = ReceiptNotFoundError(context.get("receipt_id", "unknown"))
    elif body.error_type == "TraceNotFoundError":
        exc = TraceNotFoundError(context.get("trace_id", "unknown"))
    elif body.error_type == "OutcomeNotFoundError":
        exc = OutcomeNotFoundError(context.get("receipt_id", "unknown"))
    elif body.error_type == "InstanceNotFoundError":
        exc = InstanceNotFoundError(context.get("instance_id", "unknown"))
    elif body.error_type == "GroupNotFoundError":
        exc = GroupNotFoundError(context.get("group_id", "unknown"))
    elif body.error_type == "RuntimeCredentialNotFoundError":
        exc = RuntimeCredentialNotFoundError(context.get("credential_id", "unknown"))
    elif body.error_type == "AuthenticationError":
        exc = AuthenticationError(body.message)
    elif body.error_type == "InstanceScopeError":
        exc = InstanceScopeError(
            context.get("instance_id", "unknown"),
            context.get("credential_scope", "unknown"),
        )
    elif body.error_type == "QueryExecutionError":
        exc = QueryExecutionError(body.message)
    elif body.error_type == "CustomerCodeExecutionUnsupportedError":
        exc = CustomerCodeExecutionUnsupportedError()
    elif body.error_type == "IngestionError":
        exc = IngestionError(body.message)
    elif body.error_type == "MutationError":
        exc = MutationError(body.message)
    else:
        exc = CoreError(body.message)

    exc.mutation_receipt_id = body.mutation_receipt_id
    return exc
