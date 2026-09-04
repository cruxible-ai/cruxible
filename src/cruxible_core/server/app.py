"""FastAPI application and entry point for the Cruxible server."""

from __future__ import annotations

import faulthandler
import os
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
)
from cruxible_client.contracts.errors import (
    ClaimAttestationRequestInvalid,
    PlaybillSinceRequestInvalid,
)
from cruxible_client.contracts.temporal import ISO_8601_FORMAT_HINT
from cruxible_core import __version__
from cruxible_core.errors import CoreError
from cruxible_core.runtime.execution_policy import discover_isolated_executors
from cruxible_core.runtime.permissions import init_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.auth import token_auth_middleware
from cruxible_core.server.config import (
    get_server_fatal_log_path,
    get_server_state_root,
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
from cruxible_core.server.state_lock import StateRootLock

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
    # Fails CLOSED and BEFORE any route exists: a distribution that advertises
    # an isolated executor this build cannot load leaves the shared hosted
    # profile refusing every Provider run for a reason no caller could see, so
    # the daemon refuses to start instead and names the entry point.
    registrations = discover_isolated_executors()
    if registrations:
        _log.info(
            "isolated_executors_registered",
            backend_ids=[item.backend_id for item in registrations],
        )
    manager = get_playbill_manager()
    try:
        manager.recover_provider_runtime()
    except Exception as exc:
        # Last-resort isolation: Provider fence recovery must never prevent the
        # non-Provider daemon surfaces from starting.
        manager.cached_provider_runtime_operator().mark_unavailable(
            "provider_runtime_recovery_failed",
            f"Provider runtime startup recovery failed: {exc}",
            retryable=True,
        )
    app = FastAPI(title="cruxible", responses=STANDARD_ERROR_RESPONSES)
    app.middleware("http")(token_auth_middleware)

    @app.exception_handler(CoreError)
    async def core_error_handler(request: Request, exc: CoreError) -> JSONResponse:
        request.state.error_type = exc.__class__.__name__
        status_code, body = error_to_response(exc)
        return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.endswith("/playbill/since"):
            typed = PlaybillSinceRequestInvalid.from_validation_errors(exc.errors())
            request.state.error_type = typed.__class__.__name__
            status_code, body = error_to_response(typed)
            content = body.model_dump(mode="json")
            content["errors"] = [_format_request_validation_error(err) for err in exc.errors()]
            return JSONResponse(status_code=status_code, content=content)
        if request.url.path.endswith("/playbill/claim-attestations"):
            attestation_error = ClaimAttestationRequestInvalid.from_validation_errors(exc.errors())
            request.state.error_type = attestation_error.__class__.__name__
            status_code, body = error_to_response(attestation_error)
            content = body.model_dump(mode="json")
            content["errors"] = [_format_request_validation_error(err) for err in exc.errors()]
            return JSONResponse(status_code=status_code, content=content)
        errors = [_format_request_validation_error(err) for err in exc.errors()]
        body = ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=errors,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    # Deliberately no blanket pydantic ValidationError handler: it cannot tell a
    # caller's malformed request from a frozen model failing deep inside a
    # service, so it would turn internal invariant breaches into 400s and put
    # internal model names on the wire. Request-shaped refusals are raised as
    # typed CoreErrors where the request is understood; anything else is a real
    # server fault and takes the generic 500 below.
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
        return {
            "version": __version__,
            "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
        }

    app.include_router(instances_router)
    app.include_router(hosted_instances_router)
    app.include_router(runtime_credentials_router)
    app.include_router(playbill_router)
    return app


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    state_root: str | None = None,
    socket_path: str | None = None,
    capability_ceiling: str | None = None,
) -> None:
    """Launch the Cruxible daemon over UDS or host/port transport.

    This is the single daemon-launch path, invoked by ``cruxible server start``.
    Explicit arguments override the corresponding environment variables
    (``CRUXIBLE_HOST`` / ``CRUXIBLE_PORT`` / ``CRUXIBLE_STATE_ROOT`` /
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
    if state_root is not None:
        os.environ["CRUXIBLE_STATE_ROOT"] = state_root
    if socket_path is not None:
        os.environ["CRUXIBLE_SERVER_SOCKET"] = socket_path
    if capability_ceiling is not None:
        os.environ["CRUXIBLE_MODE"] = capability_ceiling

    # Take exclusive ownership of the state root BEFORE any store is opened, so
    # a second daemon over the same root refuses without touching its SQLite
    # files or its ledger. Two daemons over one root can both answer a probe and
    # both write the accepted tree.
    resolved_socket = os.environ.get("CRUXIBLE_SERVER_SOCKET")
    transport = (
        f"unix socket {resolved_socket}"
        if resolved_socket
        else f"{os.environ.get('CRUXIBLE_HOST', '127.0.0.1')}:"
        f"{os.environ.get('CRUXIBLE_PORT', '8100')}"
    )
    with StateRootLock(get_server_state_root(), transport=transport):
        _serve(resolved_socket)


#: The fatal-fault log handle, held for the life of the process. faulthandler
#: writes through the file DESCRIPTOR it was handed; a handle that went out of
#: scope would be closed and the fault trace would land nowhere.
_fatal_fault_log: IO[bytes] | None = None


def enable_fatal_fault_handler(path: Path | None = None) -> Path | None:
    """Point the fatal-fault handler at the daemon's own log directory.

    A daemon death is never silent. A fatal fault -- a segfault, an abort, a bus
    error, a stack overflow -- otherwise takes the process out between two
    access-log lines, leaving the last request logged without a response and
    nothing at all about the exit; that is exactly how a large compile looked
    when it killed a daemon hosting two instances.

    faulthandler's default target is stderr, and stderr is NOT where this
    daemon's log is: `configure_request_logging` binds structlog to a rotating
    file under `<state-root>/daemon/logs/`, so under a terminal multiplexer a
    fault trace written to stderr lands in a scrollback nobody keeps and the
    death still reads as silent in the log an operator actually follows. The
    trace goes to `fatal.log` beside the request log instead.

    Returns the path the handler writes to, or None when no file could be
    opened -- an unwritable log directory, or a build where enabling the
    handler is refused. Losing the trace is not worth refusing to serve, so
    both degrade to a debug line. A SIGKILL from the kernel's OOM killer still
    cannot be observed from inside the process; the MemoryError path in
    authoring preflight is what covers the allocation failure this build sees.
    """

    global _fatal_fault_log
    resolved = get_server_fatal_log_path() if path is None else path
    handle: IO[bytes] | None = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Append, unbuffered: the writer is a signal handler that may be the
        # last thing this process does, so nothing may be left in a buffer.
        handle = resolved.open("ab", buffering=0)
        faulthandler.enable(file=handle)
    except (ValueError, OSError):
        if handle is not None:
            handle.close()
        _log.debug("faulthandler_unavailable", path=str(resolved))
        return None
    _fatal_fault_log = handle
    return resolved


def _serve(resolved_socket: str | None) -> None:
    """Start uvicorn under an already-held state-root lock."""
    enable_fatal_fault_handler()
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

    if resolved_socket:
        socket_file = Path(resolved_socket)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        socket_file.unlink(missing_ok=True)
        uvicorn.run(app, uds=str(socket_file))
        return

    resolved_host = os.environ.get("CRUXIBLE_HOST", "127.0.0.1")
    resolved_port = int(os.environ.get("CRUXIBLE_PORT", "8100"))
    uvicorn.run(app, host=resolved_host, port=resolved_port)
