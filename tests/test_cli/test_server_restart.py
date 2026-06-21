"""CLI tests for `cruxible server restart`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.errors import CoreError
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: client)


def test_server_restart_requires_server_mode(monkeypatch, runner: CliRunner):
    _patch_client(monkeypatch, None)
    result = runner.invoke(cli, ["server", "restart"])
    assert result.exit_code == 2
    assert "requires server mode" in result.output


def test_server_restart_waits_for_daemon_and_reports_version(monkeypatch, runner: CliRunner):
    class StubClient:
        def __init__(self) -> None:
            self.restart_calls = 0
            self.version_calls = 0

        def server_restart(self) -> contracts.ServerRestartResult:
            self.restart_calls += 1
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_dir="/srv/state"
            )

        def version(self) -> str:
            self.version_calls += 1
            # First poll fails (image being replaced), second succeeds.
            if self.version_calls < 2:
                raise CoreError("connection refused")
            return "0.1.6"

    client = StubClient()
    _patch_client(monkeypatch, client)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart"])

    assert result.exit_code == 0, result.output
    assert client.restart_calls == 1
    assert client.version_calls == 2
    assert "Restart scheduled (was version 0.1.5)." in result.output
    assert "Daemon is back on version 0.1.6." in result.output


def test_server_restart_no_wait_skips_polling(monkeypatch, runner: CliRunner):
    class StubClient:
        def __init__(self) -> None:
            self.version_calls = 0

        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_dir="/srv/state"
            )

        def version(self) -> str:
            self.version_calls += 1
            return "0.1.5"

    client = StubClient()
    _patch_client(monkeypatch, client)
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart", "--no-wait"])

    assert result.exit_code == 0, result.output
    assert client.version_calls == 0
    assert "Not waiting for the daemon" in result.output


def test_server_restart_json_output(monkeypatch, runner: CliRunner):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_dir="/srv/state"
            )

        def version(self) -> str:
            return "0.1.6"

    _patch_client(monkeypatch, StubClient())
    result = runner.invoke(cli, ["--server-url", "http://server", "server", "restart", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scheduled"] is True
    assert payload["version"] == "0.1.5"
    assert payload["state_dir"] == "/srv/state"
    assert payload["waited"] is True
    assert payload["confirmed_version"] == "0.1.6"


def test_server_restart_times_out_when_daemon_never_returns(monkeypatch, runner: CliRunner):
    class StubClient:
        def server_restart(self) -> contracts.ServerRestartResult:
            return contracts.ServerRestartResult(
                scheduled=True, version="0.1.5", state_dir="/srv/state"
            )

        def version(self) -> str:
            raise CoreError("connection refused")

    _patch_client(monkeypatch, StubClient())
    # Patch sleep so the timeout budget burns down without real waiting.
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "server", "restart", "--timeout", "0.05"],
    )

    assert result.exit_code != 0
    assert "did not come back" in result.output
