"""The loopback listener behind `cruxible console`: built files, one config read, read routes.

One ASGI app, no framework routing, so every request takes the same checks in
the same order:

1. **Host** must be ``127.0.0.1:<port>`` or ``localhost:<port>`` (a page on a
   rebound DNS name cannot reach the listener under its own origin).
2. **Origin**, when a browser sends one, must be the listener's own, and
   ``Sec-Fetch-Site``, when sent, must not say cross-site or same-site (another
   localhost port is the same *site* as this one). No CORS headers are ever
   sent, so no other origin's page can send the session header (a custom
   header forces a preflight the listener never approves).
3. **Session**: ``/__console/bootstrap?token=…`` takes the per-run token the
   opened URL carries, once, and answers a tiny page that stores a second,
   never-printed session token in ``localStorage`` and replaces itself with
   the clean ``/i/<instance>`` URL. Web storage is scoped to the full origin,
   port included; a cookie is not (every server on 127.0.0.1 would receive
   it), so no cookie is set and cookies are never read. The config endpoint
   and every API call need the token in the ``X-Cruxible-Console-Session``
   header; built files do not (they are public code).
4. **Allow-list**: an API call that is not one of the console's read routes
   (``routes.READ_ROUTES``) is refused with 403 here and never reaches the
   daemon.
5. **Forwarding**: an allowed call goes to the daemon with only its content
   headers and the daemon credential the CLI context resolved; the browser's
   cookies, the session token and its other headers stay here. The response
   streams back chunk by chunk, never buffered whole.
"""

from __future__ import annotations

import errno
import hmac
import json
import mimetypes
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote

import httpx
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from cruxible_core.console.bundle import MANIFEST_NAME, ConsoleBundle
from cruxible_core.console.routes import is_read_route

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

BOOTSTRAP_PATH = "/__console/bootstrap"
CONFIG_PATH = "/__console/config"
MANIFEST_PATH = f"/__console/{MANIFEST_NAME}"
CONFIG_TAG = "cruxible-console-config-v1"
#: The request header that carries the session token, and the localStorage key
#: the bootstrap page stores it under (the console reads both names).
SESSION_HEADER = "x-cruxible-console-session"
SESSION_STORAGE_KEY = "cruxible.console.session"

#: The console's request bodies are small JSON reads; anything bigger is refused unread.
MAX_REQUEST_BODY_BYTES = 1 << 20

#: Request headers the daemon receives from the page; nothing else crosses.
_FORWARDED_REQUEST_HEADERS = frozenset({"accept", "content-type"})
#: Response headers the page receives from the daemon.
_FORWARDED_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-length", "content-encoding", "etag", "last-modified"}
)
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("content-security-policy", "frame-ancestors 'none'"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
)
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json",
}


@dataclass(frozen=True)
class Upstream:
    """The daemon the console reads through, and the credential it is called with."""

    #: ``unix socket PATH`` or the daemon URL: what error messages name.
    label: str
    socket_path: str | None = None
    base_url: str | None = None
    #: Authorization / principal headers, exactly as the CLI's own client sends them.
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: Repair named when the daemon does not answer.
    start_hint: str = "start it with `cruxible server start`"

    def client(self, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
        """A client that streams to the daemon; ``transport`` replaces the wire in tests."""

        if transport is None and self.socket_path is not None:
            transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        return httpx.AsyncClient(
            base_url=self.base_url or "http://cruxible",
            headers=dict(self.headers),
            transport=transport,
            # Reads that rebuild an old generation's projection can take minutes.
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=60.0, pool=5.0),
            follow_redirects=False,
        )


def _error(status: int, error_type: str, code: str, message: str) -> JSONResponse:
    """The daemon's error envelope shape, so the console renders the listener's refusals."""

    return JSONResponse(
        {"error_type": error_type, "message": message, "error_code": code},
        status_code=status,
        headers={"cache-control": "no-store"},
    )


def _send_with_security_headers(send: Send, extra: Iterable[tuple[str, str]] = ()) -> Send:
    extra = tuple(extra)

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            for name, value in (*_SECURITY_HEADERS, *extra):
                headers.append((name.encode("latin-1"), value.encode("latin-1")))
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


class ConsoleListener:
    """The ASGI app `cruxible console` serves on ``127.0.0.1:<port>``."""

    def __init__(
        self,
        *,
        bundle: ConsoleBundle,
        upstream: Upstream,
        instance_id: str,
        port: int,
        bootstrap_token: str,
        core_identity: Mapping[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.bundle = bundle
        self.upstream = upstream
        self.instance_id = instance_id
        self.port = port
        self._bootstrap_token = bootstrap_token
        self._bootstrap_spent = False
        #: Never printed and never in a URL: the bootstrap page hands it to the
        #: origin's localStorage, and the page sends it in SESSION_HEADER.
        self._session_token = secrets.token_urlsafe(32)
        self._hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self._origins = frozenset(f"http://{host}" for host in self._hosts)
        self._core_identity = dict(core_identity)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    # -- ASGI -------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return  # no websockets
        await self._handle(scope, receive, _send_with_security_headers(send))

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _upstream_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self.upstream.client(self._transport)
        return self._client

    # -- checks -----------------------------------------------------------

    @staticmethod
    def _headers(scope: Scope, name: str) -> list[str]:
        wanted = name.encode("latin-1")
        return [
            bytes(value).decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.lower() == wanted
        ]

    @classmethod
    def _single(cls, scope: Scope, name: str) -> str | None:
        """One header's value; None when absent. Sent twice, it matches nothing allowed."""

        values = cls._headers(scope, name)
        if not values:
            return None
        return values[0] if len(values) == 1 else "\x00sent-twice"

    def _has_session(self, scope: Scope) -> bool:
        """Whether the request carries this run's session token in the session header.

        Cookies are never consulted: they are scoped to the host, not the port.
        """

        offered = self._single(scope, SESSION_HEADER)
        return offered is not None and hmac.compare_digest(
            offered.encode("latin-1"), self._session_token.encode()
        )

    def _refusal(self, scope: Scope, *, api: bool) -> Response | None:
        """Why this request may not proceed (Host, Origin, fetch site), else None."""

        host = self._single(scope, "host")
        if host not in self._hosts:
            return _error(
                403,
                "ConsoleHostRefused",
                "console.host_refused",
                f"The console answers only at http://127.0.0.1:{self.port}.",
            )
        origin = self._single(scope, "origin")
        if origin is not None and origin not in self._origins:
            return _error(
                403,
                "ConsoleOriginRefused",
                "console.origin_refused",
                "The console refuses requests from other sites.",
            )
        fetch_site = self._single(scope, "sec-fetch-site")
        if api and fetch_site is not None and fetch_site not in _ALLOWED_FETCH_SITES:
            return _error(
                403,
                "ConsoleOriginRefused",
                "console.origin_refused",
                "The console refuses requests from other sites.",
            )
        return None

    # -- dispatch ---------------------------------------------------------

    async def _handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
        path = path.split("?", 1)[0]
        method: str = scope["method"].upper()
        api = path == "/version" or path == "/api" or path.startswith(("/api/", "/__console/"))
        refusal = self._refusal(scope, api=api)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        if path == BOOTSTRAP_PATH and method == "GET":
            await self._bootstrap(scope)(scope, receive, send)
        elif path == CONFIG_PATH and method == "GET":
            await self._config(scope)(scope, receive, send)
        elif path == MANIFEST_PATH and method in ("GET", "HEAD"):
            await JSONResponse(dict(self.bundle.manifest))(scope, receive, send)
        elif api:
            await self._api(scope, receive, send, method=method, path=path)
        elif method in ("GET", "HEAD"):
            await self._static(path)(scope, receive, send)
        else:
            await _error(
                405, "ConsoleMethodRefused", "console.method_refused", "Method not allowed."
            )(scope, receive, send)

    def _bootstrap(self, scope: Scope) -> Response:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        offered = (query.get("token") or [""])[0]
        fresh = not self._bootstrap_spent and hmac.compare_digest(
            offered.encode("latin-1"), self._bootstrap_token.encode()
        )
        if not fresh:
            return HTMLResponse(
                "<!doctype html><title>Cruxible console</title>"
                "<p>This console link was already used, or is not this run's link. "
                "Run <code>cruxible console</code> again for a new one.</p>",
                status_code=403,
                headers={"cache-control": "no-store"},
            )
        self._bootstrap_spent = True
        nonce = secrets.token_urlsafe(16)
        landing = f"/i/{quote(self.instance_id, safe='')}"

        def js(value: str) -> str:
            return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")

        script = (
            "try{"
            f"localStorage.setItem({js(SESSION_STORAGE_KEY)},{js(self._session_token)});"
            f"location.replace({js(landing)});"
            "}catch(e){document.body.textContent="
            f"{js('This browser would not store the console session (is site storage blocked?).')}"
            "}"
        )
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Cruxible console</title><body>"
            f'<script nonce="{nonce}">{script}</script>'
            "<noscript>The console needs JavaScript.</noscript>",
            headers={
                "cache-control": "no-store",
                # Only this response may run an inline script, and only this one.
                "content-security-policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; base-uri 'none'; "
                    "form-action 'none'; frame-ancestors 'none'"
                ),
            },
        )

    def _config(self, scope: Scope) -> Response:
        if not self._has_session(scope):
            return self._no_session()
        manifest = self.bundle.manifest
        return JSONResponse(
            {
                "tag": CONFIG_TAG,
                "instance_id": self.instance_id,
                "api_base_url": "",
                "core": self._core_identity,
                "console": {
                    "commit": manifest.get("console_commit"),
                    "pin": manifest.get("console_pin"),
                },
            },
            headers={"cache-control": "no-store"},
        )

    def _no_session(self) -> Response:
        return _error(
            401,
            "ConsoleSessionRequired",
            "console.session_required",
            "Open the link `cruxible console` printed; it signs this browser in for this run.",
        )

    def _static(self, path: str) -> Response:
        name = path.lstrip("/") or "index.html"
        file = self.bundle.files.get(name)
        if file is None:
            if name.startswith("assets/") or name in ("favicon.ico", "robots.txt"):
                return Response("Not found", status_code=404, media_type="text/plain")
            # Single-page app: every other path is a client route.
            name, file = "index.html", self.bundle.files["index.html"]
        suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        media_type = _MEDIA_TYPES.get(suffix) or mimetypes.guess_type(name)[0]
        cache = "public, max-age=31536000, immutable" if name.startswith("assets/") else "no-cache"
        return FileResponse(file, media_type=media_type, headers={"cache-control": cache})

    # -- forwarding -------------------------------------------------------

    async def _api(
        self, scope: Scope, receive: Receive, send: Send, *, method: str, path: str
    ) -> None:
        if not self._has_session(scope):
            await self._no_session()(scope, receive, send)
            return
        if not is_read_route(method, path):
            await _error(
                403,
                "ConsoleRouteRefused",
                "console.route_refused",
                f"The console is read-only: {method} {path} is not one of its read routes.",
            )(scope, receive, send)
            return
        body = await self._read_body(receive)
        if body is None:
            await _error(
                413,
                "ConsoleRequestTooLarge",
                "console.request_too_large",
                f"A console request body is at most {MAX_REQUEST_BODY_BYTES} bytes.",
            )(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key.decode("latin-1").lower() in _FORWARDED_REQUEST_HEADERS
        }
        query = scope.get("query_string", b"").decode("latin-1")
        client = self._upstream_client()
        request = client.build_request(
            method, path + (f"?{query}" if query else ""), headers=headers, content=body
        )
        try:
            upstream = await client.send(request, stream=True)
        except httpx.TimeoutException:
            await _error(
                504,
                "DaemonTimeout",
                "console.daemon_timeout",
                f"The Cruxible daemon at {self.upstream.label} did not answer in time.",
            )(scope, receive, send)
            return
        except httpx.TransportError as exc:
            await _error(
                502,
                "DaemonUnreachable",
                "console.daemon_unreachable",
                f"The Cruxible daemon at {self.upstream.label} is not answering "
                f"({exc.__class__.__name__}); {self.upstream.start_hint}.",
            )(scope, receive, send)
            return
        try:
            response_headers = [
                (key.encode("latin-1"), value.encode("latin-1"))
                for key, value in upstream.headers.multi_items()
                if key.lower() in _FORWARDED_RESPONSE_HEADERS
            ]
            response_headers.append((b"cache-control", b"no-store"))
            await send(
                {
                    "type": "http.response.start",
                    "status": upstream.status_code,
                    "headers": response_headers,
                }
            )
            async for chunk in upstream.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await upstream.aclose()

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST_BODY_BYTES:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)


class PortUnavailable(Exception):
    """The requested loopback port could not be bound."""


def _something_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def bind_loopback(port: int) -> socket.socket:
    """A listening TCP socket on ``127.0.0.1:port`` (``0`` picks a free port).

    SO_REUSEADDR lets a fixed ``--port`` restart while the previous run's
    connections sit in TIME_WAIT. On BSD/macOS it would also let this socket
    shadow another service listening on the wildcard address, so a port where
    something already answers is refused before binding.
    """

    def unavailable(reason: str) -> PortUnavailable:
        return PortUnavailable(
            f"cannot listen on 127.0.0.1:{port}: {reason}. Pass --port 0 for a free "
            "port, or another --port."
        )

    if port and _something_answers(port):
        raise unavailable("it is already in use")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        reason = (
            "it is already in use" if exc.errno == errno.EADDRINUSE else (exc.strerror or str(exc))
        )
        raise unavailable(reason) from exc
    return sock


def serve(app: ConsoleListener, sock: socket.socket) -> None:
    """Serve ``app`` on the bound socket in the foreground until Ctrl-C."""

    import uvicorn

    config = uvicorn.Config(
        app,
        # The bootstrap URL carries the one-time token: no access log prints it.
        access_log=False,
        log_level="warning",
        lifespan="on",
        server_header=False,
        proxy_headers=False,
    )
    uvicorn.Server(config).run(sockets=[sock])
