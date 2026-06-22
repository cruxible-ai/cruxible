"""HTTP auth helpers for the Cruxible server."""

from __future__ import annotations

import contextvars
import hmac
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from cruxible_core.runtime.permissions import (
    PermissionMode,
    request_instance_scope,
    request_permission_scope,
)
from cruxible_core.server.config import (
    get_runtime_bootstrap_secret,
    is_origin_allowed,
    is_server_auth_enabled,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.errors import ErrorResponse
from cruxible_core.server.request_logging import log_runtime_request
from cruxible_core.server.route_paths import (
    HEALTH_PATH,
    HOSTED_INSTANCE_INIT_PATH,
    INSTANCE_RESTORE_PATH,
    RUNTIME_BOOTSTRAP_CLAIM_PATH,
    SERVER_INFO_PATH,
    SERVER_RESTART_PATH,
    VERSION_PATH,
    api_v1_path,
    is_ui_static_path,
    route_template_matches,
)

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


@dataclass(frozen=True)
class ResolvedAuthContext:
    principal_id: str
    principal_label: str
    credential_type: str
    instance_scope: str | None
    role: str | None
    effective_permission_mode: PermissionMode | None
    created_by: str | None = None


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
_HOSTED_INSTANCE_INIT_ROUTE = api_v1_path(HOSTED_INSTANCE_INIT_PATH)
# (method, route) pairs for the daemon-wide server-operation endpoints that the
# unscoped runtime bootstrap operator may drive directly with the bootstrap secret.
_SERVER_OPERATION_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", api_v1_path(SERVER_INFO_PATH)),
    ("POST", api_v1_path(SERVER_RESTART_PATH)),
    ("POST", api_v1_path(INSTANCE_RESTORE_PATH)),
)


def _is_bootstrap_claim_request(request: Request) -> bool:
    return request.method == "POST" and route_template_matches(
        request.url.path,
        _RUNTIME_BOOTSTRAP_CLAIM_ROUTE,
    )


def _is_hosted_instance_init_request(request: Request) -> bool:
    return request.method == "POST" and route_template_matches(
        request.url.path,
        _HOSTED_INSTANCE_INIT_ROUTE,
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
        principal_id="runtime_bootstrap",
        principal_label="runtime_bootstrap",
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
    if request.url.path in {HEALTH_PATH, VERSION_PATH} or is_ui_static_path(request.url.path):
        return await call_next(request)
    # Reject browser-originated cross-origin API requests before any handler runs.
    # Programmatic clients send no Origin; this closes DNS-rebinding / malicious
    # webpage attacks against the loopback daemon without breaking CLI/SDK clients.
    # Browsers always attach Origin to cross-origin and to every non-GET request
    # (so the whole mutating surface is covered); Referer is consulted only as a
    # fallback when Origin is absent, since referrer-policy can suppress it.
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin is not None and not is_origin_allowed(origin):
        return _forbidden_origin_response(request)
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

    if bearer_token is not None:
        if (
            auth_enabled
            and _is_hosted_instance_init_request(request)
            and bootstrap_secret
            and hmac.compare_digest(bearer_token, bootstrap_secret)
            and not get_runtime_credential_store().bootstrap_secret_claimed(bootstrap_secret)
        ):
            resolved_context = _runtime_bootstrap_operator_context()
        elif (
            # Daemon-wide server operations (global metadata, in-place re-exec,
            # restore) are authorized for the unscoped runtime bootstrap operator,
            # never for an instance-scoped runtime credential. Unlike hosted init,
            # these are repeatable operator actions, so they are NOT gated on the
            # one-time bootstrap claim. An instance-scoped credential that presents
            # its own token still resolves below and is rejected by the runtime's
            # require_unscoped_operator gate.
            auth_enabled
            and _is_server_operation_request(request)
            and bootstrap_secret
            and hmac.compare_digest(bearer_token, bootstrap_secret)
        ):
            resolved_context = _runtime_bootstrap_operator_context()
        elif auth_enabled:
            runtime_credential = get_runtime_credential_store().authenticate(bearer_token)
            if runtime_credential is not None:
                resolved_context = ResolvedAuthContext(
                    principal_id=runtime_credential.credential_id,
                    principal_label=runtime_credential.label,
                    credential_type="runtime_credential",
                    instance_scope=runtime_credential.instance_id,
                    role=runtime_credential.permission_mode.name.lower(),
                    effective_permission_mode=runtime_credential.permission_mode,
                    created_by=runtime_credential.created_by,
                )
            else:
                return _unauthorized_request_response(request)

    if bearer_token is None and auth_enabled:
        return _unauthorized_request_response(request)
    if request.headers.get(EFFECTIVE_PERMISSION_MODE_HEADER) is not None and (
        resolved_context is None or resolved_context.credential_type != "runtime_credential"
    ):
        return _unauthorized_request_response(request)

    with _auth_context_scope(resolved_context, request):
        if resolved_context is not None and resolved_context.effective_permission_mode is not None:
            relayed_mode = _relayed_effective_permission_mode(request, resolved_context)
            if relayed_mode is None:
                return _unauthorized_request_response(request)
            with (
                request_permission_scope(relayed_mode),
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
