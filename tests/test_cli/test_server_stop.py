"""CLI and HTTP contract for `cruxible server stop`."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
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
    """A daemon that answers `version()` until the scheduled stop takes effect."""

    def __init__(self, state_root: Path, *, answers_after_stop: int = 0) -> None:
        self.state_root = state_root
        self.stop_calls = 0
        self.version_calls = 0
        self._answers_after_stop = answers_after_stop

    def server_stop(self) -> contracts.ServerStopResult:
        self.stop_calls += 1
        return contracts.ServerStopResult(
            scheduled=True,
            version="0.5.1",
            state_root=str(self.state_root),
            pid=4242,
        )

    def version(self) -> str:
        self.version_calls += 1
        if self.version_calls <= self._answers_after_stop:
            return "0.5.1"
        raise httpx.ConnectError("connection refused")


class _NeverLeavingClient(_StubClient):
    """A daemon that acknowledges the stop and then keeps on answering."""

    def version(self) -> str:
        self.version_calls += 1
        return "0.5.1"


def test_server_stop_requires_server_mode(monkeypatch, runner: CliRunner) -> None:
    _patch_client(monkeypatch, None)

    result = runner.invoke(cli, ["server", "stop"])

    assert result.exit_code == 2
    assert "Server mode is required" in result.output


def test_server_stop_reports_the_released_state_root(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    """A released root is the only proof the daemon really left."""

    root = tmp_path / "state"
    root.mkdir()
    with StateRootLock(root, transport="unix socket /run/a.sock"):
        pass  # an exited daemon leaves its lock file behind, unheld
    client = _StubClient(root, answers_after_stop=1)
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)

    result = runner.invoke(cli, ["--server-url", "http://server", "server", "stop"])

    assert result.exit_code == 0, result.output
    assert client.stop_calls == 1
    assert client.version_calls >= 2  # the exit is observed from the daemon
    assert "Stop scheduled (pid 4242, version 0.5.1)." in result.output
    assert "released its state root" in result.output


def test_server_stop_exits_non_zero_when_the_root_is_still_locked(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    """`stop && start` must not walk into the lock the stop was meant to clear."""

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

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["scheduled"] is True
    assert payload["pid"] == 4242
    assert payload["daemon_exited"] is True
    assert payload["state_root_released"] is False
    assert "cruxible.server.stop_not_confirmed" in result.stderr


def test_server_stop_exits_non_zero_when_the_daemon_keeps_answering(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    root = tmp_path / "state"
    root.mkdir()
    client = _NeverLeavingClient(root)
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)

    result = runner.invoke(
        cli,
        ["--server-url", "http://server", "server", "stop", "--timeout", "0.05"],
    )

    assert result.exit_code == 1, result.output
    assert "cruxible.server.stop_not_confirmed" in result.stderr
    assert "still answering" in result.stderr


def test_a_bound_tcp_target_never_claims_a_release_it_cannot_see(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    """The daemon's state root is a path on the DAEMON's host, not on this one.

    Polling it here answered "released" instantly for a daemon that had not
    begun to shut down, because the path simply does not exist on the client.
    """

    remote_root = tmp_path / "not-on-this-host"
    assert not remote_root.exists()
    client = _StubClient(remote_root, answers_after_stop=1)
    _patch_client(monkeypatch, client)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)

    result = runner.invoke(
        cli,
        ["--server-url", "https://daemon.example.test:8100", "server", "stop"],
    )

    assert result.exit_code == 0, result.output
    assert client.version_calls >= 2
    assert "released its state root" not in result.output
    assert "lock release not observable from this client" in result.stderr


def test_the_socket_shape_and_the_tcp_shape_report_the_same_keys(
    monkeypatch, runner: CliRunner, tmp_path: Path
) -> None:
    local = tmp_path / "state"
    local.mkdir()
    with StateRootLock(local, transport="unix socket /run/a.sock"):
        pass
    remote = tmp_path / "elsewhere"

    payloads = []
    for root, flag, value in (
        (local, "--server-socket", "/run/a.sock"),
        (remote, "--server-url", "https://remote.example.test"),
    ):
        client = _StubClient(root, answers_after_stop=1)
        _patch_client(monkeypatch, client)
        monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _s: None)
        result = runner.invoke(cli, [flag, value, "server", "stop", "--json"])
        assert result.exit_code == 0, result.output
        payloads.append(json.loads(result.stdout))

    socket_payload, tcp_payload = payloads
    assert set(socket_payload) == set(tcp_payload)
    assert socket_payload["daemon_exited"] is True
    assert socket_payload["state_root_released"] is True
    assert tcp_payload["daemon_exited"] is True
    assert tcp_payload["state_root_released"] is None
