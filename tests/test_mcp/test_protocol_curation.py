"""Curation enforcement over the REAL MCP protocol.

These tests drive an actual ``ClientSession`` across in-memory transport
streams — the same JSON-RPC path a stdio agent host uses — instead of calling
``server.list_tools()`` / ``server.call_tool()`` in process. That distinction is
the whole point: the advertised surface was curated only at the in-process
seam, so ``tools/list`` and ``tools/call`` over the wire happily exposed and
executed every registered tool regardless of the active profile. An in-process
test cannot observe that bug.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.tool_manager import ToolManager
from mcp.shared.memory import create_connected_server_and_client_session

from cruxible_core.errors import ConfigError
from cruxible_core.mcp.permissions import reset_permissions
from cruxible_core.mcp.server import create_server, validate_runtime_tools


def _protocol_session(server):
    """Open a real ClientSession connected to the server's low-level protocol."""
    return create_connected_server_and_client_session(server._mcp_server)


def _run(coro):
    return asyncio.run(coro)


def test_protocol_tools_list_hides_uncurated_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    reset_permissions()
    server = create_server()

    async def exercise() -> set[str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            return {tool.name for tool in listed.tools}

    names = _run(exercise())

    assert "cruxible_batch_direct_write" in names
    assert "cruxible_query" in names
    # Review-surface tools are outside the state_authoring profile.
    assert "cruxible_feedback" not in names
    assert "cruxible_propose_group" not in names


def test_protocol_tools_call_refuses_uncurated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug: an uncurated tool was hidden from the listing but still callable."""
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    reset_permissions()
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool(
                "cruxible_propose_group",
                {
                    "instance_id": "inst_missing",
                    "relationship_type": "fits",
                    "members": [],
                },
            )
            text = " ".join(block.text for block in result.content if getattr(block, "text", None))
            return bool(result.isError), text

    is_error, text = _run(exercise())

    assert is_error
    assert "cruxible_propose_group" in text
    # The refusal teaches WHY and names the active profile.
    assert "state_authoring" in text
    assert "CRUXIBLE_MCP_PROFILE" in text


def test_protocol_tools_call_allows_curated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Curation must refuse only what it hides; advertised tools still execute."""
    monkeypatch.setenv("CRUXIBLE_MCP_PROFILE", "state_authoring")
    reset_permissions()
    server = create_server()

    async def exercise() -> tuple[bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            result = await session.call_tool("cruxible_version", {})
            text = " ".join(block.text for block in result.content if getattr(block, "text", None))
            return bool(result.isError), text

    is_error, text = _run(exercise())

    assert not is_error
    assert "version" in text


def test_protocol_allowlist_refusal_names_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_MCP_TOOLS", "cruxible_version,cruxible_query")
    reset_permissions()
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "cruxible_get_entity",
                {"instance_id": "inst_missing", "entity_type": "X", "entity_id": "Y"},
            )
            text = " ".join(block.text for block in result.content if getattr(block, "text", None))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, text = _run(exercise())

    assert names == {"cruxible_version", "cruxible_query"}
    assert is_error
    assert "CRUXIBLE_MCP_TOOLS" in text


def test_protocol_mode_refusal_names_the_required_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool hidden by permission mode refuses with the mode it needs."""
    monkeypatch.setenv("CRUXIBLE_MODE", "read_only")
    reset_permissions()
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await session.call_tool(
                "cruxible_batch_direct_write",
                {"instance_id": "inst_missing", "payload": {}},
            )
            text = " ".join(block.text for block in result.content if getattr(block, "text", None))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, text = _run(exercise())

    assert "cruxible_batch_direct_write" not in names
    assert is_error
    assert "CRUXIBLE_MODE" in text
    assert "GRAPH_WRITE" in text


def test_protocol_listing_answers_without_a_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools/list is static: no transport configured, listing still answers."""
    monkeypatch.setenv("CRUXIBLE_REQUIRE_SERVER", "true")
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    reset_permissions()
    server = create_server()

    async def exercise() -> tuple[set[str], bool, str]:
        async with _protocol_session(server) as session:
            await session.initialize()
            listed = await session.list_tools()
            result = await asyncio.wait_for(
                session.call_tool("cruxible_server_info", {}),
                timeout=10.0,
            )
            text = " ".join(block.text for block in result.content if getattr(block, "text", None))
            return {tool.name for tool in listed.tools}, bool(result.isError), text

    names, is_error, text = _run(exercise())

    assert "cruxible_query" in names
    assert is_error
    assert "CRUXIBLE_SERVER_URL" in text


class TestCurationSeamPinning:
    """The gate wraps PRIVATE FastMCP internals, so it must fail loudly on drift.

    An ``mcp`` package bump can move these seams with no deprecation. Without
    this check the failure mode is either a total outage on the first tool call
    or — far worse for a security gate — a seam that silently stops being
    wrapped. ``validate_runtime_tools()`` runs in ``main()``, so drift is a
    startup refusal with a named reason.
    """

    def test_call_tool_signature_drift_refuses_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_permissions()
        server = create_server()

        async def renamed_call_tool(self, tool_name, args, ctx=None):  # noqa: ANN001
            raise AssertionError("not called")

        monkeypatch.setattr(ToolManager, "call_tool", renamed_call_tool)

        with pytest.raises(ConfigError) as exc_info:
            validate_runtime_tools(server)

        message = str(exc_info.value)
        assert "ToolManager.call_tool()" in message
        assert "curation gate is written against" in message
        assert "mcp package" in message

    def test_list_tools_signature_drift_refuses_at_startup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reset_permissions()
        server = create_server()

        async def paginated_list_tools(self, cursor=None):  # noqa: ANN001
            raise AssertionError("not called")

        monkeypatch.setattr(FastMCP, "list_tools", paginated_list_tools)

        with pytest.raises(ConfigError) as exc_info:
            validate_runtime_tools(server)

        message = str(exc_info.value)
        assert "FastMCP.list_tools()" in message
        assert "curation gate is written against" in message

    def test_unwrapped_call_seam_refuses_at_startup(self) -> None:
        """A gate that failed to install must not be served as if it had."""
        reset_permissions()
        server = create_server()
        # Simulate FastMCP restoring its own dispatcher after our install.
        server._tool_manager.call_tool = ToolManager.call_tool.__get__(server._tool_manager)

        with pytest.raises(ConfigError, match="tools/call is not curated"):
            validate_runtime_tools(server)

    def test_unwrapped_list_seam_refuses_at_startup(self) -> None:
        reset_permissions()
        server = create_server()
        del server.list_tools

        with pytest.raises(ConfigError, match="tools/list is not curated"):
            validate_runtime_tools(server)

    def test_intact_seams_pass(self) -> None:
        reset_permissions()
        validate_runtime_tools(create_server())
