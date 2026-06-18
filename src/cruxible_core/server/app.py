"""FastAPI application and entry point for the Cruxible server."""

from __future__ import annotations

import os
from pathlib import Path

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
)
from cruxible_core.server.credentials import get_runtime_credential_store
from cruxible_core.server.errors import (
    STANDARD_ERROR_RESPONSES,
    ErrorResponse,
    error_to_response,
)
from cruxible_core.server.registry import get_registry
from cruxible_core.server.request_logging import configure_request_logging
from cruxible_core.server.routes.decision_records import router as decision_records_router
from cruxible_core.server.routes.feedback import router as feedback_router
from cruxible_core.server.routes.groups import router as groups_router
from cruxible_core.server.routes.hosted_instances import router as hosted_instances_router
from cruxible_core.server.routes.instances import router as instances_router
from cruxible_core.server.routes.mutations import router as mutations_router
from cruxible_core.server.routes.queries import router as queries_router
from cruxible_core.server.routes.runtime_credentials import (
    router as runtime_credentials_router,
)
from cruxible_core.server.routes.snapshots import router as snapshots_router
from cruxible_core.server.routes.source_artifacts import router as source_artifacts_router
from cruxible_core.server.routes.state import router as state_router
from cruxible_core.server.routes.workflows import router as workflows_router


def create_app() -> FastAPI:
    """Create and configure the Cruxible server app."""
    get_registry()
    app = FastAPI(title="cruxible-core", responses=STANDARD_ERROR_RESPONSES)
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
        errors = [
            f"{'.'.join(str(part) for part in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
            for err in exc.errors()
        ]
        body = ErrorResponse(
            error_type="RequestValidationError",
            message="Request validation failed",
            errors=errors,
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request.state.error_type = exc.__class__.__name__
        body = ErrorResponse(error_type=exc.__class__.__name__, message=str(exc))
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": __version__}

    app.include_router(instances_router)
    app.include_router(hosted_instances_router)
    app.include_router(state_router)
    app.include_router(queries_router)
    app.include_router(runtime_credentials_router)
    app.include_router(decision_records_router)
    app.include_router(mutations_router)
    app.include_router(feedback_router)
    app.include_router(groups_router)
    app.include_router(workflows_router)
    app.include_router(snapshots_router)
    app.include_router(source_artifacts_router)
    return app


def main() -> None:
    """Run the Cruxible server using UDS or host/port transport."""
    credential_store = get_runtime_credential_store()
    runtime_credentials_available = credential_store.has_active_credentials()
    auth_required = credential_store.is_auth_required()
    validate_server_startup_settings(
        runtime_credentials_available=runtime_credentials_available,
        auth_required=auth_required,
    )
    if is_server_auth_enabled():
        credential_store.mark_auth_required("server_startup_auth_enabled")

    import uvicorn

    configure_request_logging()
    init_permissions()
    app = create_app()

    socket_path = os.environ.get("CRUXIBLE_SERVER_SOCKET")
    if socket_path:
        socket_file = Path(socket_path)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        socket_file.unlink(missing_ok=True)
        uvicorn.run(app, uds=str(socket_file))
        return

    host = os.environ.get("CRUXIBLE_HOST", "127.0.0.1")
    port = int(os.environ.get("CRUXIBLE_PORT", "8100"))
    uvicorn.run(app, host=host, port=port)
