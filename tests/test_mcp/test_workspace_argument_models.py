"""Workspace mappings have declared MCP records, with no silent duplicate overwrite."""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.mcp import handlers
from cruxible_core.mcp.tools import register_tools


def test_workspace_mapping_records_reach_the_adapter_and_reject_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def check(instance_id: str, **kwargs: object) -> None:
        assert instance_id == "inst_test"
        calls.append(kwargs)

    monkeypatch.setattr(handlers, "handle_playbill_sources_check", check)
    monkeypatch.setattr(handlers, "handle_playbill_coverage", check)
    server = FastMCP("workspace arguments")
    register_tools(server)
    manager = server._tool_manager

    async def exercise() -> None:
        # Invoke the registered functions through their actual MCP argument parser.
        source = manager.get_tool("cruxible_sources_check")
        coverage = manager.get_tool("cruxible_coverage_resolve")
        assert source is not None and coverage is not None
        await source.run(
            {"instance_id": "inst_test", "root_aliases": [{"alias": "repo", "path": "."}]}
        )
        assert calls[-1]["root_aliases"] == {"repo": "."}
        await coverage.run(
            {
                "instance_id": "inst_test",
                "bindings": [{"path": "notes.md", "source": "external:repo.notes"}],
            }
        )
        assert calls[-1]["bindings"] == {"notes.md": "external:repo.notes"}
        for tool, field, record in (
            (source, "root_aliases", {"alias": "repo", "path": "."}),
            (coverage, "bindings", {"path": "notes.md", "source": "external:repo.notes"}),
        ):
            before = len(calls)
            with pytest.raises(ToolError, match="more than once"):
                await tool.run({"instance_id": "inst_test", field: [record, record]})
            assert len(calls) == before
        with pytest.raises(ToolError):
            await source.run(
                {"instance_id": "inst_test", "root_aliases": [{"alias": "repo", "path": 5}]}
            )
        with pytest.raises(ToolError):
            await coverage.run({"instance_id": "inst_test", "bindings": [{"path": "notes.md"}]})

    asyncio.run(exercise())
