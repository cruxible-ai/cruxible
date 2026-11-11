"""A client and a daemon on different authoring contracts refuse before the first call.

What can break a call is the contract, so the contract snapshot digest decides:
a different digest refuses with one line naming both versions, both digests,
where the daemon is and the fix; the same digest proceeds silently whatever the
package versions. One ``/version`` request per process. ``--version``, ``--help``,
``server stop`` and ``context show`` never refuse, and
``CRUXIBLE_SKIP_VERSION_CHECK=1`` skips the check for development.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
from cruxible_core import __version__
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers

OTHER_DIGEST = "sha256:" + "9" * 64
URL = "http://daemon.invalid"


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """A daemon that answers /version (and stop) with what the test sets; nothing else."""

    state: dict[str, object] = {"digest": OTHER_DIGEST, "version": "9.9.9", "calls": []}

    def respond(request: httpx.Request) -> httpx.Response:
        calls = state["calls"]
        assert isinstance(calls, list)
        calls.append(request.url.path)
        if request.url.path == "/version":
            if state.get("stopped"):
                raise httpx.ConnectError("daemon stopped", request=request)
            return httpx.Response(
                200,
                json={
                    "version": state["version"],
                    "sdk_contract_snapshot_digest": state["digest"],
                    "boot_id": "image",
                },
            )
        if request.url.path == "/api/v1/server/stop":
            state["stopped"] = True
            return httpx.Response(
                200,
                json={
                    "scheduled": True,
                    "version": state["version"],
                    "state_root": str(tmp_path / "state"),
                    "boot_id": "image",
                    "pid": 4242,
                },
            )
        return httpx.Response(
            404, json={"error_type": "NotFound", "message": f"no route {request.url.path}"}
        )

    original = httpx.Client

    def client(**kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(respond)
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.delenv("CRUXIBLE_SKIP_VERSION_CHECK", raising=False)
    monkeypatch.setattr("cruxible_core.cli.commands.server.time.sleep", lambda _seconds: None)
    return state


def _run(*args: str) -> object:
    return CliRunner().invoke(cli, ["--server-url", URL, *args])


def test_a_different_contract_refuses_in_one_line_naming_both_and_the_fix(daemon) -> None:
    result = _run("--instance-id", "inst_x", "whoami")

    assert result.exit_code == 1  # type: ignore[attr-defined]
    stderr = result.stderr  # type: ignore[attr-defined]
    (line,) = [item for item in stderr.splitlines() if "is cruxible" in item]
    assert f"this client is cruxible {__version__}" in line
    assert f"the daemon at {URL} is cruxible 9.9.9" in line
    assert f"client_snapshot_digest={AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST}" in line
    assert f"daemon_snapshot_digest={OTHER_DIGEST}" in line
    assert "cruxible server stop" in line
    assert daemon["calls"] == ["/version"]


def test_the_same_contract_proceeds_silently_whatever_the_versions(daemon) -> None:
    daemon["digest"] = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    result = _run("--instance-id", "inst_x", "whoami")

    assert "daemon_version_incompatible" not in result.output  # type: ignore[attr-defined]
    calls = daemon["calls"]
    assert isinstance(calls, list) and calls[0] == "/version" and calls.count("/version") == 1


@pytest.mark.parametrize(
    "command",
    [("--version",), ("--help",), ("server", "stop"), ("context", "show")],
)
def test_exempt_commands_never_refuse(daemon, command: tuple[str, ...]) -> None:
    result = _run("--instance-id", "inst_x", *command)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    assert "is cruxible 9.9.9" not in result.stderr  # type: ignore[attr-defined]


def test_the_development_escape_hatch_skips_the_check(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRUXIBLE_SKIP_VERSION_CHECK", "1")
    result = _run("--instance-id", "inst_x", "whoami")
    assert "daemon_version_incompatible" not in result.output  # type: ignore[attr-defined]
    assert "/version" not in daemon["calls"]  # type: ignore[operator]


def test_the_mcp_server_refuses_too_and_checks_once_per_client(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", URL)
    handlers.reset_client_cache()
    with pytest.raises(IncompatibleDaemonVersion, match=f"the daemon at {URL} is cruxible 9.9.9"):
        handlers._get_client()

    daemon["digest"] = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    first = handlers._get_client()
    assert handlers._get_client() is first
    assert daemon["calls"] == ["/version", "/version"]
    handlers.reset_client_cache()


def test_version_names_the_contract_and_a_checkouts_commit() -> None:
    result = CliRunner().invoke(cli, ["--version"])
    assert result.exit_code == 0
    digest = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST.removeprefix("sha256:")[:12]
    assert re.fullmatch(
        rf"cruxible {re.escape(__version__)} \(contract {digest}(, git [0-9a-f]{{9}})?\)\n",
        result.output,
    )
