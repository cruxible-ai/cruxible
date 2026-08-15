"""Keep the concise Playbill references synchronized with served surfaces."""

from __future__ import annotations

import re
from pathlib import Path

import click

from cruxible_core.cli.main import CLI_COMMANDS, cli
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def _leaf_cli_commands(
    command: click.Command,
    prefix: tuple[str, ...] = (),
) -> set[str]:
    if not isinstance(command, click.Group):
        return {"cruxible " + " ".join(prefix)}
    result: set[str] = set()
    for name, child in command.commands.items():
        result.update(_leaf_cli_commands(child, (*prefix, name)))
    return result


def test_cli_reference_names_the_exact_public_groups_and_all_leaf_commands() -> None:
    text = (DOCS / "cli-reference.md").read_text(encoding="utf-8")
    headings = set(re.findall(r"^## ([a-z]+)(?: .*)?$", text, re.MULTILINE))
    documented_commands = set(re.findall(r"\bcruxible(?: [a-z][a-z-]*)+", text))

    assert set(CLI_COMMANDS) == {"context", "credential", "playbill", "server"}
    assert set(CLI_COMMANDS) <= headings
    assert _leaf_cli_commands(cli) <= documented_commands


def _documented_mcp_permissions(text: str) -> dict[str, str]:
    return {
        tool: permission
        for tool, permission in re.findall(
            r"^\| `(cruxible_[a-z_]+)` \|.*?\| `([A-Z_]+)` \|$",
            text,
            re.MULTILINE,
        )
    }


def test_mcp_reference_lists_exact_public_tools_and_permissions() -> None:
    text = (DOCS / "mcp-tools.md").read_text(encoding="utf-8")
    documented = _documented_mcp_permissions(text)
    authoritative = {name: tier.name for name, tier in TOOL_PERMISSIONS.items()}

    assert documented == authoritative
    assert "private key" in text.lower()
    assert "client-side" in text.lower()


def test_mkdocs_navigation_names_only_live_documents() -> None:
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    linked = re.findall(r":\s+([A-Za-z0-9_./-]+\.md)\s*$", config, re.MULTILINE)
    assert linked
    assert all((DOCS / path).is_file() for path in linked)

    removed_products = {
        "agent-operation-guide.md",
        "common-providers.md",
        "config-reference.md",
        "deep-dive.md",
        "design-read-ergonomics.md",
    }
    assert removed_products.isdisjoint(linked)


def test_readme_identifies_the_breaking_playbill_development_surface() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    assert "playbill" in text
    assert "breaking" in text
    assert "family 1" in text
    assert "claims" in text
    assert "procedures" in text
