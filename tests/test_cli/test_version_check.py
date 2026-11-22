"""A client and a daemon on different authoring contracts.

The same contract snapshot digest proceeds silently whatever the package
versions. A different digest in the same ``major.minor`` series serves reads
and refuses each write before sending it; another series refuses every call,
in one line naming both versions, both digests, where the daemon is and the
fix. One ``/version`` request per process. ``--version``, ``--help``,
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
        if request.url.path.endswith("/proposal-selector"):
            selector = request.url.params["selector"]
            return httpx.Response(200, json={"selector": selector, "proposal_id": selector})
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
    monkeypatch.chdir(tmp_path)
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


# Patch skew: the same major.minor series on another authoring contract (a
# 0.5.0 client and a 0.5.1 daemon). Reads serve; writes refuse before sending.
_major, _minor, *_rest = __version__.split(".")
PATCH_SKEW = f"{_major}.{_minor}.999"
OTHER_SERIES = f"{_major}.{int(_minor) + 1}.0"
_WRITE_PATHS = ("/set", "/retire", "/write", "/activate", "/reject", "/ack")


def _patch_skew(daemon: dict[str, object]) -> list[str]:
    daemon["version"] = PATCH_SKEW
    daemon["digest"] = OTHER_DIGEST
    calls = daemon["calls"]
    assert isinstance(calls, list)
    return calls


@pytest.mark.parametrize(
    ("command", "route"),
    [
        (("whoami",), "/api/v1/inst_x/whoami"),
        (("orient",), "/api/v1/inst_x/orient"),
        (("query", "project.work_item"), "/api/v1/inst_x/query"),
        (("get", "project.work_item/wi-1"), "/api/v1/inst_x/get"),
        (("next",), "/api/v1/inst_x/next"),
        (("proposal", "list"), "/api/v1/inst_x/proposals"),
    ],
)
def test_cli_reads_serve_across_patch_skew(daemon, command: tuple[str, ...], route: str) -> None:
    calls = _patch_skew(daemon)
    result = _run("--instance-id", "inst_x", *command)

    assert "daemon_version_incompatible" not in result.stderr  # type: ignore[attr-defined]
    assert calls[0] == "/version" and calls.count("/version") == 1
    assert route in calls


@pytest.mark.parametrize(
    "command",
    [
        ("proposal", "activate", "sha256:" + "1" * 64),
        ("proposal", "reject", "sha256:" + "1" * 64, "--reason", "no"),
        ("inbox", "ack", "inb_1"),
    ],
)
def test_cli_writes_refuse_across_patch_skew_before_sending(
    daemon, command: tuple[str, ...]
) -> None:
    calls = _patch_skew(daemon)
    result = _run("--instance-id", "inst_x", *command)

    assert result.exit_code == 1  # type: ignore[attr-defined]
    stderr = result.stderr  # type: ignore[attr-defined]
    assert "cruxible.sdk.daemon_version_incompatible" in stderr
    assert "Reads still serve across this patch skew" in stderr
    assert not [path for path in calls if path.endswith(_WRITE_PATHS)]


def test_cli_reads_refuse_across_a_minor_series(daemon) -> None:
    daemon["version"] = OTHER_SERIES
    result = _run("--instance-id", "inst_x", "orient")

    assert result.exit_code == 1  # type: ignore[attr-defined]
    assert "cruxible.sdk.daemon_version_incompatible" in result.stderr  # type: ignore[attr-defined]
    assert "Reads still serve" not in result.stderr  # type: ignore[attr-defined]
    assert daemon["calls"] == ["/version"]


def test_cli_escape_hatch_lets_writes_through_patch_skew(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_skew(daemon)
    monkeypatch.setenv("CRUXIBLE_SKIP_VERSION_CHECK", "1")
    result = _run("--instance-id", "inst_x", "proposal", "activate", "sha256:" + "1" * 64)

    assert "daemon_version_incompatible" not in result.stderr  # type: ignore[attr-defined]
    assert "/version" not in calls
    assert any(path.endswith("/activate") for path in calls)


def test_mcp_reads_serve_and_writes_refuse_across_patch_skew(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _patch_skew(daemon)
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", URL)
    handlers.reset_client_cache()
    try:
        handlers._get_client()  # the server's client starts
        with pytest.raises(Exception) as read:
            handlers.handle_playbill_whoami("inst_x")  # reaches the daemon (404 here)
        assert not isinstance(read.value, IncompatibleDaemonVersion)
        assert "/api/v1/inst_x/whoami" in calls

        with pytest.raises(IncompatibleDaemonVersion, match="Reads still serve"):
            handlers.handle_playbill_retire(
                "inst_x", target="CLM-" + "0" * 32, because="no longer true"
            )
        with pytest.raises(IncompatibleDaemonVersion):
            handlers.handle_playbill_activate("inst_x", "sha256:" + "1" * 64)
        assert not [path for path in calls if path.endswith(_WRITE_PATHS)]
        assert calls.count("/version") == 1
    finally:
        handlers.reset_client_cache()


def test_mcp_refuses_a_minor_series_at_start(daemon, monkeypatch: pytest.MonkeyPatch) -> None:
    daemon["version"] = OTHER_SERIES
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", URL)
    handlers.reset_client_cache()
    with pytest.raises(IncompatibleDaemonVersion):
        handlers._get_client()
    handlers.reset_client_cache()


@pytest.mark.parametrize(
    ("path", "reads"),
    [
        ("/api/v1/inst_x/query", True),
        ("/api/v1/inst_x/get", True),
        ("/api/v1/inst_x/get-batch", True),
        ("/api/v1/inst_x/next", True),
        ("/api/v1/inst_x/since", True),
        ("/api/v1/inst_x/coverage/resolve", True),
        ("/api/v1/inst_x/captures/read", True),
        ("/api/v1/inst_x/floor/export", True),
        ("/api/v1/inst_x/proposals/sha256:1/review", True),
        ("/api/v1/inst_x/procedures/p/readings", True),
        ("/api/v1/server/stop", True),
        ("/api/v1/inst_x/set", False),
        ("/api/v1/inst_x/write", False),
        ("/api/v1/inst_x/retire", False),
        ("/api/v1/inst_x/bodies", False),
        ("/api/v1/inst_x/authoring/submit", False),
        ("/api/v1/inst_x/proposals/sha256:1/activate", False),
        ("/api/v1/inst_x/proposals/sha256:1/approval-challenge", False),
        ("/api/v1/inst_x/procedures/p/runs", False),
        ("/api/v1/inst_x/captures/erase", False),
        ("/api/v1/inst_x/query/extra", False),
    ],
)
def test_only_declared_read_posts_cross_patch_skew(path: str, reads: bool) -> None:
    from cruxible_client.transport.http import _is_read_post

    assert _is_read_post(path) is reads


def test_the_explicit_precheck_raises_the_recorded_refusal(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client import CruxibleClient
    from cruxible_client.compatibility import (
        check_daemon_compatibility,
        require_authoring_compatibility,
    )

    _patch_skew(daemon)
    with CruxibleClient(base_url=URL) as client:
        require_authoring_compatibility(client)  # nothing recorded before the probe
        check_daemon_compatibility(client)
        with pytest.raises(IncompatibleDaemonVersion):
            require_authoring_compatibility(client)
        daemon["digest"] = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
        check_daemon_compatibility(client)  # a matching probe clears it
        require_authoring_compatibility(client)
