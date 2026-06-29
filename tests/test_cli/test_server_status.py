"""CLI tests for `cruxible server status` (a client RPC over a running daemon).

`status` is a CLIENT command: it must require a transport, report a reachable
daemon's metadata, and fail with a clear message (not a hang/opaque trace) when
no daemon is reachable. The in-process ephemeral-daemon case exercises the real
FastAPI `server_info` route through the actual CruxibleClient, mirroring the
harness in tests/test_demos/test_kev_quickstart_smoke.py.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
from cruxible_client.errors import ServerUnreachableError
from cruxible_core.cli.main import cli
from cruxible_core.mcp.handlers import reset_client_cache
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.runtime.instance_manager import get_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: client)


def test_status_requires_server_mode(monkeypatch, runner: CliRunner) -> None:
    """No transport configured -> a clear usage error, not a hang."""
    _patch_client(monkeypatch, None)
    result = runner.invoke(cli, ["server", "status"])
    assert result.exit_code == 2
    assert "Server mode is required" in result.output
    assert "cruxible server start" in result.output


def test_status_down_daemon_errors_clearly(monkeypatch, runner: CliRunner) -> None:
    """A reachable transport but a dead daemon -> a clear single-line error."""

    class DownClient:
        def server_info(self) -> contracts.ServerInfoResult:
            raise ServerUnreachableError("http://127.0.0.1:59999", "Connection refused")

    _patch_client(monkeypatch, DownClient())
    result = runner.invoke(cli, ["--server-url", "http://127.0.0.1:59999", "server", "status"])
    assert result.exit_code == 1
    assert "could not reach Cruxible server" in result.output
    assert "Connection refused" in result.output


def test_status_reports_daemon_metadata(monkeypatch, runner: CliRunner) -> None:
    class StubClient:
        def server_info(self) -> contracts.ServerInfoResult:
            return contracts.ServerInfoResult(
                server_required=False,
                state_dir="/srv/state",
                version="0.2.0",
                instance_count=3,
                auth_enabled=True,
                auth_required=True,
            )

    _patch_client(monkeypatch, StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "status"])

    assert result.exit_code == 0, result.output
    assert "Daemon: reachable (http://server)" in result.output
    assert "Version: 0.2.0" in result.output
    assert "State dir: /srv/state" in result.output
    assert "Instances: 3" in result.output
    assert "Auth enabled: yes" in result.output
    assert "Auth required: yes" in result.output


def test_status_json_includes_transport(monkeypatch, runner: CliRunner) -> None:
    class StubClient:
        def server_info(self) -> contracts.ServerInfoResult:
            return contracts.ServerInfoResult(
                server_required=False,
                state_dir="/srv/state",
                version="0.2.0",
                instance_count=1,
                auth_enabled=False,
                auth_required=False,
            )

    _patch_client(monkeypatch, StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version"] == "0.2.0"
    assert payload["instance_count"] == 1
    assert payload["transport"] == "http://server"


@pytest.fixture
def ephemeral_daemon_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CruxibleClient]:
    """A real CruxibleClient bound to a fresh in-process daemon (no socket).

    Mirrors tests/test_demos/test_kev_quickstart_smoke.py: the client's sync HTTP
    transport is a FastAPI TestClient over create_app(), so `server status`
    exercises the real `/api/v1/server/info` route end to end.
    """
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    reset_client_cache()
    get_manager().clear()

    test_client = TestClient(create_app())
    client = CruxibleClient(base_url="http://cruxible-daemon")
    client._client = test_client
    try:
        yield client
    finally:
        test_client.close()
        get_manager().clear()


def test_status_against_ephemeral_daemon_reports_live_fields(
    monkeypatch, runner: CliRunner, ephemeral_daemon_client: CruxibleClient
) -> None:
    """start->status path: status over the real server_info route returns live data."""
    from cruxible_core import __version__

    _patch_client(monkeypatch, ephemeral_daemon_client)
    result = runner.invoke(
        cli, ["--server-url", "http://cruxible-daemon", "server", "status", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["version"] == __version__
    assert payload["instance_count"] == 0
    assert payload["transport"] == "http://cruxible-daemon"
