"""FastAPI application and entry point for the Cruxible server."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cruxible_core import __version__
from cruxible_core.errors import CoreError
from cruxible_core.runtime.permissions import init_permissions
from cruxible_core.server.auth import token_auth_middleware
from cruxible_core.server.config import (
    is_server_auth_enabled,
    validate_server_startup_settings,
    volatile_state_path_warnings,
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.errors import (
    STANDARD_ERROR_RESPONSES,
    ErrorResponse,
    error_to_response,
)
from cruxible_core.server.registry import get_registry
from cruxible_core.server.request_logging import configure_request_logging
from cruxible_core.server.routes.hosted_instances import router as hosted_instances_router
from cruxible_core.server.routes.instances import router as instances_router
from cruxible_core.server.routes.playbill import router as playbill_router
from cruxible_core.server.routes.runtime_credentials import (
    router as runtime_credentials_router,
)
from cruxible_core.temporal import ISO_8601_FORMAT_HINT

_log = structlog.get_logger("cruxible.server.app")

# Generic, schema-free client message for any database error. The real sqlite
# detail (e.g. "UNIQUE constraint failed: internal_table.internal_column") names live
# tables and columns and must never reach the client; it is logged server-side
# instead. See wi-daemon-network-security-hardening (#5).
_DB_CONSTRAINT_MESSAGE = "database constraint violation"
_DB_ERROR_MESSAGE = "database error"

# Pydantic tags every datetime/date failure with a type starting "datetime" or
# "date" (datetime_parsing, datetime_type, date_from_datetime_parsing, ...).
# Its own message names WHAT went wrong ("invalid character in year") but never
# what a good value looks like, so callers resubmit the same malformed shape.
_TEMPORAL_ERROR_TYPE_PREFIXES = ("datetime", "date")


def _format_request_validation_error(error: Mapping[str, Any]) -> str:
    """Render one pydantic request-validation error, self-correcting when temporal."""
    location = ".".join(str(part) for part in (error.get("loc") or ()))
    message = str(error.get("msg", "invalid"))
    error_type = str(error.get("type", ""))
    if error_type.startswith(_TEMPORAL_ERROR_TYPE_PREFIXES):
        message = f"{message} ({ISO_8601_FORMAT_HINT})"
    return f"{location}: {message}"


def create_app() -> FastAPI:
    """Create and configure the Cruxible server app."""
    get_registry()
    app = FastAPI(title="cruxible", responses=STANDARD_ERROR_RESPONSES)
    app.middleware("http")(token_auth_middleware)

    @app.exception_handler(CoreError)
    async def core_error_handler(request: Request, exc: CoreError) -> JSONResponse:
        request.state.error_type = exc.__class__.__name__
        status_code, body = error_to_response(exc)
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [_format_request_validation_error(err) for err in exc.errors()]
        body = ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=errors,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error_handler(
        request: Request, exc: sqlite3.IntegrityError
    ) -> JSONResponse:
        # An unhandled sqlite IntegrityError (UNIQUE/FOREIGN KEY/CHECK/NOT NULL)
        # otherwise surfaces through the catch-all handler below, echoing the raw
        # message (e.g. "UNIQUE constraint failed: <table.col>") and leaking the
        # internal schema. Return a generic 409 and log the real detail only on
        # the server. See wi-daemon-network-security-hardening (#5).
        request.state.error_type = exc.__class__.__name__
        _log.warning(
            "database_integrity_error",
            route=request.url.path,
            method=request.method,
            detail=str(exc),
        )
        body = ErrorResponse(
            error_type="ConstraintViolationError",
            message=_DB_CONSTRAINT_MESSAGE,
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @app.exception_handler(sqlite3.DatabaseError)
    async def database_error_handler(request: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # Any other low-level sqlite error (OperationalError, etc.) may also carry
        # SQL fragments / schema names. Keep the client message generic and log
        # the detail server-side.
        request.state.error_type = exc.__class__.__name__
        _log.error(
            "database_error",
            route=request.url.path,
            method=request.method,
            detail=str(exc),
        )
        body = ErrorResponse(
            error_type="MutationError",
            message=_DB_ERROR_MESSAGE,
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Genuine fallthrough only: every Cruxible domain error subclasses
        # CoreError (dedicated handler above, preserves its intended message),
        # RequestValidationError and the sqlite3 error families also have their
        # own handlers. So `exc` here is an UNEXPECTED exception whose raw
        # str(exc) may embed sqlite SQL, file paths, or other internal detail
        # (e.g. a RuntimeError/ValueError wrapping sqlite text that escaped the
        # sqlite3.* handlers). Returning a generic body keeps that detail off the
        # wire; the real exception is logged server-side for diagnosis. See
        # wi-daemon-network-security-hardening (#5).
        request.state.error_type = exc.__class__.__name__
        _log.error(
            "unhandled_server_error",
            route=request.url.path,
            method=request.method,
            error_type=exc.__class__.__name__,
            detail=str(exc),
            exc_info=exc,
        )
        body = ErrorResponse(
            error_type="InternalServerError",
            message="internal server error",
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        # Liveness only. /health is reachable without a credential, so it must
        # not disclose the daemon's capability ceiling: that tells an
        # unauthenticated prober exactly how much authority this daemon can
        # ever grant. Authorized callers read the tier from the denial context
        # of a refused operation instead.
        return {"status": "ok"}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": __version__}

    app.include_router(instances_router)
    app.include_router(hosted_instances_router)
    app.include_router(runtime_credentials_router)
    app.include_router(playbill_router)
    return app


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    state_dir: str | None = None,
    socket_path: str | None = None,
    capability_ceiling: str | None = None,
) -> None:
    """Launch the Cruxible daemon over UDS or host/port transport.

    This is the single daemon-launch path, invoked by ``cruxible server start``.
    Explicit arguments override the corresponding environment variables
    (``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` / ``CRUXIBLE_SERVER_STATE_DIR`` /
    ``CRUXIBLE_SERVER_SOCKET`` / ``CRUXIBLE_MODE``); when an argument is ``None``
    the env default is used. Overrides are applied to ``os.environ`` before any
    config is resolved so the registry, credential store, permission ceiling,
    and startup validation all observe the same effective settings, and so an
    in-place re-exec (``cruxible server restart``) reproduces them via
    ``sys.argv``.
    """
    if host is not None:
        os.environ["CRUXIBLE_HOST"] = host
    if port is not None:
        os.environ["CRUXIBLE_PORT"] = str(port)
    if state_dir is not None:
        os.environ["CRUXIBLE_SERVER_STATE_DIR"] = state_dir
    if socket_path is not None:
        os.environ["CRUXIBLE_SERVER_SOCKET"] = socket_path
    if capability_ceiling is not None:
        os.environ["CRUXIBLE_MODE"] = capability_ceiling

    # Resolve and freeze the process ceiling before registry/config access or
    # uvicorn startup. Unknown names and attempts to reinitialize this process
    # at a different tier therefore fail closed before the daemon serves.
    init_permissions()

    credential_store = get_runtime_credential_store()
    registry = get_registry()
    runtime_credentials_available = credential_store.has_active_credentials()
    auth_required = credential_store.is_auth_required()
    validate_server_startup_settings(
        runtime_credentials_available=runtime_credentials_available,
        auth_required=auth_required,
    )
    if is_server_auth_enabled():
        credential_store.mark_auth_required("server_startup_auth_enabled")
    for warning in volatile_state_path_warnings(
        instance_locations=[
            (record.instance_id, record.location) for record in registry.list_instances()
        ],
    ):
        print(f"Warning: {warning}", file=sys.stderr)

    import uvicorn

    configure_request_logging()
    app = create_app()

    resolved_socket = os.environ.get("CRUXIBLE_SERVER_SOCKET")
    if resolved_socket:
        socket_file = Path(resolved_socket)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        socket_file.unlink(missing_ok=True)
        uvicorn.run(app, uds=str(socket_file))
        return

    resolved_host = os.environ.get("CRUXIBLE_HOST", "127.0.0.1")
    resolved_port = int(os.environ.get("CRUXIBLE_PORT", "8100"))
    uvicorn.run(app, host=resolved_host, port=resolved_port)
