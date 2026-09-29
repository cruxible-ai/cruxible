"""Client-side error hierarchy and HTTP error decoding."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cruxible_client._error_base import CoreError as CoreError
from cruxible_client.contracts.errors import (
    PlaybillInstanceDecommissioned,
    PlaybillObjectFormatConflict,
    PlaybillSinceRequestInvalid,
    ProposalActivationRequestInvalid,
    ProposalNotFoundError,
    ProposalSelectorAmbiguousError,
    ReadRefusalError,
)
from cruxible_client.contracts.repairs import ServedRepairV1

_MAX_DISPLAY_ERRORS = 10


class ConfigError(CoreError):
    """Client-side config or validation error."""

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
    ) -> None:
        self.summary = message
        self.errors = errors or []
        super().__init__(message)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary
        shown = self.errors[:_MAX_DISPLAY_ERRORS]
        detail = "; ".join(shown)
        suffix = ""
        if len(self.errors) > _MAX_DISPLAY_ERRORS:
            suffix = f" ... and {len(self.errors) - _MAX_DISPLAY_ERRORS} more error(s)"
        return f"{self.summary}: {detail}{suffix}"


class DataValidationError(CoreError):
    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
    ) -> None:
        self.summary = message
        self.errors = errors or []
        super().__init__(message)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary
        shown = self.errors[:_MAX_DISPLAY_ERRORS]
        detail = "; ".join(shown)
        suffix = ""
        if len(self.errors) > _MAX_DISPLAY_ERRORS:
            suffix = f" ... and {len(self.errors) - _MAX_DISPLAY_ERRORS} more error(s)"
        return f"{self.summary}: {detail}{suffix}"


class CustomerCodeExecutionUnsupportedError(CoreError):
    error_code = "customer_code_execution_unsupported"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        message = "Customer code execution is not supported in this hosted runtime profile."
        super().__init__(message if detail is None else f"{message} ({detail})")


class HostedProfileUnknownError(CoreError):
    error_code = "hosted_profile_unknown"

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(
            f"Hosted server profile {profile!r} is unknown to this build, so its execution "
            "policy cannot be established; repair: unset CRUXIBLE_HOSTED_SERVER_PROFILE, or "
            "set it to a profile this build declares."
        )


class InstanceNotFoundError(CoreError):
    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        super().__init__(f"Instance '{instance_id}' not found")


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
        super().__init__(
            f"could not reach Cruxible server at {target}: {reason}. "
            "Repair: run `cruxible server start`"
        )


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


class DaemonOperationScopeError(InstanceScopeError):
    """An instance-scoped credential reached for one daemon-wide operation."""

    def __init__(self, operation: str, credential_scope: str, message: str | None = None):
        self.operation = operation
        self.instance_id = credential_scope
        self.credential_scope = credential_scope
        CoreError.__init__(
            self,
            message
            or (
                f"Credential scoped to instance {credential_scope!r} cannot perform "
                f"daemon-wide operation {operation!r}"
            ),
        )


# What each cumulative permission tier allows, and what it still does not.
PERMISSION_TIER_SUMMARIES: dict[str, str] = {
    "read_only": "reads state and writes nothing",
    "governed_write": ("reads, proposes and authors, but cannot submit approvals or activate"),
    "graph_write": (
        "does everything governed_write does, plus submitting approvals and activating"
    ),
    "admin": (
        "does everything graph_write does, plus operator actions: credentials, host "
        "and init, principal changes, compiler upgrades, provider installs, ledger "
        "mirrors and daemon stop/restart"
    ),
}


def permission_tier_summary(mode: str) -> str:
    """What ``mode`` allows, as ``<mode> mode <predicate>``."""
    summary = PERMISSION_TIER_SUMMARIES.get(mode.strip().lower())
    return f"{mode} mode {summary}" if summary is not None else f"{mode} mode"


def permission_denied_message(
    tool_name: str,
    current_mode: str,
    required_mode: str,
    ceiling_mode: str | None,
) -> str:
    if ceiling_mode is not None:
        denial = (
            f"Operation '{tool_name}' requires {required_mode} mode, but the daemon "
            f"capability ceiling is {ceiling_mode} mode "
            f"(effective request mode: {current_mode})"
        )
    else:
        denial = (
            f"Tool '{tool_name}' requires {required_mode} mode, "
            f"but server is running in {current_mode} mode"
        )
    return f"{denial}. {permission_tier_summary(required_mode)}."


class PermissionDeniedError(CoreError):
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
        super().__init__(
            permission_denied_message(tool_name, current_mode, required_mode, ceiling_mode)
        )


class ErrorResponse(BaseModel):
    """Structured error payload returned by the HTTP server."""

    error_type: str
    message: str
    error_code: str | None = None
    errors: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    # The daemon always fills this in (server.errors.error_to_response). It stays
    # optional on the parsing side so a client never invents a repair the server
    # did not send: a null repair is the truthful reading of an envelope that
    # carried none.
    repair: ServedRepairV1 | None = None


def response_to_error(status: int, body: ErrorResponse) -> CoreError:
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
    elif body.error_type == "PlaybillSinceRequestInvalid":
        exc = PlaybillSinceRequestInvalid(
            field_path=str(context.get("field_path", "$")),
            message=body.message,
        )
    elif body.error_type == "ProposalActivationRequestInvalid":
        exc = ProposalActivationRequestInvalid(body.message)
    elif body.error_type == "ProposalNotFoundError":
        exc = ProposalNotFoundError(
            str(context.get("selector", "unknown")),
            message=body.message,
        )
    elif body.error_type == "ProposalSelectorAmbiguousError":
        exc = ProposalSelectorAmbiguousError(
            str(context.get("selector", "unknown")),
            tuple(str(item) for item in context.get("candidates", [])),
            message=body.message,
        )
    elif body.error_type == "PermissionDeniedError":
        exc = PermissionDeniedError(
            context.get("tool_name", "unknown"),
            context.get("current_mode", "unknown"),
            context.get("required_mode", "unknown"),
            ceiling_mode=context.get("ceiling_mode"),
        )
    elif body.error_type == "InstanceNotFoundError":
        exc = InstanceNotFoundError(context.get("instance_id", "unknown"))
    elif body.error_type == "RuntimeCredentialNotFoundError":
        exc = RuntimeCredentialNotFoundError(context.get("credential_id", "unknown"))
    elif body.error_type in {"AuthenticationError", "BootstrapClaimRefusedError"}:
        exc = AuthenticationError(body.message)
    elif body.error_type == "DaemonOperationScopeError":
        exc = DaemonOperationScopeError(
            str(context.get("operation", "unknown")),
            str(context.get("credential_scope", "unknown")),
            message=body.message,
        )
    elif body.error_type == "InstanceScopeError":
        exc = InstanceScopeError(
            context.get("instance_id", "unknown"),
            context.get("credential_scope", "unknown"),
        )
    elif body.error_type == "CustomerCodeExecutionUnsupportedError":
        exc = CustomerCodeExecutionUnsupportedError(context.get("detail"))
    elif body.error_type == "HostedProfileUnknownError":
        exc = HostedProfileUnknownError(str(context.get("profile", "unknown")))
    elif body.error_type == "PlaybillObjectFormatConflict":
        exc = PlaybillObjectFormatConflict(
            body.message,
            workspace_format=context.get("workspace_format"),
        )
    elif body.error_type == "ReadRefusalError":
        # A read verb's coded refusal keeps its code, candidates and repair line.
        exc = ReadRefusalError.from_served(
            code=body.error_code or "playbill.read.refused",
            message=body.message,
            http_status=status,
            context=context,
        )
    elif body.error_type == "PlaybillInstanceDecommissioned":
        exc = PlaybillInstanceDecommissioned(
            instance_id=context.get("instance_id", "unknown"),
            reason=context.get("reason", "unknown"),
            decommissioned_at=context.get("decommissioned_at", "unknown"),
        )
    else:
        exc = CoreError(body.message)
    if body.error_code is not None:
        setattr(exc, "error_code", body.error_code)
    setattr(exc, "repair", body.repair)
    return exc
