"""Structured runtime request logging for the HTTP server."""

from __future__ import annotations

import sys
from typing import Any

import structlog
from fastapi import Request

_log = structlog.get_logger("cruxible.server.requests")


def configure_request_logging() -> None:
    """Configure production server request logs as JSON on stderr."""
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        # Not cached: a cache-bound logger survives later structlog.configure
        # calls, which permanently detaches request logs from any
        # reconfiguration (observed as test-order-dependent log capture).
        cache_logger_on_first_use=False,
    )


def log_runtime_request(
    request: Request,
    *,
    status: int,
    auth_context: Any,
    error_type: str | None = None,
) -> None:
    """Emit one safe structured log event for a runtime HTTP request."""
    fields: dict[str, Any] = {
        "method": request.method,
        "route": _request_route(request),
        "status": status,
        "principal_id": _context_field(auth_context, "principal_id", "anonymous"),
        "credential_type": _context_field(auth_context, "credential_type", "anonymous"),
        "role": _context_field(auth_context, "role", None),
        "instance_scope": _context_field(auth_context, "instance_scope", None),
        "instance_id": _request_instance_id(request),
    }
    operation_id = getattr(request.state, "operation_id", None)
    if operation_id is not None:
        fields["operation_id"] = str(operation_id)
    if error_type is not None:
        fields["error_type"] = error_type
    _log.info("runtime_request", **fields)


def _context_field(auth_context: Any, field: str, default: str | None) -> str | None:
    if auth_context is None:
        return default
    value = getattr(auth_context, field, default)
    if value is None:
        return None
    return str(value)


def _request_route(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


def _request_instance_id(request: Request) -> str | None:
    value = request.path_params.get("instance_id")
    if value is None:
        return None
    return str(value)
