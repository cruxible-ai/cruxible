"""A real browser, the console listener and a second local web server.

Cookies are scoped to the host, not the port, so a cookie session would reach
any other server on 127.0.0.1 the browser visits. The session lives in the
listener origin's localStorage and travels in a request header instead. This
drives headless Chromium (the console repository's Playwright) through the
bootstrap link, a second tab and a visit to another local server, and checks
that the other server receives nothing it can replay.

Runs when ``CRUXIBLE_CONSOLE_PLAYWRIGHT_DIR`` names a directory whose
``node_modules`` holds ``@playwright/test`` with Chromium installed (a
cruxible-app checkout after ``pnpm install``); skipped otherwise.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from cruxible_core.console.bundle import load_bundle, write_manifest
from cruxible_core.console.listener import (
    SESSION_HEADER,
    SESSION_STORAGE_KEY,
    ConsoleListener,
    Upstream,
    bind_loopback,
)

PLAYWRIGHT_DIR = os.environ.get("CRUXIBLE_CONSOLE_PLAYWRIGHT_DIR")
INSTANCE = "inst_browser"
CANARY = b'{"canary":"instance-data"}'

_DRIVER = r"""
import { createRequire } from "node:module";
const require = createRequire(process.env.PLAYWRIGHT_PACKAGE_JSON);
const { chromium } = require("@playwright/test");
const config = JSON.parse(process.argv[2]);
const browser = await chromium.launch();
const out = {};
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto(config.bootstrap);
  await page.waitForURL((url) => url.pathname === config.landing);
  out.landing = new URL(page.url()).pathname + new URL(page.url()).search;
  out.token = await page.evaluate((key) => localStorage.getItem(key), config.key);
  out.cookies = (await context.cookies()).length;
  // Runs in the page: one GET, with or without the stored session header.
  const read = async ([url, key, name, withHeader]) => {
    const headers = withHeader ? { [name]: localStorage.getItem(key) } : {};
    return (await fetch(url, { headers })).status;
  };
  const args = (withHeader) => [config.read, config.key, config.header, withHeader];
  out.pageRead = await page.evaluate(read, args(true));
  out.pageReadWithoutHeader = await page.evaluate(read, args(false));
  const tab = await context.newPage();
  await tab.goto(config.origin + config.landing);
  out.secondTabRead = await tab.evaluate(read, args(true));
  // The other local server, in the same browser.
  await page.goto(config.attacker);
  out.attackerStorage = await page.evaluate((key) => localStorage.getItem(key), config.key);
  out.attackerFetch = await page.evaluate(async ([url, name]) => {
    try {
      const response = await fetch(url, { headers: { [name]: "guess" } });
      return { readable: true, status: response.status };
    } catch { return { readable: false }; }
  }, [config.read, config.header]);
  out.attackerPlainFetch = await page.evaluate(async (url) => {
    try {
      const response = await fetch(url, { credentials: "include" });
      return { readable: true, status: response.status };
    } catch { return { readable: false }; }
  }, config.read);
} finally {
  await browser.close();
}
console.log(JSON.stringify(out));
"""


class _OtherServer(http.server.BaseHTTPRequestHandler):
    received: list[dict[str, str]] = []

    def log_message(self, *_args: Any) -> None:
        pass

    def do_GET(self) -> None:
        type(self).received.append({key.lower(): value for key, value in self.headers.items()})
        body = b"<!doctype html><title>another local web server</title>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def listener(tmp_path: Path) -> Iterator[tuple[str, str, list[httpx.Request]]]:
    import uvicorn

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>console fixture</title>")
    write_manifest(dist, console_commit="c" * 40, console_pin={})
    sock = bind_loopback(0)
    port = sock.getsockname()[1]
    seen: list[httpx.Request] = []

    def daemon(request: httpx.Request) -> httpx.Response:
        seen.append(request)

        async def body() -> Any:
            yield CANARY

        return httpx.Response(200, headers={"content-type": "application/json"}, content=body())

    bootstrap = "browser-bootstrap-token"
    app = ConsoleListener(
        bundle=load_bundle(dist),
        upstream=Upstream(label="fake daemon", headers={"Authorization": "Bearer fake"}),
        instance_id=INSTANCE,
        port=port,
        bootstrap_token=bootstrap,
        core_identity={},
        transport=httpx.MockTransport(daemon),
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
    try:
        yield f"http://127.0.0.1:{port}", bootstrap, seen
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.mark.skipif(
    not PLAYWRIGHT_DIR or shutil.which("node") is None,
    reason="set CRUXIBLE_CONSOLE_PLAYWRIGHT_DIR to a directory with @playwright/test installed",
)
def test_another_local_server_the_browser_visits_receives_nothing_replayable(
    listener: tuple[str, str, list[httpx.Request]], tmp_path: Path
) -> None:
    origin, bootstrap, seen = listener
    _OtherServer.received = []
    other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OtherServer)
    other_thread = threading.Thread(target=other.serve_forever, daemon=True)
    other_thread.start()
    read_url = f"{origin}/api/v1/{INSTANCE}/head"
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER)
    try:
        completed = subprocess.run(
            [
                "node",
                str(driver),
                json.dumps(
                    {
                        "bootstrap": f"{origin}/__console/bootstrap?token={bootstrap}",
                        "origin": origin,
                        "landing": f"/i/{INSTANCE}",
                        "read": read_url,
                        "key": SESSION_STORAGE_KEY,
                        "header": SESSION_HEADER,
                        "attacker": f"http://127.0.0.1:{other.server_port}/",
                    }
                ),
            ],
            env={
                **os.environ,
                "PLAYWRIGHT_PACKAGE_JSON": str(Path(str(PLAYWRIGHT_DIR)) / "package.json"),
            },
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    finally:
        other.shutdown()
        other.server_close()
        other_thread.join(timeout=5)
    assert completed.returncode == 0, completed.stderr
    out = json.loads(completed.stdout.strip().splitlines()[-1])
    token = out["token"]

    # The listener's own page holds the session, under its origin only.
    assert out["landing"] == f"/i/{INSTANCE}"  # the one-time link left no trace in the URL
    assert token and token != bootstrap
    assert out["cookies"] == 0
    assert out["pageRead"] == 200 and out["secondTabRead"] == 200
    assert out["pageReadWithoutHeader"] == 401

    # The other local server got no credential, cannot read the listener's
    # storage, and its page cannot call the listener.
    assert _OtherServer.received, "the browser visited the other server"
    for headers in _OtherServer.received:
        assert "cookie" not in headers
        assert all(token not in value and bootstrap not in value for value in headers.values())
    assert out["attackerStorage"] is None
    assert out["attackerFetch"] == {"readable": False}
    assert out["attackerPlainFetch"] == {"readable": False}

    # Replaying everything it received, from its own HTTP client, is refused.
    for headers in _OtherServer.received:
        replay = {k: v for k, v in headers.items() if k not in ("host", "content-length")}
        assert httpx.get(read_url, headers=replay).status_code == 401
    # Exactly the two signed-in reads reached the daemon.
    assert len(seen) == 2
    assert all(SESSION_HEADER not in request.headers for request in seen)
