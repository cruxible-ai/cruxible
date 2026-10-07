"""The daemon an MCP handler test talks to: the real app, served in process.

MCP tools have one path, through the daemon client. A test that exercises a
handler end to end binds the adapter to a `CruxibleClient` whose transport is a
FastAPI `TestClient` over `create_app()`, under the test's own state root, so
the request crosses the real routes without a socket or a second process.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient
from cruxible_core.mcp import handlers
from cruxible_core.mcp.daemon import DaemonTarget
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import get_registry

IN_PROCESS_DAEMON_URL = "http://cruxible-daemon"


def bind_mcp_daemon(
    monkeypatch: pytest.MonkeyPatch,
    client: object | None = None,
    *,
    instances: Sequence[str] = (),
) -> CruxibleClient:
    """Point every MCP handler at ``client``, or at a fresh in-process daemon.

    ``instances`` are registered with the daemon first, for a test that stubs
    the service behind a route and only needs the route to find its host.
    """

    for instance_id in instances:
        get_registry().create_governed_instance_with_id(instance_id)
    if client is None:
        served = CruxibleClient(base_url=IN_PROCESS_DAEMON_URL)
        served._client.close()
        served._client = TestClient(create_app())  # type: ignore[assignment]
        client = served
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    monkeypatch.setattr(
        handlers,
        "resolve_daemon_target",
        lambda: DaemonTarget(IN_PROCESS_DAEMON_URL, None, "environment"),
    )
    return client  # type: ignore[return-value]


__all__ = ["IN_PROCESS_DAEMON_URL", "bind_mcp_daemon"]
