"""Lifecycle RPCs cross authoring skew without weakening governed clients."""

from __future__ import annotations

import json
from pathlib import Path

import click
import httpx
import pytest
from click.testing import CliRunner

from cruxible_client import __version__
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_core.cli.commands import _common
from cruxible_core.cli.main import cli


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest):
    scoped = getattr(request, "param", False)
    calls: list[str] = []
    probes = 0
    action = None

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal probes, action
        path = request.url.path
        calls.append(path)
        if path == "/version":
            probes += 1
            if action == "stop" and probes > 1:
                raise httpx.ConnectError("daemon stopped", request=request)
            # A digest change alone is not restart confirmation: the boot ID
            # must change, even when the package version stays the same.
            return httpx.Response(
                200,
                json={
                    "version": __version__,
                    "boot_id": "new-image" if action == "restart" and probes >= 3 else "old-image",
                    "sdk_contract_snapshot_digest": "sha256:" + ("8" if probes >= 2 else "9") * 64,
                },
            )
        if path == "/api/v1/server/info":
            assert request.method == "GET"
            if scoped:
                return httpx.Response(
                    403,
                    json={
                        "error_type": "DaemonOperationScopeError",
                        "message": "Instance-scoped credential cannot read daemon-wide info",
                        "context": {
                            "operation": "cruxible_server_info",
                            "credential_scope": "inst_scoped",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "server_required": False,
                    "state_root": str(tmp_path / "state"),
                    "version": __version__,
                    "instance_count": 0,
                    "auth_enabled": True,
                    "auth_required": True,
                    "provider_lane": {"state": "available", "code": None, "detail": None},
                },
            )
        if path in {"/api/v1/server/restart", "/api/v1/server/stop"}:
            assert request.method == "POST"
            action = path.rsplit("/", 1)[1]
            return httpx.Response(
                200,
                json={
                    "scheduled": True,
                    "version": __version__,
                    "state_root": str(tmp_path / "state"),
                    "boot_id": "old-image",
                    "pid": 4242,
                },
            )
        pytest.fail(f"Governed endpoint called through skew: {path}")

    original = httpx.Client
    clients = []

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(respond)
        value = original(**kwargs)
        clients.append(value)
        return value

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _seconds: None)
    yield calls
    for value in clients:
        value.close()


@pytest.mark.parametrize("command", ("restart", "stop", "status"))
def test_lifecycle_works_through_an_incompatible_daemon(daemon, command: str) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://daemon.invalid",
            "server",
            command,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    if command == "restart":
        assert daemon == ["/api/v1/server/restart", "/version", "/version", "/version"]
        assert payload["confirmed_version"] == __version__ and payload["waited"] is True
    elif command == "stop":
        assert daemon == ["/api/v1/server/stop", "/version", "/version"]
        assert payload["daemon_exited"] is True
    else:
        assert daemon == ["/api/v1/server/info"]
        assert payload["version"] == __version__ and payload["scope"] == "daemon"


@pytest.mark.parametrize(
    "command",
    (
        ("playbill", "get", "project.work_item/wi-42"),
        ("playbill", "proposal", "activate", "sha256:" + "1" * 64),
    ),
)
def test_reads_and_authoring_still_refuse_contract_skew(daemon, command: tuple[str, ...]) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://daemon.invalid",
            "--instance-id",
            "inst_test",
            *command,
        ],
    )
    assert result.exit_code == 1, result.output
    assert "playbill.sdk.daemon_version_incompatible" in result.stderr
    assert daemon == ["/version"]


def test_a_cached_lifecycle_client_cannot_bypass_a_later_governed_handshake(daemon) -> None:
    with click.Context(cli, obj={"server_url": "http://daemon.invalid"}):
        lifecycle = _common._get_lifecycle_client()
        assert lifecycle is _common._get_lifecycle_client()
        from cruxible_client import CruxibleClient
        from cruxible_client.transport.lifecycle import DaemonLifecycleClient

        assert isinstance(lifecycle, DaemonLifecycleClient)
        assert not isinstance(lifecycle, CruxibleClient)
        assert {name for name in dir(lifecycle) if not name.startswith("_")} == {
            "version",
            "daemon_identity",
            "server_info",
            "server_restart",
            "server_stop",
            "close",
        }
        assert daemon == []
        with pytest.raises(IncompatibleDaemonVersion):
            _common._get_client()
        assert daemon == ["/version"]


@pytest.mark.parametrize("daemon", [True], indirect=True)
@pytest.mark.parametrize("as_json", [False, True])
def test_scoped_status_keeps_lifecycle_facts_without_unchecked_instance_reads(daemon, as_json):
    args = ["--server-url", "http://daemon.invalid", "server", "status"]
    if as_json:
        args.append("--json")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert daemon == ["/api/v1/server/info", "/version", "/version"]
    if as_json:
        payload = json.loads(result.stdout)
        assert payload["instance_status"] == "needs_matching_client"
        assert payload["code"] == "playbill.sdk.daemon_version_incompatible"
        assert payload["version"] == __version__
        assert payload["transport"] == "http://daemon.invalid"
        assert payload["instance_id"] == "inst_scoped"
        assert "host" not in payload and "identity" not in payload
    else:
        assert f"Version: {__version__}" in result.output
        assert "Daemon: reachable (http://daemon.invalid)" in result.output
    assert "Instance section needs a matching client" in result.output
