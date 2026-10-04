"""HTTP auth helpers for the Cruxible server."""

from __future__ import annotations

import contextvars
import hmac
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from fastapi import Request
from fastapi.responses import JSONResponse

from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_client.contracts.operator_mac import (
    OPERATOR_BOOT_HEADER,
    OPERATOR_MAC_HEADER,
    OPERATOR_NONCE_HEADER,
    OPERATOR_TIMESTAMP_HEADER,
)
from cruxible_client.contracts.principals import (
    PRINCIPAL_ID_ENV,
    PRINCIPAL_ID_HEADER,
    is_canonical_principal_id,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.errors import PrincipalRefusalCode, PrincipalRefusedError
from cruxible_core.runtime.permissions import (
    PermissionMode,
    clamp_to_capability_ceiling,
    request_instance_scope,
    request_permission_scope,
)
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server import restart as restart_state
from cruxible_core.server.bootstrap_secret import OperatorRequestRefused, verify_operator_request
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    is_origin_allowed,
    is_server_auth_enabled,
)
from cruxible_core.server.credentials import (
    RuntimeCredentialRecord,
    get_runtime_credential_store,
)
from cruxible_core.server.errors import ErrorResponse, error_to_response
from cruxible_core.server.request_logging import log_runtime_request, mark_request_received
from cruxible_core.server.route_paths import (
    HEALTH_PATH,
    PLAYBILL_FLOOR_DELIVERY_PATH,
    PLAYBILL_HOST_CREATE_PATH,
    PLAYBILL_HOST_SHOW_PATH,
    PLAYBILL_WORKSPACE_ATTACH_PATH,
    PLAYBILL_WORKSPACE_DETACH_PATH,
    RUNTIME_BOOTSTRAP_CLAIM_PATH,
    SERVER_INFO_PATH,
    SERVER_RESTART_PATH,
    SERVER_STOP_PATH,
    VERSION_PATH,
    api_v1_path,
    route_template_matches,
)
from cruxible_core.service.identity import principal_refusal

_AUTH_CONTEXT: contextvars.ContextVar["ResolvedAuthContext | None"] = contextvars.ContextVar(
    "cruxible_auth_context",
    default=None,
)
_REQUEST_OPERATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cruxible_request_operation_id",
    default=None,
)
_REQUEST_CONTEXT: contextvars.ContextVar[Request | None] = contextvars.ContextVar(
    "cruxible_request",
    default=None,
)

EFFECTIVE_PERMISSION_MODE_HEADER = "X-Cruxible-Effective-Permission-Mode"

# Methods that mutate state. A cross-site browser write must clear the Origin
# gate; the no-Origin fail-closed rule below applies only to these.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The one body media type that is NOT a CORS "simple request" content type, so a
# cross-site POST carrying it always triggers a browser preflight (which a
# malicious page cannot satisfy against the loopback daemon). Simple-request
# types (text/plain, application/x-www-form-urlencoded, multipart/form-data) can
# be sent cross-site with NO preflight and NO Origin in some legacy paths, so a
# state-changing request that carries a *body* and lacks an allowed Origin must
# carry exactly this type.
_JSON_MEDIA_TYPE = "application/json"

MISSING_BEARER_CREDENTIAL_MESSAGE = (
    "Daemon reachable; credential missing. Supply a bearer token in "
    "`CRUXIBLE_SERVER_BEARER_TOKEN`. Operators may use the bootstrap-secret file "
    "the daemon writes to <state-root>/daemon/bootstrap-secret (or the copy from "
    "`cruxible server start --auth --bootstrap-secret-file PATH`)."
)


def _request_media_type(request: Request) -> str:
    """Return the lower-cased Content-Type *media type*, stripped of parameters.

    A real header may be e.g. ``application/json; charset=utf-8``; only the media
    type is significant for the simple-request check, so parameters and case are
    discarded.
    """
    content_type = request.headers.get("Content-Type", "")
    return content_type.split(";", 1)[0].strip().lower()


def _request_has_body(request: Request) -> bool:
    """Return whether the request carries a request body.

    True when ``Content-Length`` is a positive integer or the body is chunked
    (``Transfer-Encoding`` present). A bodyless action POST (e.g. ``/revoke``)
    from a CLI client sends ``Content-Length: 0`` and no ``Content-Type`` and is
    NOT a simple-request body-smuggling vector, so it is not subject to the JSON
    requirement. Anything that actually carries bytes must be JSON (below).
    """
    if request.headers.get("Transfer-Encoding"):
        return True
    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        return False
    try:
        return int(raw_length) > 0
    except ValueError:
        # An unparseable Content-Length is suspect; treat as "has body" so the
        # JSON requirement applies (fail closed).
        return True


CredentialType = Literal["runtime_bootstrap", "runtime_credential", "principal_claim"]


@dataclass(frozen=True)
class ResolvedAuthContext:
    """Who a request is: its credential (if any) and the principal it acts as.

    ``credential_id``/``credential_label`` describe the bearer credential; a
    principal claim on an auth-off daemon carries none. ``principal_id`` is the
    governed principal the request acts as, or None when the request names
    none (the runtime bootstrap operator).
    """

    credential_id: str | None
    credential_label: str | None
    credential_type: CredentialType
    instance_scope: str | None
    role: str | None
    effective_permission_mode: PermissionMode | None
    created_by: str | None = None
    principal_id: str | None = None

    @property
    def authenticated(self) -> bool:
        """Whether a bearer credential backs this identity, not only a claim."""

        return self.credential_type != "principal_claim"


def get_current_auth_context() -> ResolvedAuthContext | None:
    """Return the current request-scoped auth context, if any."""
    return _AUTH_CONTEXT.get()


def set_current_operation_id(operation_id: str) -> None:
    """Record the effective governed operation id for request logging."""
    _REQUEST_OPERATION_ID.set(operation_id)
    request = _REQUEST_CONTEXT.get()
    if request is not None:
        request.state.operation_id = operation_id


def _unauthorized_response(message: str = "Unauthorized") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=ErrorResponse(
            error_type="AuthenticationError",
            message=message,
        ).model_dump(mode="json"),
    )


def _identity_refusal_response(request: Request, refusal: PrincipalRefusedError) -> JSONResponse:
    status, body = error_to_response(refusal)
    response = JSONResponse(status_code=status, content=body.model_dump(mode="json"))
    log_runtime_request(
        request,
        status=response.status_code,
        auth_context=None,
        error_type=refusal.__class__.__name__,
    )
    return response


#: The lifecycle requests a local command may sign instead of sending a secret.
_OPERATOR_MAC_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", api_v1_path(SERVER_INFO_PATH)),
    ("POST", api_v1_path(SERVER_RESTART_PATH)),
    ("POST", api_v1_path(SERVER_STOP_PATH)),
)


def _operator_mac_refusal(
    request: Request, *, bootstrap_secret: str | None, has_bearer: bool
) -> PrincipalRefusedError | None:
    """Verify one MAC-signed lifecycle request, or say why it is refused."""

    def refused(code: str, detail: str) -> PrincipalRefusedError:
        return PrincipalRefusedError(
            cast(PrincipalRefusalCode, code),
            f"{detail}; repair: run the command on the daemon's own host with its state "
            "root, or set CRUXIBLE_SERVER_BEARER_TOKEN",
            repair=RepairOperation(operation="server.status"),
        )

    if bootstrap_secret is None or has_bearer or _request_has_body(request):
        return refused(
            "runtime_bootstrap.operator_mac_invalid",
            "a signed operator request needs an auth-on daemon with a bootstrap secret, "
            "no bearer token, and no body",
        )
    if not any(
        request.method == method and route_template_matches(request.url.path, route)
        for method, route in _OPERATOR_MAC_ROUTES
    ):
        return refused(
            "runtime_bootstrap.operator_mac_invalid",
            "a signed operator request authorizes only server status, restart and stop",
        )
    try:
        verify_operator_request(
            bootstrap_secret,
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            nonce=request.headers.get(OPERATOR_NONCE_HEADER),
            timestamp=request.headers.get(OPERATOR_TIMESTAMP_HEADER),
            mac=request.headers.get(OPERATOR_MAC_HEADER),
            boot_id=request.headers.get(OPERATOR_BOOT_HEADER),
            current_boot_id=restart_state.PROCESS_BOOT_ID,
        )
    except OperatorRequestRefused as exc:
        return refused(exc.code, str(exc))
    return None


def _bound_principal_refusal(credential: RuntimeCredentialRecord) -> PrincipalRefusedError | None:
    """Refuse, and revoke, a credential whose principal is no longer active.

    Revoking a principal revokes every credential that acts as it. The accepted
    registry is the authority, so this is checked on use rather than trusted to
    a sweep: the first request after the revocation lands revokes the rows.
    """

    if credential.principal_id is None:
        return None
    try:
        instance = get_playbill_manager().get(credential.instance_id)
    except PlaybillBootstrapError:
        return None
    refusal = principal_refusal(instance, credential.principal_id, configured=True)
    if refusal is not None:
        get_runtime_credential_store().revoke_credentials_of_principal(
            instance_id=credential.instance_id, principal_id=credential.principal_id
        )
    return refusal


def _principal_claim_refusal(request: Request) -> PrincipalRefusedError | None:
    """Refuse a principal claim no registry could hold, before it names anyone."""

    raw = request.headers.get(PRINCIPAL_ID_HEADER)
    if raw is None or is_canonical_principal_id(raw.strip()):
        return None
    return PrincipalRefusedError(
        "playbill.identity.principal_claim_invalid",
        f"the configured principal ID {raw.strip()!r} is not a canonical lowercase "
        "identifier (a letter, then up to 127 of a-z 0-9 . _ -); repair: set "
        f"{PRINCIPAL_ID_ENV} or --principal-id to a registered principal ID",
        repair=RepairOperation(operation="playbill.orient", arguments={"section": "principals"}),
    )


def _unauthorized_request_response(request: Request, message: str = "Unauthorized") -> JSONResponse:
    response = _unauthorized_response(message)
    log_runtime_request(
        request,
        status=response.status_code,
        auth_context=None,
        error_type="AuthenticationError",
    )
    return response


def _forbidden_origin_response(request: Request) -> JSONResponse:
    """Reject a browser cross-origin request to the HTTP API.

    A normal CLI/SDK client sends no ``Origin`` header; only a browser does. A
    cross-origin ``Origin`` that is neither loopback nor explicitly allowlisted is
    a DNS-rebinding / malicious-webpage-hits-localhost attempt, so it is refused
    before any handler runs. See wi-daemon-network-security-hardening (#4).
    """
    response = JSONResponse(
        status_code=403,
        content=ErrorResponse(
            error_type="OriginNotAllowedError",
            message="Cross-origin browser requests are not allowed",
        ).model_dump(mode="json"),
    )
    log_runtime_request(
        request,
        status=response.status_code,
        auth_context=None,
        error_type="OriginNotAllowedError",
    )
    return response


_RUNTIME_BOOTSTRAP_CLAIM_ROUTE = api_v1_path(RUNTIME_BOOTSTRAP_CLAIM_PATH)
_PLAYBILL_HOST_CREATE_ROUTE = api_v1_path(PLAYBILL_HOST_CREATE_PATH)
_PLAYBILL_HOST_SHOW_ROUTE = api_v1_path(PLAYBILL_HOST_SHOW_PATH)
# (method, route) pairs for the daemon-wide server-operation endpoints that the
# unscoped runtime bootstrap operator may drive directly with the bootstrap secret.
_SERVER_OPERATION_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", api_v1_path(SERVER_INFO_PATH)),
    ("POST", api_v1_path(SERVER_RESTART_PATH)),
    ("POST", api_v1_path(SERVER_STOP_PATH)),
    ("GET", _PLAYBILL_HOST_SHOW_ROUTE),
    ("POST", api_v1_path(PLAYBILL_WORKSPACE_DETACH_PATH)),
    ("POST", api_v1_path(PLAYBILL_WORKSPACE_ATTACH_PATH)),
    ("POST", api_v1_path(PLAYBILL_FLOOR_DELIVERY_PATH)),
)


def _is_bootstrap_claim_request(request: Request) -> bool:
    return request.method == "POST" and route_template_matches(
        request.url.path,
        _RUNTIME_BOOTSTRAP_CLAIM_ROUTE,
    )


def _is_playbill_host_create_request(request: Request) -> bool:
    return request.method == "POST" and route_template_matches(
        request.url.path,
        _PLAYBILL_HOST_CREATE_ROUTE,
    )


def _is_server_operation_request(request: Request) -> bool:
    """Return whether the request targets a daemon-wide server-operation route."""
    return any(
        request.method == method and route_template_matches(request.url.path, route)
        for method, route in _SERVER_OPERATION_ROUTES
    )


def _runtime_bootstrap_operator_context() -> ResolvedAuthContext:
    """Build the unscoped (``instance_scope=None``) runtime bootstrap operator context."""
    return ResolvedAuthContext(
        credential_id="runtime_bootstrap",
        credential_label="runtime_bootstrap",
        credential_type="runtime_bootstrap",
        instance_scope=None,
        role="admin",
        effective_permission_mode=PermissionMode.ADMIN,
        created_by="runtime_bootstrap",
    )


@contextmanager
def _auth_context_scope(
    context: ResolvedAuthContext | None,
    request: Request,
) -> Any:
    auth_token = _AUTH_CONTEXT.set(context)
    operation_token = _REQUEST_OPERATION_ID.set(None)
    request_token = _REQUEST_CONTEXT.set(request)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(request_token)
        _REQUEST_OPERATION_ID.reset(operation_token)
        _AUTH_CONTEXT.reset(auth_token)


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    """Resolve auth context and request-scoped permission mode for incoming requests."""
    mark_request_received(request)
    # Reject browser-originated cross-origin API requests before any handler runs.
    # Programmatic clients send no Origin; this closes DNS-rebinding / malicious
    # webpage attacks against the loopback daemon without breaking CLI/SDK clients.
    # Browsers always attach Origin to cross-origin and to every non-GET request
    # (so the whole mutating surface is covered); Referer is consulted only as a
    # fallback when Origin is absent, since referrer-policy can suppress it.
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin is not None and not is_origin_allowed(origin):
        return _forbidden_origin_response(request)
    # Fail CLOSED for state-changing methods that carry a BODY but present NO
    # allowed Origin/Referer. `is_origin_allowed(None)` is True, so a missing
    # Origin reaches here as "allowed"; without this guard a cross-site
    # *simple-request* POST (no Origin, text/plain or form/multipart body) would
    # sail through at loopback-ADMIN. The whole mutating surface today binds a
    # JSON body, but a future raw/form route must not silently re-open that hole.
    # A bodied request must use the JSON media type (a non-CORS-simple content
    # type that forces a browser preflight). Constraints this preserves:
    #   - CLI/SDK clients send application/json with no Origin -> pass.
    #   - Bodyless action POSTs (/revoke, /rotate, restart) send no body and no
    #     Content-Type -> not a body-smuggling vector, pass.
    #   - An allowed (loopback/allowlisted) Origin passes regardless of body type
    #     (handled by the check above).
    #   - GET/HEAD/OPTIONS are unaffected.
    if (
        request.method in _STATE_CHANGING_METHODS
        and origin is None
        and _request_has_body(request)
        and _request_media_type(request) != _JSON_MEDIA_TYPE
    ):
        return _forbidden_origin_response(request)
    # Credential-free surfaces: liveness and version.
    # They skip auth resolution but NOT the Origin allowlist above — a hostile
    # page must not be able to fingerprint the loopback daemon by probing
    # /health or /version from the browser.
    if request.url.path in {HEALTH_PATH, VERSION_PATH}:
        return await call_next(request)
    if _is_bootstrap_claim_request(request):
        return await _call_next_with_request_log(request, call_next, auth_context=None)

    auth_header = request.headers.get("Authorization", "")
    bearer_token: str | None = None
    if auth_header:
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return _unauthorized_request_response(request)
        bearer_token = auth_header[len(prefix) :].strip()
        if not bearer_token:
            return _unauthorized_request_response(request)

    resolved_context: ResolvedAuthContext | None = None
    bootstrap_secret = get_runtime_bootstrap_secret()
    auth_enabled = is_server_auth_enabled()

    if request.headers.get(OPERATOR_MAC_HEADER) is not None:
        # A local lifecycle command signed this request with the bootstrap
        # secret instead of sending it. Only the daemon-wide lifecycle reads and
        # levers accept it, never alongside a bearer token, never with a body.
        mac_refusal = _operator_mac_refusal(
            request,
            bootstrap_secret=bootstrap_secret if auth_enabled else None,
            has_bearer=bearer_token is not None,
        )
        if mac_refusal is not None:
            return _identity_refusal_response(request, mac_refusal)
        if request.headers.get(EFFECTIVE_PERMISSION_MODE_HEADER) is not None:
            return _unauthorized_request_response(request)
        operator_mode = clamp_to_capability_ceiling(PermissionMode.ADMIN)
        resolved_context = replace(
            _runtime_bootstrap_operator_context(), effective_permission_mode=operator_mode
        )
        with _auth_context_scope(resolved_context, request):
            with (
                request_permission_scope(operator_mode),
                request_instance_scope(None),
            ):
                return await _call_next_with_request_log(
                    request, call_next, auth_context=resolved_context
                )

    if bearer_token is not None:
        if (
            # Daemon-wide operator actions -- global metadata, in-place re-exec,
            # graceful stop, host inspection, and allocating a host -- are
            # authorized for the unscoped runtime bootstrap operator and never
            # for an instance-scoped runtime credential, which resolves below
            # and is rejected by the runtime's require_unscoped_operator gate.
            #
            # Host creation used to be gated on the bootstrap secret being
            # UNCLAIMED, which made a daemon a one-shot: `credential
            # claim-bootstrap` consumes the claim, every other credential on the
            # daemon is instance-scoped, and so no credential could allocate a
            # second host. The only repair was restarting the daemon, which
            # mints a fresh secret at the same path -- silently invalidating the
            # operator's saved copy and taking every hosted instance offline. A
            # control plane cannot restart the daemon to add a tenant. Creating
            # a host is a repeatable operator action like the rest of this list,
            # and it is strictly weaker than the restart and stop already here.
            auth_enabled
            and bootstrap_secret
            and hmac.compare_digest(bearer_token, bootstrap_secret)
            and (_is_server_operation_request(request) or _is_playbill_host_create_request(request))
        ):
            resolved_context = _runtime_bootstrap_operator_context()
        elif auth_enabled:
            runtime_credential = get_runtime_credential_store().authenticate(bearer_token)
            if runtime_credential is not None:
                standing_refusal = _bound_principal_refusal(runtime_credential)
                if standing_refusal is not None:
                    return _identity_refusal_response(request, standing_refusal)
                resolved_context = ResolvedAuthContext(
                    credential_id=runtime_credential.credential_id,
                    credential_label=runtime_credential.label,
                    credential_type="runtime_credential",
                    instance_scope=runtime_credential.instance_id,
                    role=runtime_credential.permission_mode.name.lower(),
                    effective_permission_mode=runtime_credential.permission_mode,
                    created_by=runtime_credential.created_by,
                    principal_id=runtime_credential.principal_id,
                )
            else:
                return _unauthorized_request_response(request)

    if bearer_token is None and auth_enabled:
        return _unauthorized_request_response(request, MISSING_BEARER_CREDENTIAL_MESSAGE)
    claim_refusal = _principal_claim_refusal(request)
    if claim_refusal is not None:
        return _identity_refusal_response(request, claim_refusal)
    claimed = request.headers.get(PRINCIPAL_ID_HEADER)
    if claimed is not None:
        claimed = claimed.strip()
        if not auth_enabled:
            # Auth off: the claim IS the identity. Every process of this OS user
            # is equally trusted, so this names who acts; it proves nothing.
            resolved_context = ResolvedAuthContext(
                credential_id=None,
                credential_label=None,
                credential_type="principal_claim",
                instance_scope=None,
                role=None,
                effective_permission_mode=None,
                principal_id=claimed,
            )
        elif (
            resolved_context is not None
            and resolved_context.credential_type == "runtime_credential"
            and resolved_context.principal_id != claimed
        ):
            # Auth on: the credential decides who acts. A claim may only repeat it.
            return _identity_refusal_response(
                request,
                PrincipalRefusedError(
                    "playbill.identity.principal_claim_mismatch",
                    f"the configured principal ID {claimed!r} is not the principal this "
                    "bearer credential acts as "
                    f"({resolved_context.principal_id or 'none'}); repair: unset "
                    f"{PRINCIPAL_ID_ENV} (or --principal-id), or use the credential "
                    "minted for that principal",
                    repair=RepairOperation(operation="playbill.whoami"),
                ),
            )
        # The runtime bootstrap operator acts as no principal; a claim sent
        # alongside its daemon-wide operations is ignored, not honored.
    if request.headers.get(EFFECTIVE_PERMISSION_MODE_HEADER) is not None and (
        resolved_context is None or resolved_context.credential_type != "runtime_credential"
    ):
        return _unauthorized_request_response(request)

    effective_mode: PermissionMode | None = None
    if resolved_context is not None and resolved_context.effective_permission_mode is not None:
        relayed_mode = _relayed_effective_permission_mode(request, resolved_context)
        if relayed_mode is None:
            return _unauthorized_request_response(request)
        effective_mode = clamp_to_capability_ceiling(relayed_mode)
        resolved_context = replace(
            resolved_context,
            effective_permission_mode=effective_mode,
        )

    with _auth_context_scope(resolved_context, request):
        if effective_mode is not None:
            assert resolved_context is not None
            with (
                request_permission_scope(effective_mode),
                request_instance_scope(resolved_context.instance_scope),
            ):
                return await _call_next_with_request_log(
                    request,
                    call_next,
                    auth_context=resolved_context,
                )
        return await _call_next_with_request_log(
            request,
            call_next,
            auth_context=resolved_context,
        )


def _relayed_effective_permission_mode(
    request: Request,
    context: ResolvedAuthContext,
) -> PermissionMode | None:
    raw_mode = request.headers.get(EFFECTIVE_PERMISSION_MODE_HEADER)
    if raw_mode is None:
        return context.effective_permission_mode
    if context.credential_type != "runtime_credential":
        return None
    try:
        relayed_mode = PermissionMode[raw_mode.strip().upper()]
    except KeyError:
        return None
    credential_mode = context.effective_permission_mode
    if credential_mode is None or relayed_mode > credential_mode:
        return None
    return relayed_mode


async def _call_next_with_request_log(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
    *,
    auth_context: ResolvedAuthContext | None,
) -> Any:
    try:
        response = await call_next(request)
    except Exception as exc:
        log_runtime_request(
            request,
            status=500,
            auth_context=auth_context,
            operation_id=_REQUEST_OPERATION_ID.get(),
            error_type=exc.__class__.__name__,
        )
        raise
    log_runtime_request(
        request,
        status=response.status_code,
        auth_context=auth_context,
        operation_id=_REQUEST_OPERATION_ID.get(),
        error_type=getattr(request.state, "error_type", None),
    )
    return response
