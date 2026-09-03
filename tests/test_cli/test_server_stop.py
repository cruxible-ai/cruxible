"""CLI and HTTP contract for `cruxible server stop`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.server.state_lock import StateRootLock


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: object) -> None:
    monkeypatch.setattr("cruxible_core.cli.commands.server._get_client", lambda: client)


class _StubClient:
    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root
        self.stop_calls = 0

    def server_stop(self) -> contracts.ServerStopResult:
        self.stop_calls += 1
        return contracts.ServerStopResult(
            scheduled=True,
            version="0.5.1",
            state_root=str(self.state_root),
            pid=4242,
        )


def test_server_stop_requires_server_mode(monkeypatch, runner: CliRunner) -> None:
    _patch_client(monkeypatch, None)

    result = runner.invoke(cli, ["server", "stop"])

    assert result.exit_code == 2
    assert "Server mode is required" in result.output


def test_server_stop_reports_the_released_state_root(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    """A released root is the only proof the daemon really left."""

    client = _StubClient(tmp_path / "state")
    (tmp_path / "state").mkdir()
    _patch_client(monkeypatch, client)

    result = runner.invoke(cli, ["--server-url", "http://server", "server", "stop"])

    assert result.exit_code == 0, result.output
    assert client.stop_calls == 1
    assert "Stop scheduled (pid 4242, version 0.5.1)." in result.output
    assert "released its state root" in result.output


def test_server_stop_says_so_when_the_daemon_still_holds_the_root(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    client = _StubClient(root)
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)

    with StateRootLock(root, transport="127.0.0.1:8100"):
        result = runner.invoke(
            cli,
            ["--server-url", "http://server", "server", "stop", "--timeout", "0.05", "--json"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["scheduled"] is True
    assert payload["pid"] == 4242
    assert payload["state_root_released"] is False
