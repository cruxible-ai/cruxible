"""Error hierarchy for Cruxible Core.

All exceptions inherit from CoreError. Two intermediate base classes
separate config-level errors (schema definitions) from graph-level
errors (runtime data), making it easy to catch by category.

    CoreError
    ├── SchemaError (config definition problems)
    │   ├── ConfigError
    │   ├── EntityTypeNotFoundError
    │   ├── RelationshipNotFoundError
    │   └── QueryNotFoundError
    ├── GraphError (runtime data problems)
    │   ├── EntityNotFoundError
    │   ├── DataValidationError
    │   ├── RelationshipAmbiguityError
    │   └── ConstraintViolationError
    ├── ExecutionError (operation failures)
    │   ├── IngestionError
    │   ├── MutationError
    │   ├── QueryExecutionError
    │   ├── CustomerCodeExecutionUnsupportedError
    │   └── TransportError
    ├── OwnershipError (overlay type-level ownership)
    ├── ReceiptNotFoundError (receipt store lookup)
    ├── TraceNotFoundError (trace store lookup)
    ├── OutcomeNotFoundError (feedback store lookup)
    ├── InstanceNotFoundError (instance registry lookup)
    ├── GroupNotFoundError (group store lookup)
    ├── SourceArtifactNotFoundError (source artifact store lookup)
    ├── RuntimeCredentialNotFoundError (server credential store lookup)
    ├── AuthenticationError (HTTP/API credential failure)
    ├── InstanceScopeError (HTTP/API credential scope mismatch)
    ├── PermissionDeniedError (MCP permission mode)
    └── DirectWriteRefusedError (governed proposal_only direct-write refusal)
"""

from __future__ import annotations

from cruxible_client.errors import CoreError as _ClientCoreError
from cruxible_client.errors import (
    InvalidContinuationError as _ClientInvalidContinuationError,
)
from cruxible_client.errors import (
    StaleContinuationError as _ClientStaleContinuationError,
)


class CoreError(_ClientCoreError):
    """Base exception for all Cruxible Core errors.

    Inherits the client base so one `except cruxible_client.errors.CoreError`
    catches local and remote failures alike — no parallel hierarchies.
    """


# ---------------------------------------------------------------------------
# Schema errors — config definition is wrong or missing
# ---------------------------------------------------------------------------


class SchemaError(CoreError):
    """Base for errors in the config schema definition."""

    pass


_MAX_DISPLAY_ERRORS = 10


def _format_capped_errors(errors: list[str]) -> str:
    shown = errors[:_MAX_DISPLAY_ERRORS]
    detail = "; ".join(shown)
    if len(errors) > _MAX_DISPLAY_ERRORS:
        detail += f" ... and {len(errors) - _MAX_DISPLAY_ERRORS} more error(s)"
    return detail


class ConfigError(SchemaError):
    """Invalid configuration YAML.

    Raised when config fails schema validation or cross-reference checks.
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


class EntityTypeNotFoundError(SchemaError):
    """Entity type not defined in config schema."""

    def __init__(self, entity_type: str, *, known_entity_types: list[str] | None = None):
        self.entity_type = entity_type
        self.known_entity_types = sorted(known_entity_types or [])
        message = f"Entity type '{entity_type}' not found in schema"
        if self.known_entity_types:
            message += f". Known entity types: {', '.join(self.known_entity_types)}"
        super().__init__(message)


class RelationshipNotFoundError(SchemaError):
    """Relationship type not defined in config schema."""

    def __init__(self, relationship_name: str):
        self.relationship_name = relationship_name
        super().__init__(f"Relationship '{relationship_name}' not found in schema")


class QueryNotFoundError(SchemaError):
    """Named query not defined in config schema."""

    def __init__(self, query_name: str):
        self.query_name = query_name
        super().__init__(f"Named query '{query_name}' not found in schema")


# ---------------------------------------------------------------------------
# Graph errors — runtime data is wrong or missing
# ---------------------------------------------------------------------------


class GraphError(CoreError):
    """Base for errors in graph data at runtime."""

    pass


class EntityNotFoundError(GraphError):
    """Entity with given ID not found in the graph."""

    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} '{entity_id}' not found in graph")


class DataValidationError(GraphError):
    """Ingested data doesn't match config schema.

    Raised when CSV/JSON data doesn't conform to the entity/relationship
    property definitions in the config (wrong columns, bad types, etc.).
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


class RelationshipAmbiguityError(GraphError):
    """A relationship target is ambiguous and needs a stable edge key."""

    def __init__(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ):
        self.from_type = from_type
        self.from_id = from_id
        self.to_type = to_type
        self.to_id = to_id
        self.relationship_type = relationship_type
        super().__init__(
            "Ambiguous relationship target for "
            f"{from_type}:{from_id}:{relationship_type}:{to_type}:{to_id}; "
            "specify edge_key to target a single edge"
        )


class ConstraintViolationError(GraphError):
    """Constraint rule was violated."""

    def __init__(
        self,
        message: str,
        violations: list[str] | None = None,
        *,
        mutation_receipt_id: str | None = None,
    ):
        self.summary = message
        self.violations = violations or []
        super().__init__(message, mutation_receipt_id=mutation_receipt_id)

    def __str__(self) -> str:
        if not self.violations:
            return self.summary + self._receipt_suffix()
        detail = _format_capped_errors(self.violations)
        return f"{self.summary}: {detail}" + self._receipt_suffix()


# ---------------------------------------------------------------------------
# Execution errors — operation failures
# ---------------------------------------------------------------------------


class ExecutionError(CoreError):
    """Base for errors during operation execution."""

    pass


class IngestionError(ExecutionError):
    """Error during data ingestion.

    Raised when CSV parsing, column mapping, or data normalization fails.
    """

    pass


class MutationError(ExecutionError):
    """Unexpected failure during a graph mutation.

    Raised when durable writes (save_graph, store writes) fail for reasons
    other than data validation (OSError, sqlite3 errors, etc.).
    """

    pass


class QueryExecutionError(ExecutionError):
    """Error during query execution.

    Raised when query setup fails (missing parameters, no primary key,
    entry entity type not in config, etc.). The query exists in config
    but cannot be executed with the given inputs.
    """

    def __init__(self, message: str):
        super().__init__(message)


class CustomerCodeExecutionUnsupportedError(ExecutionError):
    """Customer code execution is unavailable in the current hosted runtime."""

    error_code = "customer_code_execution_unsupported"

    def __init__(self) -> None:
        super().__init__("Customer code execution is not supported in this hosted runtime profile.")


class TransportError(ExecutionError):
    """Error during state release transport operations."""

    pass


class OwnershipError(CoreError):
    """Write rejected because the target type is upstream-owned in a overlay instance."""

    def __init__(self, message: str, *, blocked_types: list[str] | None = None):
        self.blocked_types = blocked_types or []
        super().__init__(message)


# ---------------------------------------------------------------------------
# Store errors — persistence lookups
# ---------------------------------------------------------------------------


class ReceiptNotFoundError(CoreError):
    """Receipt ID not found in store."""

    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"Receipt '{receipt_id}' not found")


class TraceNotFoundError(CoreError):
    """Execution trace ID not found in store."""

    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        super().__init__(f"Trace '{trace_id}' not found")


class OutcomeNotFoundError(CoreError):
    """Outcome for a receipt was not found in the feedback store."""

    def __init__(self, receipt_id: str):
        self.receipt_id = receipt_id
        super().__init__(f"No outcome found for receipt '{receipt_id}'")


class InstanceNotFoundError(CoreError):
    """Cruxible instance not found."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        super().__init__(f"Instance '{instance_id}' not found")


class GroupNotFoundError(CoreError):
    """Group ID not found in store."""

    def __init__(self, group_id: str):
        self.group_id = group_id
        super().__init__(f"Group '{group_id}' not found")


class SourceArtifactNotFoundError(CoreError):
    """Source artifact ID not found in store."""

    def __init__(self, source_artifact_id: str):
        self.source_artifact_id = source_artifact_id
        super().__init__(f"Source artifact '{source_artifact_id}' not found")


class RuntimeCredentialNotFoundError(CoreError):
    """Runtime credential ID not found in the server credential store."""

    def __init__(self, credential_id: str):
        self.credential_id = credential_id
        super().__init__(f"Runtime credential '{credential_id}' not found")


class InvalidContinuationError(CoreError, _ClientInvalidContinuationError):
    """Continuation token is malformed or bound to a different read (422).

    Dual-inherits the client class so `except cruxible_client.errors.
    InvalidContinuationError` catches local raises and HTTP reconstructions
    alike; the app's CoreError handler serializes it across the wire.
    """


class StaleContinuationError(CoreError, _ClientStaleContinuationError):
    """Continuation token minted at a different read_revision or config (409).

    State moved between pages; the pagination window is no longer coherent
    and the caller must restart the read. Same dual-inheritance rationale as
    :class:`InvalidContinuationError`.
    """


class AuthenticationError(CoreError):
    """HTTP/API request is unauthenticated or uses an invalid credential."""

    pass


class InstanceScopeError(CoreError):
    """Runtime credential scope does not match the requested instance."""

    def __init__(self, instance_id: str, credential_scope: str):
        self.instance_id = instance_id
        self.credential_scope = credential_scope
        super().__init__(
            f"Credential scoped to instance '{credential_scope}' cannot access "
            f"instance '{instance_id}'"
        )


# ---------------------------------------------------------------------------
# Permission errors
# ---------------------------------------------------------------------------


class PermissionDeniedError(CoreError):
    """Operation denied due to insufficient effective permission mode."""

    def __init__(
        self,
        tool_name: str,
        current_mode: str,
        required_mode: str,
        *,
        ceiling_mode: str | None = None,
    ):
        self.tool_name = tool_name
        self.current_mode = current_mode
        self.required_mode = required_mode
        self.ceiling_mode = ceiling_mode
        if ceiling_mode is not None:
            super().__init__(
                f"Operation '{tool_name}' requires {required_mode} mode, but the daemon "
                f"capability ceiling is {ceiling_mode} mode "
                f"(effective request mode: {current_mode})"
            )
            return
        super().__init__(
            f"Tool '{tool_name}' requires {required_mode} mode, "
            f"but server is running in {current_mode} mode"
        )


class DirectWriteRefusedError(CoreError):
    """Direct graph write refused because the target policy disallows the source.

    A HARD governance constraint, independent of permission tier (even
    ``CRUXIBLE_MODE=admin`` is refused). State for a ``proposal_only`` type may
    only enter through the governed proposal/workflow path; relationship writes
    may also be staged with ``pending=true``. State for a ``mint_only`` type may
    only enter through runtime credential minting.
    """

    error_code = "direct_write_refused"

    def __init__(
        self,
        kind: str,
        type_name: str,
        source: str,
        *,
        policy: str = "proposal_only",
    ):
        self.kind = kind
        self.type_name = type_name
        self.source = source
        self.policy = policy
        if policy == "mint_only":
            super().__init__(
                f"Direct write to {kind} '{type_name}' is refused "
                f"(write_policy=mint_only). This auth-managed type is writable "
                f"only via credential mint (`cruxible credential mint`)."
            )
            return
        if kind == "relationship":
            forward = (
                "Use 'group propose' to stage a governed proposal, or pass "
                "pending=true to stage the edge for review."
            )
        else:
            forward = (
                "Add it through a governed canonical workflow (apply_entities) "
                "instead of a direct write."
            )
        super().__init__(
            f"Direct write to {kind} '{type_name}' is refused "
            f"(write_policy=proposal_only). {forward}"
        )
