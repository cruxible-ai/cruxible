"""S6: every MCP tool refuses an argument it does not take, naming it and its arguments.

Before, FastMCP's argument models ignored extra keys, so a misspelt argument
(``detials`` for ``detail``) was dropped and the call ran with the default.
Each tool's argument model now forbids extras, and a call naming one is
refused with ``cruxible.mcp.argument_unknown`` before the tool runs.
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cruxible_core.mcp.server import ARGUMENT_UNKNOWN_CODE, create_server

PROBE = "cruxible_unknown_probe"


@pytest.fixture(scope="module")
def server(monkeypatch_module: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch_module.setenv("CRUXIBLE_MCP_PROFILE", "full")
    monkeypatch_module.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch_module.delenv("CRUXIBLE_MCP_TOOLS", raising=False)
    monkeypatch_module.delenv("CRUXIBLE_MCP_TOOL_ALLOWLIST", raising=False)
    return create_server()


@pytest.fixture(scope="module")
def monkeypatch_module():  # type: ignore[no-untyped-def]
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


def test_every_tool_argument_model_forbids_extra_arguments(server) -> None:  # type: ignore[no-untyped-def]
    tools = server._tool_manager.list_tools()
    assert len(tools) > 50
    open_models = sorted(
        tool.name
        for tool in tools
        if tool.fn_metadata.arg_model.model_config.get("extra") != "forbid"
    )
    assert open_models == []


def test_every_tool_refuses_an_unknown_argument_naming_it_and_its_arguments(server) -> None:  # type: ignore[no-untyped-def]
    manager = server._tool_manager
    not_refused: list[str] = []
    for tool in asyncio.run(server.list_tools()):
        accepted = list(tool.inputSchema.get("properties", {}))
        try:
            asyncio.run(manager.call_tool(tool.name, {PROBE: 1}))
        except ToolError as error:
            message = str(error)
            if not (
                message.startswith(ARGUMENT_UNKNOWN_CODE)
                and repr(PROBE) in message
                and all(name in message for name in accepted)
            ):
                not_refused.append(f"{tool.name}: {message[:160]}")
            continue
        not_refused.append(f"{tool.name}: ran")
    assert not_refused == []


def test_a_misspelt_argument_is_named_with_the_real_ones(server) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError) as refused:
        asyncio.run(server._tool_manager.call_tool("cruxible_get", {"ref": "x", "detials": "why"}))
    message = str(refused.value)
    assert message.startswith(f"{ARGUMENT_UNKNOWN_CODE}: tool 'cruxible_get' takes no argument")
    assert "'detials'" in message and "detail" in message and "ref" in message
