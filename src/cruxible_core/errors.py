"""Error hierarchy for Cruxible Core.

Every exception inherits from CoreError. Playbill refusals carry their own
typed errors in ``cruxible_client.contracts.errors``; these are the runtime and
credential errors shared by the daemon, CLI and MCP boundaries.

    CoreError
    ├── FloorAdmissionMisuse (internal floor admission programming error)
    ├── ConfigError (invalid configuration or request shape)
    ├── DataValidationError (payload does not match its declared contract)
    │   └── RequestRefusedError (a coded refusal of caller input, with its repair)
    ├── CustomerCodeExecutionUnsupportedError (hosted profile refuses customer code)
    ├── HostedProfileUnknownError (unknown hosted server profile)
    ├── IsolatedExecutorDiscoveryError (advertised isolated executor failed to load)
    ├── InstanceNotFoundError (instance registry lookup)
    ├── RuntimeCredentialNotFoundError (server credential store lookup)
    ├── AuthenticationError (HTTP/API credential failure)
    │   └── BootstrapClaimRefusedError (one refused runtime bootstrap claim)
    ├── InstanceScopeError (HTTP/API credential scope mismatch)
    │   └── DaemonOperationScopeError (instance-scoped credential on a daemon operation)
    └── PermissionDeniedError (permission mode)
"""

from __future__ import annotations

from typing import Literal

from cruxible_client._error_base import CoreError as CoreError
from cruxible_client.contracts.repairs import HandEditRepairV1, RepairOperationV1
from cruxible_client.errors import permission_denied_message

_MAX_DISPLAY_ERRORS = 10


def _format_capped_errors(errors: list[str]) -> str:
    shown = errors[:_MAX_DISPLAY_ERRORS]
    detail = "; ".join(shown)
    if len(errors) > _MAX_DISPLAY_ERRORS:
        detail += f" ... and {len(errors) - _MAX_DISPLAY_ERRORS} more error(s)"
    return detail


class FloorAdmissionMisuse(CoreError):
    """An internal admission misuse, surfaced through the typed error boundaries."""

    error_code = "internal.floor_admission_misuse"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.error_code}: {message}")


class ConfigError(CoreError):
    """Invalid configuration YAML.

    Raised when config fails schema validation or cross-reference checks.
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}"


class DataValidationError(CoreError):
    """Ingested data doesn't match config schema.

    Raised when CSV/JSON data doesn't conform to the entity/relationship
    property definitions in the config (wrong columns, bad types, etc.).
    """

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
    ):
        self.summary = message
        self.errors = errors or []
        super().__init__(message)

    def __str__(self) -> str:
        if not self.errors:
            return self.summary
        detail = _format_capped_errors(self.errors)
        return f"{self.summary}: {detail}"


class RequestRefusedError(DataValidationError):
    """A coded refusal of caller input that names the repair.

    ``repair`` is a served repair (a runnable operation or a hand edit); the
    HTTP boundary renders it as the envelope's repair and answers 400.
    """

    def __init__(self, error_code: str, message: str, *, repair: object) -> None:
        self.error_code = error_code
        self.repair = repair
        super().__init__(f"{error_code}: {message}")


class CustomerCodeExecutionUnsupportedError(CoreError):
    """Customer code execution is unavailable in the current hosted runtime."""

    error_code = "customer_code_execution_unsupported"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail
        message = "Customer code execution is not supported in this hosted runtime profile."
        super().__init__(message if detail is None else f"{message} ({detail})")


class HostedProfileUnknownError(CoreError):
    """The configured hosted server profile is not one this build understands."""

    error_code = "hosted_profile_unknown"

    def __init__(self, profile: str) -> None:
        self.profile = profile
        super().__init__(
            f"Hosted server profile {profile!r} is unknown to this build, so its execution "
            "policy cannot be established; repair: unset CRUXIBLE_HOSTED_SERVER_PROFILE, or "
            "set it to a profile this build declares."
        )


class IsolatedExecutorDiscoveryError(CoreError):
    """An advertised isolated executor could not be loaded or registered.

    Fails CLOSED at daemon start rather than at the first Provider run: an
    operator who installed an executor package that this build cannot load has
    a daemon whose execution policy it cannot establish, and starting anyway
    would leave the shared profile refusing for a reason nobody can see. The
    broken backend stays unregistered either way.
    """

    error_code = "isolated_executor_discovery_failed"

    def __init__(
        self,
        *,
        name: str,
        entry_point: str,
        distribution: str | None,
        group: str,
        detail: str,
    ) -> None:
        self.name = name
        self.entry_point = entry_point
        self.distribution = distribution
        self.group = group
        self.detail = detail
        # The repair is "remove or repair the distribution", so the message has
        # to name the distribution. A module name is not a package name, and
        # `importlib.metadata` hands the package over on the same object, so an
        # operator reading this knows what to uninstall without going looking.
        advertised = (
            f"distribution {distribution!r}"
            if distribution is not None
            else "an unknown distribution"
        )
        repair = (
            f"remove or repair {distribution!r}"
            if distribution is not None
            else "remove or repair the distribution that advertises it"
        )
        super().__init__(
            f"Isolated executor entry point {name!r} = {entry_point!r} in group {group!r}, "
            f"advertised by {advertised}, could not be registered: {detail}; "
            f"repair: {repair}, then start the daemon again."
        )


class InstanceNotFoundError(CoreError):
    """Cruxible instance not found."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        super().__init__(f"Instance '{instance_id}' not found")


class RuntimeCredentialNotFoundError(CoreError):
    """Runtime credential ID not found in the server credential store."""

    def __init__(self, credential_id: str):
        self.credential_id = credential_id
        super().__init__(f"Runtime credential '{credential_id}' not found")


class AuthenticationError(CoreError):
    """HTTP/API request is unauthenticated or uses an invalid credential."""

    pass


BootstrapClaimRefusalCode = Literal[
    "runtime_bootstrap.secret_invalid",
    "runtime_bootstrap.secret_already_claimed",
    "runtime_bootstrap.admin_exists",
    "runtime_bootstrap.claim_conflict",
]

# One line each: what was refused, then the repair. None of them names or
# hints at the expected secret.
_BOOTSTRAP_CLAIM_REFUSALS: dict[str, tuple[str, str]] = {
    "runtime_bootstrap.secret_invalid": (
        "The bootstrap secret does not match this daemon's runtime bootstrap secret.",
        "pass the exact CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET the daemon was started with "
        "(--secret-file or the env var).",
    ),
    "runtime_bootstrap.secret_already_claimed": (
        "This bootstrap secret has already been claimed; it mints one ADMIN credential once.",
        "use the ADMIN token that claim printed, or run `cruxible credential recover-admin` "
        "with the daemon stopped.",
    ),
    "runtime_bootstrap.admin_exists": (
        "Instance {instance_id} already has an ADMIN credential.",
        "this instance is already bootstrapped; use an existing ADMIN credential or run "
        "`cruxible credential recover-admin` with the daemon stopped.",
    ),
    "runtime_bootstrap.claim_conflict": (
        "The bootstrap claim collided with a concurrent claim or failed an integrity check.",
        "retry `cruxible credential claim-bootstrap`.",
    ),
}


class BootstrapClaimRefusedError(AuthenticationError):
    """The one-time runtime bootstrap claim was refused, with its specific reason."""

    def __init__(self, error_code: BootstrapClaimRefusalCode, *, instance_id: str) -> None:
        summary, repair = _BOOTSTRAP_CLAIM_REFUSALS[error_code]
        self.error_code = error_code
        self.instance_id = instance_id
        super().__init__(
            f"{error_code}: {summary.format(instance_id=instance_id)} Repair: {repair}"
        )


PrincipalRefusalCode = Literal[
    "playbill.identity.principal_claim_invalid",
    "playbill.identity.principal_claim_mismatch",
    "playbill.identity.principal_absent",
    "playbill.identity.principal_revoked",
    "playbill.identity.principal_unconfigured",
    "playbill.identity.init_owner_mismatch",
    "playbill.identity.credential_unbound",
    "playbill.identity.permission_insufficient",
    "runtime_credential.auth_off",
    "runtime_bootstrap.operator_mac_invalid",
    "runtime_bootstrap.operator_mac_stale",
    "runtime_bootstrap.operator_mac_replayed",
    "runtime_credential.principal_not_ordinary",
    "runtime_credential.principal_authority_required",
    "runtime_credential.principal_proof_invalid",
    "runtime_credential.principal_proof_replayed",
]

#: HTTP status per identity refusal: a malformed claim is a bad request, a claim
#: contradicting its credential is unauthenticated, and a well-formed identity
#: that may not act here is forbidden.
_PRINCIPAL_REFUSAL_STATUS: dict[str, int] = {
    "playbill.identity.principal_claim_invalid": 400,
    "playbill.identity.principal_claim_mismatch": 401,
    "runtime_credential.principal_proof_replayed": 409,
    "runtime_credential.auth_off": 409,
    "runtime_bootstrap.operator_mac_invalid": 401,
    "runtime_bootstrap.operator_mac_stale": 401,
    "runtime_bootstrap.operator_mac_replayed": 401,
    "runtime_bootstrap.operator_mac_boot_changed": 401,
}


class PrincipalRefusedError(CoreError):
    """The principal a request acts as cannot act here; names the code and repair.

    The message already carries the runnable CLI command; ``repair`` carries the
    same next step as a served operation so every surface renders it typed.
    """

    def __init__(
        self,
        error_code: PrincipalRefusalCode,
        message: str,
        *,
        repair: RepairOperationV1 | HandEditRepairV1 | None = None,
    ) -> None:
        self.error_code = error_code
        self.repair = repair
        self.http_status = _PRINCIPAL_REFUSAL_STATUS.get(error_code, 403)
        super().__init__(f"{error_code}: {message}")


class InstanceScopeError(CoreError):
    """Runtime credential scope does not match the requested instance."""

    def __init__(self, instance_id: str, credential_scope: str):
        self.instance_id = instance_id
        self.credential_scope = credential_scope
        super().__init__(
            f"Credential scoped to instance '{credential_scope}' cannot access "
            f"instance '{instance_id}'"
        )


class DaemonOperationScopeError(InstanceScopeError):
    """An instance-scoped credential attempted one daemon-wide operation."""

    def __init__(self, operation: str, credential_scope: str):
        self.operation = operation
        self.credential_scope = credential_scope
        CoreError.__init__(
            self,
            f"Credential scoped to instance {credential_scope!r} cannot perform daemon-wide "
            f"operation {operation!r}; use an unscoped operator credential",
        )


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
        super().__init__(
            permission_denied_message(tool_name, current_mode, required_mode, ceiling_mode)
        )
