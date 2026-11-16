"""`cruxible_link` on MCP: the same about and guides lanes as `cruxible link`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cruxible_client.authoring.files import catalog_file
from cruxible_client.authoring.links import GuidanceLinkOutcome
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_core.mcp import handlers
from cruxible_core.mcp.server import create_server
from cruxible_core.runtime.permissions import TOOL_PERMISSIONS, PermissionMode
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_cli.test_playbill_link import _Served


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[_Served, Path]:
    (tmp_path / "state").mkdir()
    instance, _ = seed_write_surface(tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CRUXIBLE_MCP_WORKSPACE_ROOT", str(workspace))
    client = _Served(instance)
    monkeypatch.setattr(handlers, "_get_client", lambda: client)
    return client, workspace


def test_mcp_link_about_then_guides(served: tuple[_Served, Path]) -> None:
    client, workspace = served
    (workspace / "notes.md").write_text("# Triage\nBlocked.\n")
    catalog_file(workspace, path=workspace / "notes.md")
    written = handlers.handle_playbill_write(
        "inst_write",
        because="Triage",
        changes=[
            {
                "op": "about",
                "file": "notes.md",
                "section": ["Triage"],
                "subjects": [f"{KIND}/wi-1"],
            }
        ],
    )
    assert written.status == "accepted", written
    linked = handlers.handle_playbill_link(
        "inst_write", source="doc-notes.md#Triage", about=[f"{KIND}/wi-2"], because="Also"
    )
    assert linked.status == "accepted", linked
    guided = handlers.handle_playbill_link(
        "inst_write", source="notes.md", guides=[f"ClaimType:{KIND}.status"]
    )
    assert isinstance(guided, GuidanceLinkOutcome) and guided.catalog_written
    with pytest.raises(WriteRefusalError) as refused:
        handlers.handle_playbill_link("inst_write", source="notes.md#Triage", guides=["x/y"])
    assert refused.value.error_code == "cruxible.link.guides_needs_definition"


def test_link_tool_is_a_governed_write_with_an_object_output() -> None:
    assert TOOL_PERMISSIONS["cruxible_link"] == PermissionMode.GOVERNED_WRITE
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
        tools = asyncio.run(create_server().list_tools())
    (tool,) = [item for item in tools if item.name == "cruxible_link"]
    assert set(tool.inputSchema["properties"]) >= {"source", "about", "guides", "unlink"}
    assert tool.outputSchema["type"] == "object"
