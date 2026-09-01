"""Curation enforcement over the real Playbill MCP protocol."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.tool_manager import ToolManager
from mcp.shared.memory import create_connected_server_and_client_session

from cruxible_core.errors import ConfigError
from cruxible_core.mcp.server import create_server, validate_runtime_tools


def _protocol_session(server: FastMCP):
    return create_connected_server_and_client_session(server._mcp_server)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_protocol_list_hides_tools_outside_playbill_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    server = create_server()

    async def exercise() -> set[str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            return {tool.name for tool in listed.tools}

    names = _run(exercise())
    assert "cruxible_playbill_propose_document" in names
    assert "cruxible_playbill_authoring_confirm_insertion" in names
    assert "cruxible_playbill_authoring_prepare_publication" in names
    assert "cruxible_playbill_submit_approval" not in names
    assert "cruxible_playbill_activate" not in names


def test_protocol_call_refuses_hidden_playbill_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_playbill_activate",
                {"instance_id": "inst_missing", "proposal_id": "sha256:" + "0" * 64},
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, message = _run(exercise())
    assert is_error
    assert "cruxible_playbill_activate" in message
    assert "state_authoring" in message


def test_protocol_call_allows_advertised_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool("cruxible_version", {})
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, message = _run(exercise())
    assert not is_error
    assert "version" in message


def test_protocol_explicit_allowlist_is_enforced_on_list_and_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv(
        "CRUXIBLE_MCP_TOOLS",
        "cruxible_version,cruxible_playbill_get_document",
    )
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "cruxible_playbill_explain",
                {
                    "instance_id": "inst_missing",
                    "subject": {
                        "tag": "playbill-semantic-address-v1",
                        "artifact_path": "documents/design.json",
                        "selector": {"scheme": "artifact-v1", "value": ""},
                    },
                },
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, message = _run(exercise())
    assert names == {"cruxible_version", "cruxible_playbill_get_document"}
    assert is_error
    assert "cruxible_playbill_explain" in message


def test_protocol_permission_tier_hides_and_refuses_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "cruxible_playbill_store_body",
                {"instance_id": "inst_missing", "content_base64": ""},
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, message = _run(exercise())
    assert "cruxible_playbill_store_body" not in names
    assert is_error
    assert "GOVERNED_WRITE" in message
    assert "READ_ONLY" in message


def test_unknown_allowlist_name_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_TOOLS", "cruxible_version,cruxible_query")

    with pytest.raises(ConfigError, match="Unknown MCP tools"):
        create_server()


def test_protocol_listing_is_static_when_daemon_transport_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool("cruxible_server_info", {})
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, message = _run(exercise())
    assert "cruxible_playbill_search" in names
    assert "cruxible_playbill_since" in names
    assert is_error
    assert "CRUXIBLE_SERVER_URL" in message


def test_curation_private_seams_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    server = create_server()

    async def renamed_call_tool(self, tool_name, args, ctx=None):  # noqa: ANN001
        raise AssertionError("not called")

    monkeypatch.setattr(ToolManager, "call_tool", renamed_call_tool)
    with pytest.raises(ConfigError, match="MCP curation seam ToolManager.call_tool"):
        validate_runtime_tools(server)


def test_unwrapped_curation_seams_fail_startup() -> None:
    server = create_server()
    server._tool_manager.call_tool = ToolManager.call_tool.__get__(server._tool_manager)

    with pytest.raises(ConfigError, match="tools/call is not curated"):
        validate_runtime_tools(server)
