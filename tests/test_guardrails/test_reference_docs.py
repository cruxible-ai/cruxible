"""Guardrails that keep public references synchronized with runtime surfaces."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from cruxible_core.cli.main import cli
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS


@pytest.fixture(autouse=True)
def reset_mcp_surface_env(monkeypatch):
    """Keep ambient MCP curation env from changing reference-doc assertions."""
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_TOOLS", raising=False)
    monkeypatch.delenv("CRUXIBLE_MCP_TOOL_ALLOWLIST", raising=False)
    reset_permissions()
    yield
    reset_permissions()


def _walk_cli_commands(command, prefix: tuple[str, ...] = ()) -> list[str]:
    rows: list[str] = []
    if hasattr(command, "commands"):
        for name, subcommand in sorted(command.commands.items()):
            path = prefix + (name,)
            rows.append("cruxible " + " ".join(path))
            rows.extend(_walk_cli_commands(subcommand, path))
    return rows


def _headings(path: str | Path) -> set[str]:
    text = Path(path).read_text()
    return {
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
    }


def _section(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    assert match is not None, f"Missing section for {heading}"
    return match.group("body")


def test_cli_reference_lists_every_click_command() -> None:
    expected = set(_walk_cli_commands(cli))
    actual = _headings("docs/cli-reference.md")

    missing = sorted(expected - actual)
    stale = sorted(
        heading for heading in actual if heading.startswith("cruxible ") and heading not in expected
    )
    assert missing == []
    assert stale == []


def test_mcp_reference_lists_every_registered_tool_and_input_property() -> None:
    text = Path("docs/mcp-tools.md").read_text()
    headings = _headings("docs/mcp-tools.md")
    tools = asyncio.run(create_server().list_tools())

    expected = {tool.name for tool in tools}
    actual = {heading for heading in headings if heading.startswith("cruxible_")}
    assert sorted(expected - actual) == []
    assert sorted(actual - expected) == []

    for tool in tools:
        body = _section(text, tool.name)
        assert f"**Permission:** `{TOOL_PERMISSIONS[tool.name].name}`" in body, (
            f"{tool.name} has stale permission documentation"
        )
        assert f"**Purpose:** {tool.description}" in body, (
            f"{tool.name} has stale purpose documentation"
        )
        for prop in tool.inputSchema.get("properties", {}):
            assert f"`{prop}`" in body, f"{tool.name} omits input property {prop}"
