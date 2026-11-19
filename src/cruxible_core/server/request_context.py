"""Mark HTTP requests through response completion, including offloaded workers.

Blocking floor admission is forbidden on request paths regardless of how a
route reaches it. Context propagation into the worker pool makes that rule
checkable at the acquisition rather than through a static call graph.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from cruxible_core.runtime.admission import HTTP_REQUEST_CONTEXT


class HTTPRequestContextMiddleware:
    """Keep the HTTP marker separate from authentication and lifespan tasks."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = HTTP_REQUEST_CONTEXT.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            HTTP_REQUEST_CONTEXT.reset(token)
