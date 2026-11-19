"""`cruxible_console`: the read-only console from the MCP server, token kept out of the result.

One real scratch daemon (`daemon_world`), a folder made an instance by
`cruxible_init`, and a small console build. The browser is a recorder.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest

from cruxible_core.console import bundle as bundle_module
from cruxible_core.errors import ConfigError
from cruxible_core.mcp import console as console_module
from cruxible_core.mcp import daemon, handlers
from cruxible_core.mcp.daemon import ensure_local_daemon as real_ensure_local_daemon
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode, reset_permissions
from tests.test_console.test_console_listener import _dist


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    links: list[str] = []

    def record(link: str) -> bool:
        links.append(link)
        return True

    monkeypatch.setattr(console_module.webbrowser, "open", record)
    return links


@pytest.fixture
def instance(daemon_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(daemon, "ensure_local_daemon", real_ensure_local_daemon)
    daemon.forget_local_daemon()
    dist = _dist(tmp_path / "build")
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: dist)
    instance_id = handlers.handle_init("owner").instance_id
    yield instance_id
    console_module.stop_all_consoles()


def _answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_one_listener_per_instance_the_token_stays_out_of_the_result(
    instance: str, opened: list[str]
) -> None:
    first = handlers.handle_console(instance)

    assert first.status == "started" and first.port is not None
    assert first.address == f"http://127.0.0.1:{first.port}/i/{instance}"
    assert first.browser_opened is True and first.message is None
    assert first.stop is not None and "stop=true" in first.stop
    (link,) = opened
    token = link.partition("token=")[2]
    assert token and token not in json.dumps(first.model_dump(mode="json"))
    signed_in = httpx.get(link)
    assert signed_in.status_code == 200

    second = handlers.handle_console(instance)
    assert (second.status, second.port) == ("running", first.port)
    assert token not in json.dumps(second.model_dump(mode="json"))
    fresh = opened[-1]
    assert fresh != link and httpx.get(fresh).status_code == 200
    assert httpx.get(link).status_code == 403

    stopped = handlers.handle_console(instance, stop=True)
    assert stopped.status == "stopped"
    assert not _answers(first.port)
    assert handlers.handle_console(instance, stop=True).status == "not_running"


def test_without_a_browser_no_link_is_returned_and_the_user_runs_cruxible_console(
    instance: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    links: list[str] = []

    def no_browser(link: str) -> bool:
        links.append(link)
        return False

    monkeypatch.setattr(console_module.webbrowser, "open", no_browser)
    result = handlers.handle_console(instance)

    (link,) = links
    token = link.partition("token=")[2]
    rendered = json.dumps(result.model_dump(mode="json"))
    assert token and token not in rendered
    assert "bootstrap" not in rendered and "token" not in rendered
    assert (result.status, result.browser_opened) == ("no_browser", False)
    assert result.address is None and result.port is None and result.stop is None
    assert result.message is not None and "`cruxible console`" in result.message
    # The listener this call started is stopped: none runs without a browser session.
    assert instance not in console_module._running
    assert not _answers(int(link.split(":")[2].split("/")[0]))


def test_a_running_console_without_a_browser_keeps_its_listener_and_returns_no_link(
    instance: str, monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    first = handlers.handle_console(instance)
    monkeypatch.setattr(console_module.webbrowser, "open", lambda link: False)
    again = handlers.handle_console(instance)
    assert (again.status, again.port, again.browser_opened) == ("running", first.port, False)
    assert again.message is not None and "`cruxible console`" in again.message
    assert "token" not in json.dumps(again.model_dump(mode="json"))
    assert first.port is not None and _answers(first.port)


def test_the_listener_stops_with_the_server(instance: str, opened: list[str]) -> None:
    result = handlers.handle_console(instance)
    running = console_module._running[instance]
    assert running.background._thread.daemon is True  # type: ignore[attr-defined]
    # The process-exit hook the module registers stops every listener.
    console_module.stop_all_consoles()
    assert result.port is not None and not _answers(result.port)


def test_without_a_built_console_the_tool_says_so_plainly(
    instance: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, opened: list[str]
) -> None:
    monkeypatch.setattr(bundle_module, "default_dist_dir", lambda: tmp_path / "nothing")
    with pytest.raises(ConfigError, match="This install has no built console"):
        handlers.handle_console(instance)
    assert opened == []


def test_read_only_is_enough_to_call_it(monkeypatch: pytest.MonkeyPatch) -> None:
    assert TOOL_PERMISSIONS["cruxible_console"] == PermissionMode.READ_ONLY
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    names = {tool.name for tool in asyncio.run(create_server().list_tools())}
    assert "cruxible_console" in names
    reset_permissions()
