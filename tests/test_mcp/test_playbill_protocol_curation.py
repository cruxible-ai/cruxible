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
    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    server = create_server()

    async def exercise() -> set[str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            return {tool.name for tool in listed.tools}

    names = _run(exercise())
    assert {"cruxible_playbill_orient", "cruxible_playbill_query", "cruxible_playbill_get"} <= names
    assert "cruxible_playbill_query_spec" not in names
    assert "cruxible_playbill_set" in names
    assert "cruxible_playbill_authoring_compile" not in names
    assert "cruxible_playbill_activate" in names
    assert "cruxible_playbill_propose_document" not in names
    assert "cruxible_playbill_block_repin" not in names
    assert "cruxible_playbill_curation_list" not in names


def test_protocol_call_refuses_hidden_playbill_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_playbill_block_repin",
                {"instance_id": "inst_missing", "block": "status", "source": "runbook"},
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, message = _run(exercise())
    assert is_error
    assert "cruxible_playbill_block_repin" in message
    assert "profile 'default'" in message


def test_protocol_call_allows_advertised_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool("cruxible_server_info", {})
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return bool(result.isError), text

    is_error, message = _run(exercise())
    assert not is_error
    assert "adapter_version" in message


def test_protocol_explicit_allowlist_is_enforced_on_list_and_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch.setenv(
        "CRUXIBLE_MCP_TOOLS",
        "cruxible_server_info,cruxible_playbill_get",
    )
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "cruxible_playbill_orient", {"instance_id": "inst_missing"}
            )
            text = " ".join(block.text for block in result.content if hasattr(block, "text"))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, message = _run(exercise())
    assert names == {"cruxible_server_info", "cruxible_playbill_get"}
    assert is_error
    assert "cruxible_playbill_orient" in message


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
    monkeypatch.setenv("CRUXIBLE_MCP_TOOLS", "cruxible_server_info,cruxible_query")

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
    assert "cruxible_playbill_orient" in names
    assert "cruxible_playbill_next" in names
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


def test_default_input_schema_catalog_stays_within_agent_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default profile's model-visible catalog stays within the approved budget.

    The metric is what MCP hosts send the model: every tool's input schema
    plus its description, estimated as JSON text length over four. Output
    schemas are not call grammar and are not counted; their size is a
    separately measured follow-up.

    Any change to MCP parameter text -- a shared ``Annotated`` description
    (``ReadAt``, ``InstanceId``, ``DryRun``...) or a contract field or model
    docstring a default-profile tool exposes -- must run this test: one shared
    description is repeated in every tool that takes it.
    """
    from tests.core_support._mcp_budget import (
        DEFAULT_PROFILE_MODEL_VISIBLE_TOKENS,
        catalog_model_visible_tokens,
    )

    monkeypatch.delenv("CRUXIBLE_MCP_PROFILE", raising=False)
    tools = _run(create_server().list_tools())
    estimate = catalog_model_visible_tokens(tools)
    assert estimate <= DEFAULT_PROFILE_MODEL_VISIBLE_TOKENS, (
        f"default input schemas plus descriptions cost about {estimate:.0f} tokens"
    )
