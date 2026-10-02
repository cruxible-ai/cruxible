"""CLI transport-failure handling.

When the daemon is unreachable (connection refused, timeout, DNS), Playbill
reads and server metadata commands must emit a friendly single-line error and exit
non-zero -- never a raw httpx traceback (agent/UX-hostile).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from cruxible_client.errors import ServerUnreachableError
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


def test_playbill_read_against_dead_port_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    # 127.0.0.1:1 is reserved and refuses connections, so this exercises the
    # real httpx transport path end to end.
    dead_url = "http://127.0.0.1:1"
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", dead_url)

    result = runner.invoke(
        cli,
        ["--instance-id", "inst_x", "playbill", "orient", "--section", "documents"],
    )

    assert result.exit_code == 1
    assert f"Error: could not reach Cruxible server at {dead_url}:" in result.output
    # No raw Python traceback should leak to the user.
    assert "Traceback (most recent call last)" not in result.output
    assert "httpx." not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_server_status_against_dead_port_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    dead_url = "http://127.0.0.1:1"
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", dead_url)

    result = runner.invoke(cli, ["server", "status"])

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
        client.server_info()

    err = excinfo.value
    assert err.target == "http://server.invalid"
    assert str(err) == (
        "could not reach Cruxible server at http://server.invalid: Name or service not known. "
        "Repair: run `cruxible server start`"
    )


def test_socket_target_is_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unix-socket transports name the socket path in the friendly message."""
    client = CruxibleClient(socket_path="/" + "tmp/missing.sock")

    def _boom(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(client._client._client, "get", _boom)

    with pytest.raises(ServerUnreachableError) as excinfo:
        client.server_info()

    assert excinfo.value.target == "unix:/" + "tmp/missing.sock"


def test_admission_misuse_is_a_coded_internal_error_without_a_traceback(runner):
    import click

    from cruxible_core.cli.main import handle_errors
    from cruxible_core.errors import FloorAdmissionMisuse
    from cruxible_core.runtime.admission import FLOOR_ADMISSION, HTTP_REQUEST_CONTEXT

    @click.command()
    @handle_errors
    def misuse():
        token = HTTP_REQUEST_CONTEXT.set(True)
        try:
            with FLOOR_ADMISSION.hold("inst_cli_misuse"):
                pytest.fail("request hold was admitted")
        finally:
            HTTP_REQUEST_CONTEXT.reset(token)

    result = runner.invoke(misuse)
    assert result.exit_code == 1
    assert "FloorAdmissionMisuse" in result.output
    assert FloorAdmissionMisuse.error_code in result.output
    assert "HTTP request must use async admit with the ticket" in result.output
    assert "Traceback" not in result.output and "RuntimeError" not in result.output
    assert isinstance(result.exception, SystemExit)
    assert FLOOR_ADMISSION.active_keys() == 0
