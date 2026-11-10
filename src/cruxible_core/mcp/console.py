"""`cruxible_console`: the read-only console for the MCP workspace's instance, by tool.

The same loopback listener `cruxible console` serves (`cruxible_core.console`),
run on a background thread of this MCP server process: at most one per
instance, gone when the process exits (a daemon thread), stopped on request.
The tool opens the browser on the one-time bootstrap link itself and returns
only the clean address, so the token never enters the model's conversation;
only when no browser can be opened does it return the link, saying so.
"""

from __future__ import annotations

import atexit
import secrets
import threading
import webbrowser
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from cruxible_client.contracts.principals import PRINCIPAL_ID_HEADER
from cruxible_core.errors import ConfigError
from cruxible_core.mcp.daemon import DaemonTarget
from cruxible_core.mcp.results import McpConsoleResult

#: How the result tells the caller to stop the listener.
STOP_HINT = "call cruxible_console with stop=true, or end this MCP server"


@dataclass
class _Running:
    app: object
    background: object
    port: int


_lock = threading.Lock()
_running: dict[str, _Running] = {}


def _address(port: int, instance_id: str) -> str:
    return f"http://127.0.0.1:{port}/i/{quote(instance_id, safe='')}"


def _bootstrap_link(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/__console/bootstrap?token={token}"


def _open(link: str) -> bool:
    try:
        return bool(webbrowser.open(link))
    except webbrowser.Error:
        return False


def _result(
    status: str, instance_id: str, port: int | None, *, link: str | None
) -> McpConsoleResult:
    opened = _open(link) if link is not None else False
    return McpConsoleResult(
        status=status,  # type: ignore[arg-type]
        instance_id=instance_id,
        address=None if port is None else _address(port, instance_id),
        port=port,
        browser_opened=opened,
        # The one-time link appears only when no browser took it.
        bootstrap_link=link if link is not None and not opened else None,
        stop=STOP_HINT if status in {"started", "running"} else None,
    )


def stop_console(instance_id: str) -> McpConsoleResult:
    with _lock:
        running = _running.pop(instance_id, None)
    if running is None:
        return _result("not_running", instance_id, None, link=None)
    running.background.stop()  # type: ignore[attr-defined]
    return _result("stopped", instance_id, running.port, link=None)


def stop_all_consoles() -> None:
    with _lock:
        instances = list(_running)
    for instance_id in instances:
        stop_console(instance_id)


atexit.register(stop_all_consoles)


def open_console(
    instance_id: str,
    *,
    target: DaemonTarget,
    token: str | None,
    principal_id: str | None,
) -> McpConsoleResult:
    """Start (or reuse) the listener for ``instance_id`` and open the browser on it."""

    from cruxible_client.contracts.authoring.models import (
        AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    )
    from cruxible_core import __version__
    from cruxible_core.console.bundle import (
        NO_BUNDLE_MESSAGE,
        ConsoleBundleInvalid,
        ConsoleBundleMissing,
        default_dist_dir,
        load_bundle,
    )
    from cruxible_core.console.listener import (
        ConsoleListener,
        Upstream,
        bind_loopback,
        serve_in_background,
    )

    with _lock:
        running = _running.get(instance_id)
        if running is not None and running.background.alive:  # type: ignore[attr-defined]
            # The first link is spent: a fresh one signs this browser in again.
            fresh = running.app.renew_bootstrap()  # type: ignore[attr-defined]
            return _result(
                "running", instance_id, running.port, link=_bootstrap_link(running.port, fresh)
            )
        _running.pop(instance_id, None)
        try:
            bundle = load_bundle()
        except ConsoleBundleMissing as exc:
            raise ConfigError(NO_BUNDLE_MESSAGE.format(root=default_dist_dir())) from exc
        except ConsoleBundleInvalid as exc:
            raise ConfigError(f"the installed console build is damaged: {exc}") from exc
        headers: Mapping[str, str] = {
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **({PRINCIPAL_ID_HEADER: principal_id} if principal_id else {}),
        }
        upstream = (
            Upstream(
                label=f"unix socket {target.server_socket}",
                socket_path=target.server_socket,
                headers=headers,
            )
            if target.server_socket is not None
            else Upstream(
                label=str(target.server_url),
                base_url=str(target.server_url).rstrip("/"),
                headers=headers,
            )
        )
        sock = bind_loopback(0)
        port = int(sock.getsockname()[1])
        bootstrap = secrets.token_urlsafe(32)
        app = ConsoleListener(
            bundle=bundle,
            upstream=upstream,
            instance_id=instance_id,
            port=port,
            bootstrap_token=bootstrap,
            core_identity={
                "version": __version__,
                "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
            },
        )
        background = serve_in_background(app, sock)
        _running[instance_id] = _Running(app=app, background=background, port=port)
    return _result("started", instance_id, port, link=_bootstrap_link(port, bootstrap))


__all__ = ["STOP_HINT", "open_console", "stop_all_consoles", "stop_console"]
