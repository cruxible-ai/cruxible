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
from cruxible_core.workflow.step_handlers import DEFAULT_STEP_HANDLER_REGISTRY


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


def _documented_tool_tiers(path: str | Path) -> set[tuple[str, str]]:
    """Extract the ``(tool, permission tier)`` set documented in ``mcp-tools.md``.

    Each tool is a ``## cruxible_<name>`` heading whose section begins with a
    ``**Permission:** `<TIER>` `` line. We pair every such heading with the
    first permission line in its section, tolerating surrounding prose.
    """
    text = Path(path).read_text()
    # Capture (heading name, section body up to the next heading of any level).
    section_pattern = re.compile(
        r"^##\s+(?P<name>cruxible_[a-z_]+)\s*$\n(?P<body>.*?)(?=^#{1,6}\s+|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    permission_pattern = re.compile(r"\*\*Permission:\*\*\s+`(?P<tier>[A-Z_]+)`")
    pairs: set[tuple[str, str]] = set()
    for match in section_pattern.finditer(text):
        permission = permission_pattern.search(match.group("body"))
        assert permission is not None, (
            f"{match.group('name')} is documented without a **Permission:** line"
        )
        pairs.add((match.group("name"), permission.group("tier")))
    return pairs


def test_mcp_reference_tool_tiers_set_equal_tool_permissions() -> None:
    """The (tool, tier) set in mcp-tools.md is set-equal to TOOL_PERMISSIONS.

    Drift in EITHER direction fails: a code change to a tool's permission tier
    (or adding/removing a tool) that is not mirrored in ``docs/mcp-tools.md`` —
    or a doc edit not mirrored in code — breaks this test with a message naming
    the exact offending ``(tool, tier)`` pair.
    """
    documented = _documented_tool_tiers("docs/mcp-tools.md")
    authoritative = {(tool, mode.name) for tool, mode in TOOL_PERMISSIONS.items()}

    code_not_docs = sorted(authoritative - documented)
    docs_not_code = sorted(documented - authoritative)
    assert code_not_docs == [], (
        "TOOL_PERMISSIONS entries missing/mismatched in docs/mcp-tools.md "
        f"(in code, not docs): {code_not_docs}"
    )
    assert docs_not_code == [], (
        "docs/mcp-tools.md (tool, tier) entries missing/mismatched in "
        f"TOOL_PERMISSIONS (in docs, not code): {docs_not_code}"
    )


def _documented_step_kinds(path: str | Path) -> set[str]:
    """Extract the workflow step-kind set from the table in ``config-reference.md``.

    The "Workflow Step Types" section holds a Markdown table whose first column
    is a back-ticked step-kind token (e.g. ``| `provider` | ... |``). We read
    every data row's first cell, ignoring the header and separator rows.
    """
    text = Path(path).read_text()
    section = re.search(
        r"^###\s+Workflow Step Types\s*$\n(?P<body>.*?)(?=^#{1,6}\s+|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None, "config-reference.md is missing the Workflow Step Types section"
    kinds: set[str] = set()
    for line in section.group("body").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first_cell = line.split("|")[1].strip()
        cell_match = re.match(r"`(?P<kind>[a-z_]+)`$", first_cell)
        if cell_match:
            kinds.add(cell_match.group("kind"))
    return kinds


def test_config_reference_step_kinds_set_equal_executor_dispatch() -> None:
    """The documented workflow step kinds set-equal the executor dispatch set.

    The authoritative set is the executor dispatch registry
    (``DEFAULT_STEP_HANDLER_REGISTRY``), which itself asserts coverage of the
    ``StepKind`` literal at import time. A new step kind added to the executor
    that is not added to the ``Workflow Step Types`` table in
    ``docs/config-reference.md`` (or a stale documented kind) fails here with a
    message naming the exact offending kind.
    """
    documented = _documented_step_kinds("docs/config-reference.md")
    dispatched = set(DEFAULT_STEP_HANDLER_REGISTRY.registered_kinds)

    code_not_docs = sorted(dispatched - documented)
    docs_not_code = sorted(documented - dispatched)
    assert code_not_docs == [], (
        "Executor step kinds missing from docs/config-reference.md "
        f"Workflow Step Types table (in code, not docs): {code_not_docs}"
    )
    assert docs_not_code == [], (
        "docs/config-reference.md documents step kinds the executor does not "
        f"dispatch (in docs, not code): {docs_not_code}"
    )
