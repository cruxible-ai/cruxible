"""HTTP boundary telemetry over already-serialized response chunks."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request, Response

from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.telemetry.instrumentation import record_boundary


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
        )
        return response

    async def counted_body() -> AsyncIterator[Any]:
        response_bytes = 0
        failed = response.status_code >= 400
        try:
            async for chunk in body_iterator:
                response_bytes += (
                    len(chunk.encode(response.charset or "utf-8"))
                    if isinstance(chunk, str)
                    else len(chunk)
                )
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
            )

    setattr(response, "body_iterator", counted_body())
    return response


def _record_http_boundary(
    request: Request,
    *,
    response_bytes: int,
    duration_ms: float,
    error: bool,
) -> None:
    route = request.scope.get("route")
    surface_name = getattr(route, "name", None)
    instance_id = request.path_params.get("instance_id")
    if not isinstance(surface_name, str) or not isinstance(instance_id, str):
        return
    try:
        instance = get_manager().get(instance_id)
    except Exception:
        return
    record_boundary(
        instance,
        surface_name,
        response_bytes=response_bytes,
        duration_ms=duration_ms,
        error=error,
    )


__all__ = ["boundary_telemetry_middleware"]
