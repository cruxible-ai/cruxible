"""``cruxible mcp`` is the stdio MCP server that registry launchers reach."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cruxible_mcp_answers_initialize_over_stdio(tmp_path: Path) -> None:
    """The first stdout bytes are the server's JSON-RPC reply, nothing from the root CLI."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("CRUXIBLE_")}
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
