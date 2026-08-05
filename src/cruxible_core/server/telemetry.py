"""HTTP boundary telemetry over already-serialized response chunks."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request, Response

from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.registry import GOVERNED_DAEMON_BACKEND, get_registry
from cruxible_core.telemetry.instrumentation import record_boundary

# The instance-scope refusal. A caller not entitled to address this instance
# did not generate that instance's traffic, so the call is not counted against
# it at all rather than counted under a route it was never allowed to reach.
_SCOPE_REFUSED_STATUS = 403


async def boundary_telemetry_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Measure a routed response without re-serializing or retaining its body."""
    started = time.perf_counter_ns()
    try:
        response = await call_next(request)
    except BaseException:
        _record_http_boundary(
            request,
            response_bytes=0,
            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
            error=True,
            status_code=None,
        )
        raise

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        body = getattr(response, "body", b"")
        response_bytes = len(body) if isinstance(body, bytes) else 0
        _record_http_boundary(
            request,
            response_bytes=response_bytes,
            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
            error=response.status_code >= 400,
            status_code=response.status_code,
        )
        return response

    async def counted_body() -> AsyncIterator[Any]:
        response_bytes = 0
        failed = response.status_code >= 400
        try:
            async for chunk in body_iterator:
                # Counting must never abort the response. A chunk whose type or
                # encoding this cannot measure contributes zero bytes and the
                # body streams on unchanged.
                try:
                    response_bytes += (
                        len(chunk.encode(response.charset or "utf-8"))
                        if isinstance(chunk, str)
                        else len(chunk)
                    )
                except Exception:
                    pass
                yield chunk
        except BaseException:
            failed = True
            raise
        finally:
            _record_http_boundary(
                request,
                response_bytes=response_bytes,
                duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
                error=failed,
                status_code=response.status_code,
            )

    setattr(response, "body_iterator", counted_body())
    return response


def _record_http_boundary(
    request: Request,
    *,
    response_bytes: int,
    duration_ms: float,
    error: bool,
    status_code: int | None,
) -> None:
    """Attribute one response to an instance, or to nothing at all.

    Resolution mirrors ``resolve_server_instance_id``, which is what the routes
    themselves accept: a registered instance on the governed daemon backend.
    The middleware must never resolve more broadly than the route it observes —
    ``InstanceManager.get`` falls back to ``CruxibleInstance.load(Path(...))``
    for dev-mode local instances, which here would mean touching the filesystem
    at a raw, attacker-controlled path segment on a request the router rejects.

    The whole body is guarded: capture may drop an observation, never a
    response.
    """
    try:
        route = request.scope.get("route")
        surface_name = getattr(route, "name", None)
        instance_id = request.path_params.get("instance_id")
        if not isinstance(surface_name, str) or not isinstance(instance_id, str):
            return
        if status_code == _SCOPE_REFUSED_STATUS:
            return
        record = get_registry().get(instance_id)
        if record is None or record.backend != GOVERNED_DAEMON_BACKEND:
            return
        instance = get_manager().get(instance_id)
        record_boundary(
            instance,
            surface_name,
            response_bytes=response_bytes,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception:
        return


__all__ = ["boundary_telemetry_middleware"]
