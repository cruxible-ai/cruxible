"""``cruxible mcp`` is the stdio MCP server that registry launchers reach."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("remembered_context", [None, "{not json"])
def test_cruxible_mcp_answers_initialize_over_stdio(
    tmp_path: Path, remembered_context: str | None
) -> None:
    """The first stdout bytes are the server's reply; CLI context can neither leak nor block it."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")}
    if remembered_context is not None:
        context_path = tmp_path / "client-context.json"
        context_path.write_text(remembered_context, encoding="utf-8")
        env["CRUXIBLE_CLI_CONTEXT_PATH"] = str(context_path)
    env["CRUXIBLE_STATE_ROOT"] = str(tmp_path / "state")
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT / "src"), str(_REPO_ROOT / "packages" / "cruxible-client" / "src")]
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "entry-test", "version": "0"},
        },
    }
    completed = subprocess.run(
        [sys.executable, "-c", "from cruxible_core.cli.main import cli; cli()", "mcp"],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )

    first_line = completed.stdout.splitlines()[0]
    reply = json.loads(first_line)
    assert reply["id"] == 1
    assert "cruxible" in reply["result"]["serverInfo"]["name"].lower()


def _mcp_base_env(tmp_path: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")}
    env["CRUXIBLE_STATE_ROOT"] = str(tmp_path / "state")
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT / "src"), str(_REPO_ROOT / "packages" / "cruxible-client" / "src")]
    )
    return env


def _initialize_payload() -> str:
    return (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "entry-test", "version": "0"},
                },
            }
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "invocation",
    ["subcommand_flag", "root_flag", "env_var", "mcp_entrypoint_flag"],
)
def test_mcp_loads_env_file_across_all_entrypoints(tmp_path: Path, invocation: str) -> None:
    env_file = tmp_path / "cruxible.env"
    env_file.write_text(
        "\n".join(
            [
                f"export CRUXIBLE_SERVER_SOCKET='{tmp_path / 'daemon.sock'}'",
                "export CRUXIBLE_INSTANCE_ID='inst_mcp_envfile'",
                "export CRUXIBLE_MODE='graph_write'",
                "export CRUXIBLE_SERVER_BEARER_TOKEN='tok_mcp_secret'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    env = _mcp_base_env(tmp_path)
    if invocation == "subcommand_flag":
        cmd = [
            sys.executable,
            "-c",
            "from cruxible_core.cli.main import cli; cli()",
            "mcp",
            "--env-file",
            str(env_file),
        ]
    elif invocation == "root_flag":
        cmd = [
            sys.executable,
            "-c",
            "from cruxible_core.cli.main import cli; cli()",
            "--env-file",
            str(env_file),
            "mcp",
        ]
    elif invocation == "env_var":
        env["CRUXIBLE_ENV_FILE"] = str(env_file)
        cmd = [
            sys.executable,
            "-c",
            "from cruxible_core.cli.main import cli; cli()",
            "mcp",
        ]
    else:
        cmd = [
            sys.executable,
            "-c",
            "from cruxible_core.entrypoints import mcp; mcp()",
            "--env-file",
            str(env_file),
        ]

    completed = subprocess.run(
        cmd,
        input=_initialize_payload(),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )

    first_line = completed.stdout.splitlines()[0]
    reply = json.loads(first_line)
    assert reply["id"] == 1
    instructions = reply["result"]["instructions"]
    assert "Instance: inst_mcp_envfile" in instructions
    assert "tier: GRAPH_WRITE" in instructions


def test_mcp_invalid_env_file_writes_only_to_stderr_and_never_leaks_token(
    tmp_path: Path,
) -> None:
    secret = "tok_mcp_must_not_leak_42"
    env_file = tmp_path / "loose.env"
    env_file.write_text(
        f"export CRUXIBLE_INSTANCE_ID='inst_1'\nexport CRUXIBLE_SERVER_BEARER_TOKEN='{secret}'\n",
        encoding="utf-8",
    )
    env_file.chmod(0o644)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cruxible_core.cli.main import cli; cli()",
            "mcp",
            "--env-file",
            str(env_file),
        ],
        input=_initialize_payload(),
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_mcp_base_env(tmp_path),
        timeout=60,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "cruxible.config.env_file_invalid" in completed.stderr
    assert "chmod 600" in completed.stderr
    assert secret not in completed.stderr
