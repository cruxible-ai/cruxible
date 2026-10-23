"""Bound a Capture upload part's body before the framework buffers it.

FastAPI reads a route's whole body before the route runs, so a length check in
the handler comes after an unbounded allocation. This ASGI middleware sits
inside the auth middleware (a caller is authenticated before any byte is read)
and, on the part route alone, refuses a declared ``Content-Length`` above
``UPLOAD_PART_SIZE`` without reading the body, and otherwise reads the stream
itself, counting bytes, and refuses the moment the count passes the bound
(chunked bodies included). A body within the bound is replayed to the app
unchanged. No refusal carries body bytes.
"""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cruxible_client.contracts.uploads import UPLOAD_PART_SIZE
from cruxible_client.errors import ErrorResponse

UPLOAD_PART_ROUTE = re.compile(r"^/api/v1/[^/]+/uploads/[0-9a-f]{32}/parts/[0-9]+$")
PART_TOO_LARGE_CODE = "cruxible.upload.part_too_large"


def _too_large() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content=ErrorResponse(
            error_type="CaptureUploadRefused",
            message=f"An upload part carries at most {UPLOAD_PART_SIZE} bytes.",
            error_code=PART_TOO_LARGE_CODE,
        ).model_dump(mode="json"),
        headers={"connection": "close"},
    )


class UploadPartBodyLimit:
    """Refuse an upload part body above ``UPLOAD_PART_SIZE`` before it is buffered."""

    def __init__(self, app: ASGIApp, *, limit: int = UPLOAD_PART_SIZE) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or UPLOAD_PART_ROUTE.fullmatch(scope.get("path", "")) is None
        ):
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or ()).get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = self.limit + 1
            if length > self.limit:
                await _too_large()(scope, receive, send)
                return
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.limit:
                await _too_large()(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)


__all__ = ["PART_TOO_LARGE_CODE", "UPLOAD_PART_ROUTE", "UploadPartBodyLimit"]
