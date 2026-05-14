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

from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.server.config import (
    get_server_token,
    is_server_auth_enabled,
)
from cruxible_core.server.errors import ErrorResponse

_AUTH_CONTEXT: contextvars.ContextVar["ResolvedAuthContext | None"] = contextvars.ContextVar(
    "cruxible_auth_context",
    default=None,
)


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


def _unauthorized_response(message: str = "Unauthorized") -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content=ErrorResponse(
            error_type="AuthenticationError",
            message=message,
        ).model_dump(mode="json"),
    )


@contextmanager
def _auth_context_scope(
    context: ResolvedAuthContext | None,
) -> Any:
    token = _AUTH_CONTEXT.set(context)
    try:
        yield
    finally:
        _AUTH_CONTEXT.reset(token)


async def token_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    """Resolve auth context and request-scoped permission mode for incoming requests."""
    if request.url.path in {"/health", "/version"}:
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    bearer_token: str | None = None
    if auth_header:
        prefix = "Bearer "
        if not auth_header.startswith(prefix):
            return _unauthorized_response()
        bearer_token = auth_header[len(prefix) :].strip()
        if not bearer_token:
            return _unauthorized_response()

    resolved_context: ResolvedAuthContext | None = None
    configured_token = get_server_token()

    if bearer_token is not None:
        if configured_token and hmac.compare_digest(bearer_token, configured_token):
            resolved_context = ResolvedAuthContext(
                principal_id="legacy_server_token",
                principal_label="legacy_server_token",
                credential_type="legacy_server_token",
                instance_scope=None,
                role="admin",
                effective_permission_mode=None,
                created_by="legacy_server_token",
            )
        elif is_server_auth_enabled():
            return _unauthorized_response()

    if bearer_token is None and is_server_auth_enabled():
        return _unauthorized_response()

    with _auth_context_scope(resolved_context):
        if resolved_context is not None and resolved_context.effective_permission_mode is not None:
            with request_permission_scope(resolved_context.effective_permission_mode):
                return await call_next(request)
        return await call_next(request)
