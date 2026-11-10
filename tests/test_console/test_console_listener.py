"""The `cruxible console` listener: session, origin and host checks, allow-list, forwarding."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from cruxible_core.console import bundle as bundle_module
from cruxible_core.console.bundle import (
    MANIFEST_NAME,
    ConsoleBundleInvalid,
    load_bundle,
    write_manifest,
)
from cruxible_core.console.listener import (
    SESSION_HEADER,
    SESSION_STORAGE_KEY,
    ConsoleListener,
    PortUnavailable,
    Upstream,
    bind_loopback,
)

PORT = 43210
ORIGIN = f"http://127.0.0.1:{PORT}"
INSTANCE = "inst_console"
BOOTSTRAP = "bootstrap-token-for-tests"
DAEMON_CREDENTIAL = "daemon-credential-never-in-the-page"
INDEX = b"<!doctype html><div id=root></div><script src=/assets/app.js></script>"


def _dist(root: Path) -> Path:
    dist = root / "console_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX)
    (dist / "assets" / "app.js").write_text("console.log('app')")
    write_manifest(
        dist,
        console_commit="c" * 40,
        console_pin={"core_version": "0.5.1", "sdk_contract_snapshot_digest": "sha256:pin"},
        pin_matches_core=True,
    )
    return dist


class Daemon:
    """A recording stand-in for the daemon behind the listener."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond or (
            lambda request: httpx.Response(200, json={"path": request.url.path})
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._respond(request)
        if not response.is_stream_consumed:
            return response
        # A Response built from bytes is read at construction; a wire response is
        # a stream, so hand the listener one.
        body = response.content

        async def stream() -> Any:
            yield body

        return httpx.Response(response.status_code, headers=response.headers, content=stream())


def _listener(tmp_path: Path, daemon: Any, *, upstream: Upstream | None = None) -> ConsoleListener:
    return ConsoleListener(
        bundle=load_bundle(_dist(tmp_path)),
        upstream=upstream
        or Upstream(
            label="unix socket /run/daemon.sock",
            socket_path="/run/daemon.sock",
            headers={"Authorization": f"Bearer {DAEMON_CREDENTIAL}"},
        ),
        instance_id=INSTANCE,
        port=PORT,
        bootstrap_token=BOOTSTRAP,
        core_identity={"version": "0.5.1", "sdk_contract_snapshot_digest": "sha256:core"},
        transport=None if daemon is None else httpx.MockTransport(daemon),
    )


def _browser(app: ConsoleListener) -> TestClient:
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def _stored_session(response: httpx.Response) -> str:
    """The session token the bootstrap page writes to localStorage."""

    assert response.status_code == 200, response.text
    found = re.search(
        r'localStorage\.setItem\("' + re.escape(SESSION_STORAGE_KEY) + r'","([A-Za-z0-9_-]+)"\)',
        response.text,
    )
    assert found, response.text
    return found.group(1)


def _signed_in(app: ConsoleListener) -> tuple[TestClient, str]:
    """A browser after the bootstrap link, sending its session header, and the token."""

    browser = _browser(app)
    session = _stored_session(browser.get(f"/__console/bootstrap?token={BOOTSTRAP}"))
    browser.headers[SESSION_HEADER] = session
    return browser, session


# -- static files ----------------------------------------------------------


def test_built_files_need_no_session_and_unknown_paths_fall_back_to_index(tmp_path: Path) -> None:
    browser = _browser(_listener(tmp_path, Daemon()))

    index = browser.get("/")
    assert index.status_code == 200 and index.content == INDEX
    assert index.headers["content-type"].startswith("text/html")
    assert browser.get("/assets/app.js").text == "console.log('app')"
    assert browser.get("/assets/app.js").headers["content-type"].startswith("text/javascript")
    # Client routes, including refs with dots and escaped slashes, are the app.
    for route in ("/i/inst_console", "/i/inst_console/s/project.work_item%2Fwi-42", "/x.json"):
        assert browser.get(route).content == INDEX, route
    assert browser.get("/assets/missing.js").status_code == 404
    assert browser.post("/").status_code == 405


def test_every_response_carries_the_hardening_headers_and_no_cors(tmp_path: Path) -> None:
    daemon = Daemon(
        lambda request: httpx.Response(
            200,
            json={},
            headers={
                "access-control-allow-origin": "*",
                "set-cookie": "daemon=1",
                "www-authenticate": "Bearer",
            },
        )
    )
    app = _listener(tmp_path, daemon)
    browser, _ = _signed_in(app)
    for response in (
        browser.get("/"),
        browser.get(f"/api/v1/{INSTANCE}/head"),
        browser.get("/__console/config"),
        browser.post(f"/api/v1/{INSTANCE}/set"),
    ):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert not [name for name in response.headers if name.startswith("access-control-")]
        assert "set-cookie" not in response.headers
        assert "www-authenticate" not in response.headers


def test_static_paths_cannot_leave_the_built_files(tmp_path: Path) -> None:
    app = _listener(tmp_path, Daemon())
    (tmp_path / "secret.txt").write_text("SECRET")
    # A file dropped into the build after its manifest is not served either.
    (app.bundle.root / "stray.txt").write_text("STRAY")
    browser = _browser(app)
    for path in (
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/stray.txt",
        f"/{MANIFEST_NAME}",
        "//etc/passwd",
    ):
        response = browser.get(path)
        assert b"SECRET" not in response.content and b"STRAY" not in response.content, path
        assert response.status_code in (200, 404) and response.content in (INDEX, b"Not found")


def test_a_manifest_naming_a_symlink_or_a_changed_file_is_refused(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    (dist / "assets" / "app.js").write_text("tampered")
    with pytest.raises(ConsoleBundleInvalid, match="does not match its hash"):
        load_bundle(dist)

    other = _dist(tmp_path / "second")
    (tmp_path / "outside.js").write_text("outside")
    (other / "assets" / "app.js").unlink()
    os.symlink(tmp_path / "outside.js", other / "assets" / "app.js")
    with pytest.raises(ConsoleBundleInvalid, match="not present"):
        load_bundle(other)


# -- session ---------------------------------------------------------------


def test_the_bootstrap_link_stores_a_session_once_and_sets_no_cookie(tmp_path: Path) -> None:
    app = _listener(tmp_path, Daemon())

    wrong = _browser(app).get("/__console/bootstrap?token=guess")
    assert wrong.status_code == 403 and "set-cookie" not in wrong.headers
    assert "localStorage" not in wrong.text

    first = _browser(app).get(f"/__console/bootstrap?token={BOOTSTRAP}")
    session = _stored_session(first)
    assert session != BOOTSTRAP and BOOTSTRAP not in first.text
    assert "set-cookie" not in first.headers
    assert f'location.replace("/i/{INSTANCE}")' in first.text
    assert first.headers["cache-control"] == "no-store"
    # The inline script runs only by this response's nonce.
    policies = first.headers.get_list("content-security-policy")
    [nonce_policy] = [policy for policy in policies if "script-src" in policy]
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", nonce_policy)
    assert nonce and f'<script nonce="{nonce.group(1)}">' in first.text
    assert "default-src 'none'" in nonce_policy
    # Built files get no script allowance.
    assert all(
        "script-src" not in p
        for p in _browser(app).get("/").headers.get_list("content-security-policy")
    )

    # Spent: the link works once.
    assert _browser(app).get(f"/__console/bootstrap?token={BOOTSTRAP}").status_code == 403


def test_the_bootstrap_page_escapes_what_it_embeds(tmp_path: Path) -> None:
    app = ConsoleListener(
        bundle=load_bundle(_dist(tmp_path)),
        upstream=Upstream(label="x", socket_path="/x"),
        instance_id='a</script><script>alert(1)</script>"',
        port=PORT,
        bootstrap_token=BOOTSTRAP,
        core_identity={},
    )
    page = _browser(app).get(f"/__console/bootstrap?token={BOOTSTRAP}").text
    assert page.count("<script") == 1 and page.count("</script>") == 1


def test_config_needs_the_session_and_names_instance_and_builds(tmp_path: Path) -> None:
    app = _listener(tmp_path, Daemon())
    assert _browser(app).get("/__console/config").status_code == 401

    browser, session = _signed_in(app)
    config = browser.get("/__console/config")
    assert config.status_code == 200
    assert config.headers["cache-control"] == "no-store"
    assert config.json() == {
        "tag": "cruxible-console-config-v1",
        "instance_id": INSTANCE,
        "api_base_url": "",
        "core": {"version": "0.5.1", "sdk_contract_snapshot_digest": "sha256:core"},
        "console": {
            "commit": "c" * 40,
            "pin": {"core_version": "0.5.1", "sdk_contract_snapshot_digest": "sha256:pin"},
        },
    }
    assert DAEMON_CREDENTIAL not in config.text and session not in config.text
    manifest = browser.get(f"/__console/{MANIFEST_NAME}").json()
    assert set(manifest["files"]) == {"index.html", "assets/app.js"}


def test_api_calls_without_the_session_never_reach_the_daemon(tmp_path: Path) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    _signed_in(app)  # a session exists; these browsers do not hold it

    anonymous = _browser(app)
    assert anonymous.get(f"/api/v1/{INSTANCE}/head").status_code == 401
    assert anonymous.get("/version").status_code == 401
    forged = _browser(app)
    forged.headers[SESSION_HEADER] = BOOTSTRAP  # the URL token is not the session
    assert forged.get(f"/api/v1/{INSTANCE}/head").status_code == 401
    assert forged.get("/__console/config").status_code == 401
    assert daemon.requests == []


def test_a_cookie_never_authenticates_even_holding_the_session_token(tmp_path: Path) -> None:
    """Cookies reach every server on 127.0.0.1, whatever its port: they are not a credential."""

    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    _, session = _signed_in(app)
    browser = _browser(app)
    for cookie in (
        f"{SESSION_HEADER}={session}",
        f"{SESSION_STORAGE_KEY}={session}",
        f"cruxible_console_{PORT}={session}",
        f"session={session}",
    ):
        for path in (f"/api/v1/{INSTANCE}/head", "/__console/config"):
            assert browser.get(path, headers={"cookie": cookie}).status_code == 401
    assert daemon.requests == []


def test_a_session_header_sent_twice_is_refused(tmp_path: Path) -> None:
    app = _listener(tmp_path, Daemon())
    _, session = _signed_in(app)
    scope_headers = [(SESSION_HEADER.encode(), session.encode())] * 2
    browser = _browser(app)
    response = browser.get("/__console/config", headers=httpx.Headers(scope_headers))
    assert response.status_code == 401


# -- host, origin ----------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [f"evil.example:{PORT}", "127.0.0.1:9", "127.0.0.1", f"0.0.0.0:{PORT}", f"[::1]:{PORT}"],
)
def test_requests_under_another_host_name_are_refused(tmp_path: Path, host: str) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    browser, _ = _signed_in(app)
    for path in ("/", f"/api/v1/{INSTANCE}/head", "/__console/config"):
        response = browser.get(path, headers={"host": host})
        assert response.status_code == 403, (host, path)
        assert response.json()["error_code"] == "console.host_refused"
    assert daemon.requests == []


def test_localhost_under_the_same_port_is_the_listener(tmp_path: Path) -> None:
    app = _listener(tmp_path, Daemon())
    browser, _ = _signed_in(app)
    assert browser.get("/", headers={"host": f"localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "http://evil.example"},
        {"origin": "null"},
        {"origin": f"http://127.0.0.1:{PORT + 1}"},
        {"origin": f"https://127.0.0.1:{PORT}"},
        {"sec-fetch-site": "cross-site"},
        # Another localhost port is the same *site* as this one.
        {"sec-fetch-site": "same-site"},
    ],
)
def test_cross_site_api_requests_are_refused(tmp_path: Path, headers: dict[str, str]) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    browser, _ = _signed_in(app)
    for method, path in (
        ("GET", f"/api/v1/{INSTANCE}/head"),
        ("POST", f"/api/v1/{INSTANCE}/get"),
        ("GET", "/__console/config"),
    ):
        response = browser.request(
            method, path, headers=headers, json={} if method == "POST" else None
        )
        assert response.status_code == 403, (headers, path)
        assert response.json()["error_code"] == "console.origin_refused"
    assert daemon.requests == []


def test_same_origin_requests_pass(tmp_path: Path) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    browser, _ = _signed_in(app)
    response = browser.post(
        f"/api/v1/{INSTANCE}/get",
        headers={"origin": ORIGIN, "sec-fetch-site": "same-origin"},
        json={"ref": "x"},
    )
    assert response.status_code == 200, response.text


# -- allow-list and forwarding ---------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", f"/api/v1/{INSTANCE}/lines/l/runs"),
        ("POST", f"/api/v1/{INSTANCE}/procedures/p/runs"),
        ("POST", f"/api/v1/{INSTANCE}/runbooks/r/runs"),
        ("POST", f"/api/v1/{INSTANCE}/lines/l/evaluate"),
        ("POST", f"/api/v1/{INSTANCE}/write"),
        ("POST", f"/api/v1/{INSTANCE}/proposals/p/approve"),
        ("GET", f"/api/v1/{INSTANCE}/get"),
        ("PUT", f"/api/v1/{INSTANCE}/head"),
        ("DELETE", f"/api/v1/{INSTANCE}/head"),
        ("OPTIONS", f"/api/v1/{INSTANCE}/head"),
        ("HEAD", f"/api/v1/{INSTANCE}/head"),
        ("GET", "/api/v1/server/info"),
        ("POST", "/api/v1/server/stop"),
        ("GET", f"/api/v1/{INSTANCE}/inbox/a%2Fack"),
        ("GET", "/__console/anything"),
    ],
)
def test_routes_off_the_allow_list_are_refused_before_the_daemon(
    tmp_path: Path, method: str, path: str
) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    browser, _ = _signed_in(app)
    response = browser.request(method, path)
    assert response.status_code == 403
    if method != "HEAD":
        assert response.json()["error_code"] == "console.route_refused"
    assert daemon.requests == []


def test_allowed_reads_carry_the_daemon_credential_and_nothing_of_the_browser(
    tmp_path: Path,
) -> None:
    daemon = Daemon()
    app = _listener(tmp_path, daemon)
    browser, session = _signed_in(app)
    browser.cookies.set("other_app", "other-cookie")
    assert browser.headers[SESSION_HEADER] == session
    response = browser.post(
        f"/api/v1/{INSTANCE}/query?limit=5&cursor=a%2Fb",
        json={"kind": "Trigger"},
        headers={
            "authorization": "Bearer browser-supplied",
            "x-cruxible-principal-id": "mallory",
            "origin": ORIGIN,
            "referer": f"{ORIGIN}/i/{INSTANCE}",
            "x-forwarded-for": "10.0.0.1",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    [sent] = daemon.requests
    assert sent.method == "POST"
    assert sent.url.path == f"/api/v1/{INSTANCE}/query"
    assert sent.url.query == b"limit=5&cursor=a%2Fb"
    assert sent.content == b'{"kind":"Trigger"}'
    assert sent.headers["authorization"] == f"Bearer {DAEMON_CREDENTIAL}"
    assert sent.headers["content-type"] == "application/json"
    for name in (
        "cookie",
        SESSION_HEADER,
        "origin",
        "referer",
        "x-cruxible-principal-id",
        "x-forwarded-for",
    ):
        assert name not in sent.headers, name
    wire = repr(dict(sent.headers)) + str(sent.url) + sent.content.decode()
    for secret in (session, BOOTSTRAP, "other-cookie", "browser-supplied"):
        assert secret not in wire
    # And the daemon credential never comes back to the page.
    assert DAEMON_CREDENTIAL not in response.text + repr(dict(response.headers))


def test_version_is_forwarded_beside_the_api(tmp_path: Path) -> None:
    daemon = Daemon(lambda request: httpx.Response(200, json={"version": "0.5.1"}))
    browser, _ = _signed_in(_listener(tmp_path, daemon))
    assert browser.get("/version").json() == {"version": "0.5.1"}
    assert [r.url.path for r in daemon.requests] == ["/version"]


def test_daemon_errors_pass_through_with_their_envelope(tmp_path: Path) -> None:
    envelope = {"error_type": "NotFound", "message": "no such ref", "error_code": "ref.unknown"}
    daemon = Daemon(lambda request: httpx.Response(404, json=envelope))
    browser, _ = _signed_in(_listener(tmp_path, daemon))
    response = browser.post(f"/api/v1/{INSTANCE}/get", json={"ref": "x"})
    assert response.status_code == 404 and response.json() == envelope


def test_an_oversized_request_body_is_refused_unsent(tmp_path: Path) -> None:
    daemon = Daemon()
    browser, _ = _signed_in(_listener(tmp_path, daemon))
    response = browser.post(f"/api/v1/{INSTANCE}/get", content=b"x" * ((1 << 20) + 1))
    assert response.status_code == 413
    assert daemon.requests == []


def test_a_daemon_that_is_not_running_answers_a_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "no-daemon.sock"
    app = _listener(
        tmp_path,
        None,
        upstream=Upstream(
            label=f"unix socket {missing}",
            socket_path=str(missing),
            start_hint=f"start it with `cruxible server start --socket {missing}`",
        ),
    )
    browser, _ = _signed_in(app)
    response = browser.get(f"/api/v1/{INSTANCE}/head")
    assert response.status_code == 502
    body = response.json()
    assert body["error_code"] == "console.daemon_unreachable"
    assert str(missing) in body["message"] and "cruxible server start" in body["message"]


def test_a_daemon_that_does_not_answer_in_time_is_a_504(tmp_path: Path) -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    browser, _ = _signed_in(_listener(tmp_path, Daemon(stall)))
    response = browser.get(f"/api/v1/{INSTANCE}/head")
    assert response.status_code == 504
    assert response.json()["error_code"] == "console.daemon_timeout"


def test_large_responses_stream_through_chunk_by_chunk(tmp_path: Path) -> None:
    """The first chunk reaches the browser before the daemon has produced the rest."""

    chunk = b"b" * (1 << 20)
    chunks = 48
    relayed = asyncio.Event()

    async def body() -> Any:
        yield b"a" * 1024
        # Only a listener that relays as it reads gets past this point.
        await asyncio.wait_for(relayed.wait(), timeout=10)
        for _ in range(chunks):
            yield chunk

    app = _listener(
        tmp_path,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/json"}, content=body()
        ),
    )
    _, session = _signed_in(app)
    sizes: list[int] = []
    started: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        if message["type"] == "http.response.start":
            started.append(message["status"])
        elif message["type"] == "http.response.body" and message.get("body"):
            sizes.append(len(message["body"]))
            relayed.set()

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/v1/{INSTANCE}/head",
        "raw_path": f"/api/v1/{INSTANCE}/head".encode(),
        "query_string": b"",
        "headers": [
            (b"host", f"127.0.0.1:{PORT}".encode()),
            (SESSION_HEADER.encode(), session.encode()),
        ],
    }

    async def run() -> None:
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=60)
        finally:
            await app.aclose()

    asyncio.run(run())
    assert started == [200]
    assert sum(sizes) == 1024 + chunks * len(chunk)
    assert len(sizes) > chunks // 2  # relayed in pieces, not as one buffered body


# -- binding and the CLI ---------------------------------------------------


def test_port_zero_binds_a_free_loopback_port_and_a_taken_port_says_so() -> None:
    sock = bind_loopback(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0
        with pytest.raises(PortUnavailable, match=f"127.0.0.1:{port}: it is already in use"):
            bind_loopback(port)
    finally:
        sock.close()


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1"])
def test_a_port_another_service_listens_on_is_refused(address: str) -> None:
    holder = socket.socket()
    holder.bind((address, 0))
    holder.listen(1)
    try:
        port = holder.getsockname()[1]
        with pytest.raises(PortUnavailable, match="already in use"):
            bind_loopback(port)
    finally:
        holder.close()


def test_a_fixed_port_rebinds_right_after_a_run_that_served_connections(tmp_path: Path) -> None:
    """A served keep-alive connection leaves TIME_WAIT behind; --port N must still restart."""

    import uvicorn

    sock = bind_loopback(0)
    port = sock.getsockname()[1]
    app = ConsoleListener(
        bundle=load_bundle(_dist(tmp_path)),
        upstream=Upstream(label="x", socket_path="/x"),
        instance_id=INSTANCE,
        port=port,
        bootstrap_token=BOOTSTRAP,
        core_identity={},
    )
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, log_level="critical", lifespan="on")
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    with httpx.Client() as client:
        for _ in range(3):
            assert client.get(f"http://127.0.0.1:{port}/").status_code == 200
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
    sock.close()

    again = bind_loopback(port)
    again.close()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in (
        "CRUXIBLE_SERVER_BEARER_TOKEN",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_PRINCIPAL_ID",
        "CRUXIBLE_PRINCIPAL_KEY",
        "CRUXIBLE_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_cli(*args: str) -> Any:
    from cruxible_core.cli.main import cli

    return CliRunner().invoke(cli, ["--no-workspace", *args])


def test_without_a_built_console_the_command_says_so_plainly(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = cli_env / "empty_dist"
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: empty)
    result = _run_cli("--server-socket", str(cli_env / "d.sock"), "--instance-id", "i", "console")
    assert result.exit_code == 1
    assert "This install has no built console" in result.output
    assert "scripts/build_console.py" in result.output and "pnpm dev" in result.output


def test_without_a_daemon_the_command_says_how_to_start_one(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = _dist(cli_env)
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: dist)

    nothing = _run_cli("--instance-id", "i", "console", "--no-open")
    assert nothing.exit_code != 0
    assert "cruxible console needs server mode" in nothing.output
    assert "cruxible server start" in nothing.output

    sock = cli_env / "down.sock"
    down = _run_cli("--server-socket", str(sock), "--instance-id", "i", "console", "--no-open")
    assert down.exit_code == 1
    assert f"the Cruxible daemon at unix socket {sock} is not answering" in down.output
    assert f"cruxible server start --socket {sock}" in down.output


def test_without_an_instance_the_command_asks_for_one(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = _dist(cli_env)
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: dist)
    result = _run_cli("--server-socket", str(cli_env / "d.sock"), "console", "--no-open")
    assert result.exit_code != 0
    assert "needs an instance" in result.output


def test_a_taken_port_is_a_clear_cli_error(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cruxible_core.cli.commands import console as console_command

    dist = _dist(cli_env)
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: dist)
    monkeypatch.setattr(console_command, "_probe", lambda *args, **kwargs: None)
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        port = holder.getsockname()[1]
        result = _run_cli(
            "--server-socket",
            str(cli_env / "d.sock"),
            "--instance-id",
            "i",
            "console",
            "--no-open",
            "--port",
            str(port),
        )
    finally:
        holder.close()
    assert result.exit_code == 1
    assert f"cannot listen on 127.0.0.1:{port}: it is already in use" in result.output
