"""Rule R10: no raw traceback or pydantic dump reaches a user, on any surface.

Representative bad inputs go through the CLI, the MCP tool door and the HTTP
routes, and what comes back must be a refusal a caller can read: no Python
traceback, no pydantic ``ValidationError`` rendering (its "N validation errors
for <Model>" header, internal validator names, or documentation URLs).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from tests.support.mcp_daemon import bind_mcp_daemon

from cruxible_client.errors import CoreError
from cruxible_core.cli.main import cli
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import reset_registry

_LEAKS = (
    "Traceback (most recent call last)",
    "errors.pydantic.dev",
    "For further information visit",
    "validation error for",
    "validation errors for",
    "function-after[",
    "constrained-str",
)


def _assert_readable(text: str) -> None:
    leaked = [marker for marker in _LEAKS if marker in text]
    assert not leaked, f"{leaked} reached the user:\n{text}"


# --- CLI ---------------------------------------------------------------------------


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("CRUXIBLE_DEBUG", raising=False)
    return CliRunner()


def _cli_cases(tmp_path: Path) -> list[list[str]]:
    bad_json = tmp_path / "bad.json"
    bad_json.write_text('{"not": "a request"}', encoding="utf-8")
    empty_dir = tmp_path / "not-a-kit"
    empty_dir.mkdir()
    return [
        # a request model the CLI builds from flags
        [
            "kit",
            "build",
            "--id",
            "Bad ID",
            "--version",
            "x",
            "--owns",
            "a.",
            "--out",
            str(tmp_path / "out"),
        ],
        # kit sources that name no kit
        ["kit", "add", str(tmp_path / "missing-kit")],
        ["kit", "add", str(empty_dir)],
        ["kit", "push", str(empty_dir), "registry.example/kits/a:1"],
        ["kit", "pull", "::bad::", "--out", str(tmp_path / "pulled")],
        # request files that are not requests
        ["claim-type", "migrate", str(bad_json)],
        ["prediction", "propose", str(bad_json)],
        # a file that is not there
        [
            "sources",
            "compile",
            "--catalog",
            str(tmp_path / "missing.yaml"),
            "--root",
            str(tmp_path),
            "--output",
            str(tmp_path / "bundle.json"),
        ],
    ]


def test_cli_failures_are_one_readable_line(runner: CliRunner, tmp_path: Path) -> None:
    for args in _cli_cases(tmp_path):
        result = runner.invoke(cli, ["--instance-id", "inst_x", *args])
        assert result.exit_code != 0, (args, result.output)
        # Nothing escaped as an exception for click to print as a traceback.
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            args,
            result.exception,
        )
        _assert_readable(result.output)
        assert "Error" in result.output, (args, result.output)


def test_an_unexpected_cli_failure_is_one_line_unless_debugging(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def explode(_source: str) -> object:
        raise KeyError("something internal")

    monkeypatch.setattr("cruxible_core.cli.commands.playbill.resolve_kit", explode)
    result = runner.invoke(cli, ["--instance-id", "inst_x", "kit", "add", "x"])
    assert result.exit_code == 1
    _assert_readable(result.output)
    assert "CRUXIBLE_DEBUG=1" in result.output

    monkeypatch.setenv("CRUXIBLE_DEBUG", "1")
    debugging = runner.invoke(cli, ["--instance-id", "inst_x", "kit", "add", "x"])
    assert isinstance(debugging.exception, KeyError)


# --- MCP ---------------------------------------------------------------------------


@pytest.fixture
def mcp_tools(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    reset_permissions()
    server = create_server()
    return server._tool_manager  # noqa: SLF001 - the tool door FastMCP calls


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("cruxible_retire", {"instance_id": "inst_x", "target": 7, "because": 3}),
        ("cruxible_set", {"instance_id": "inst_x", "subject": [], "field": {}}),
        ("cruxible_curation_suppress", {"instance_id": "inst_x", "item_id": 5}),
        ("cruxible_kit_remove", {"instance_id": "inst_x", "request": {"kit_id": 1}}),
        ("cruxible_proposal_withdraw", {"instance_id": "inst_x", "reason": "x" * 1001}),
    ],
)
def test_mcp_tool_failures_name_paths_not_models(
    mcp_tools: object, tool: str, arguments: dict[str, object]
) -> None:
    call = mcp_tools.call_tool  # type: ignore[attr-defined]
    with pytest.raises(ToolError) as refused:
        asyncio.run(call(tool, arguments, context=None, convert_result=False))
    _assert_readable(str(refused.value))
    assert tool in str(refused.value)


def test_a_daemon_refusal_through_mcp_names_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP has one door, the daemon's: its refusal reaches the agent readable."""

    bind_mcp_daemon(monkeypatch)
    with pytest.raises(CoreError) as refused:
        handlers.handle_playbill_withdraw_proposal("inst_never_reached", "PROP-1", "x" * 1_001)
    message = str(refused.value)
    _assert_readable(message)
    assert "reason" in message


# --- HTTP --------------------------------------------------------------------------


@pytest.fixture
def http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        yield client
    get_playbill_manager().clear()
    reset_registry()
    reset_runtime_credential_store()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/inst_x/set", {"subject": 1, "field": [], "value": {}}),
        ("/api/v1/inst_x/retire", {"target": "", "because": ""}),
        ("/api/v1/inst_x/kits/remove", {"kit_id": "Not A Kit"}),
        ("/api/v1/inst_x/claim-types/upgrade", {"dry_run": "maybe"}),
        ("/api/v1/inst_x/curation/suppress", {"item_id": "not-a-digest"}),
        ("/api/v1/inst_x/runtime/credentials", {"permission_mode": "root"}),
    ],
)
def test_http_refusals_are_coded_envelopes(
    http: TestClient, path: str, body: dict[str, object]
) -> None:
    response = http.post(path, json=body)
    assert 400 <= response.status_code < 500, (path, response.status_code, response.text)
    _assert_readable(response.text)
    payload = response.json()
    assert payload["error_code"], payload
    assert payload["repair"] is not None, payload
