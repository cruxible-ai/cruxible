"""CLI transport-failure handling.

When the daemon is unreachable (connection refused, timeout, DNS), read
commands like ``stats`` must emit a friendly single-line error and exit
non-zero -- never a raw httpx traceback (agent/UX-hostile).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from cruxible_client.errors import ServerUnreachableError
from cruxible_client.http_client import CruxibleClient
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def test_stats_against_dead_port_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    # 127.0.0.1:1 is reserved and refuses connections, so this exercises the
    # real httpx transport path end to end.
    dead_url = "http://127.0.0.1:1"
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", dead_url)

    result = runner.invoke(cli, ["--instance-id", "inst_x", "stats"])

    assert result.exit_code == 1
    assert f"Error: could not reach Cruxible server at {dead_url}:" in result.output
    # No raw Python traceback should leak to the user.
    assert "Traceback (most recent call last)" not in result.output
    assert "httpx." not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_list_against_dead_port_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    dead_url = "http://127.0.0.1:1"
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", dead_url)

    result = runner.invoke(
        cli, ["--instance-id", "inst_x", "list", "entities", "--type", "Vehicle"]
    )

    assert result.exit_code == 1
    assert f"Error: could not reach Cruxible server at {dead_url}:" in result.output
    assert "Traceback (most recent call last)" not in result.output


def test_client_wraps_transport_error_as_server_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client translates httpx.TransportError into ServerUnreachableError."""
    client = CruxibleClient(base_url="http://server.invalid")

    def _boom(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known")

    # Patch the underlying httpx.Client so no real network call is made.
    monkeypatch.setattr(client._client._client, "get", _boom)

    with pytest.raises(ServerUnreachableError) as excinfo:
        client.stats("inst_x")

    err = excinfo.value
    assert err.target == "http://server.invalid"
    assert str(err) == (
        "could not reach Cruxible server at http://server.invalid: Name or service not known"
    )


def test_socket_target_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unix-socket transports name the socket path in the friendly message."""
    client = CruxibleClient(socket_path="/tmp/missing.sock")

    def _boom(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(client._client._client, "get", _boom)

    with pytest.raises(ServerUnreachableError) as excinfo:
        client.stats("inst_x")

    assert excinfo.value.target == "unix:/tmp/missing.sock"
