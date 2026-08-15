"""FastMCP server for the Playbill-only agent surface."""

from __future__ import annotations

import inspect
import sys
from importlib import metadata
from typing import Any

import structlog
from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.tool_manager import ToolManager
from mcp.types import Tool as MCPTool

from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.mcp.curation import (
    ToolCuration,
    advertised_tool_names,
    resolve_tool_curation,
)
from cruxible_core.mcp.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    init_permissions,
    validate_tool_permissions,
)
from cruxible_core.mcp.tools import register_tools
from cruxible_core.server.config import ServerSettings, resolve_server_settings

BASE_INSTRUCTIONS = """\\
# cruxible Playbill

Playbill is deterministic, governed state with no LLM inside. Agents propose;
accepted laws, principals, attestations, and compare-and-set settlement decide
what becomes canonical.

Start by allocating a host with cruxible_playbill_host_create, bootstrap public
principals with cruxible_playbill_init, then use proposal, review, approval,
activation, query, and explain tools. Body storage is inert. A proposal is not
accepted state, an approval is not activation, and diagnostics never carry
authority.

The daemon retains four temporary transport tiers while the destructive pivot
is in progress: READ_ONLY, GOVERNED_WRITE, GRAPH_WRITE, and ADMIN. These only
control endpoint reachability; Playbill principals and acceptance laws control
semantic authority.
"""


def _build_instructions(
    mode: PermissionMode,
    *,
    curation: ToolCuration,
    advertised: set[str],
    transport_error: str | None = None,
) -> str:
    denied = sorted(name for name, tier in TOOL_PERMISSIONS.items() if mode < tier)
    hidden = sorted(set(TOOL_PERMISSIONS) - advertised - set(denied))
    section = (
        f"\n\n## Current transport tier: {mode.name}\n\n"
        f"Available tools: {', '.join(sorted(advertised))}"
    )
    if curation.active:
        section += f"\nActive MCP profile: {curation.profile}"
    if hidden:
        section += f"\nHidden by curation: {', '.join(hidden)}"
    if denied:
        section += f"\nDenied at this tier: {', '.join(denied)}"
    if transport_error is not None:
        section += (
            "\n\nThe configured daemon transport is unusable: "
            f"{transport_error}. Tool listing is static and does not prove reachability."
        )
    return BASE_INSTRUCTIONS + section


def _uncurated_tool_message(
    name: str,
    *,
    mode: PermissionMode,
    curation: ToolCuration,
    advertised: set[str],
) -> str:
    required = TOOL_PERMISSIONS.get(name)
    if required is not None and mode < required:
        return (
            f"Tool '{name}' requires transport tier {required.name}; "
            f"this server runs at {mode.name}."
        )
    return (
        f"Tool '{name}' is excluded by MCP profile '{curation.profile}'. "
        f"Advertised tools: {', '.join(sorted(advertised))}."
    )


def _registered_tool_names(server: FastMCP) -> set[str]:
    manager = getattr(server, "_tool_manager")
    return {tool.name for tool in manager.list_tools()}


def _install_tool_curation(
    server: FastMCP,
    advertised: set[str],
    *,
    mode: PermissionMode,
    curation: ToolCuration,
) -> None:
    manager = getattr(server, "_tool_manager")
    catalog = [
        MCPTool(
            name=tool.name,
            title=tool.title,
            description=tool.description,
            inputSchema=tool.parameters,
            outputSchema=tool.output_schema,
            annotations=tool.annotations,
            icons=tool.icons,
            _meta=tool.meta,
        )
        for tool in manager.list_tools()
        if tool.name in advertised
    ]

    async def list_curated_tools() -> list[MCPTool]:
        return list(catalog)

    list_curated_tools._cruxible_curated = True  # type: ignore[attr-defined]
    server.list_tools = list_curated_tools  # type: ignore[method-assign]
    lowlevel_server = getattr(server, "_mcp_server")
    lowlevel_server.list_tools()(list_curated_tools)

    inner_call_tool = manager.call_tool

    async def curated_call_tool(
        name: str,
        arguments: dict[str, Any],
        context: Any | None = None,
        convert_result: bool = False,
    ) -> Any:
        if name not in advertised and manager.get_tool(name) is not None:
            raise ToolError(
                _uncurated_tool_message(
                    name,
                    mode=mode,
                    curation=curation,
                    advertised=advertised,
                )
            )
        return await inner_call_tool(
            name,
            arguments,
            context=context,
            convert_result=convert_result,
        )

    curated_call_tool._cruxible_curated = True  # type: ignore[attr-defined]
    manager.call_tool = curated_call_tool


def create_server() -> FastMCP:
    try:
        settings = resolve_server_settings()
        transport_error: str | None = None
    except ConfigError as exc:
        settings = ServerSettings()
        transport_error = str(exc)
    mode = init_permissions()
    server = FastMCP(name=f"cruxible v{__version__}", instructions="")
    registered = register_tools(
        server,
        offload_sync_calls=settings.enabled,
    )
    validate_tool_permissions(registered)
    curation = resolve_tool_curation()
    advertised = advertised_tool_names(
        mode=mode,
        registered_tools=set(registered),
        curation=curation,
    )
    server._mcp_server.instructions = _build_instructions(
        mode,
        curation=curation,
        advertised=advertised,
        transport_error=transport_error,
    )
    _install_tool_curation(server, advertised, mode=mode, curation=curation)
    return server


_CALL_TOOL_SEAM_PARAMS = ("self", "name", "arguments", "context", "convert_result")
_LIST_TOOLS_SEAM_PARAMS = ("self",)


def _mcp_package_version() -> str:
    try:
        return metadata.version("mcp")
    except metadata.PackageNotFoundError:
        return "unknown"


def _validate_curation_seams(server: FastMCP) -> None:
    mcp_version = _mcp_package_version()
    for owner, attr, expected in (
        (ToolManager, "call_tool", _CALL_TOOL_SEAM_PARAMS),
        (FastMCP, "list_tools", _LIST_TOOLS_SEAM_PARAMS),
    ):
        actual = tuple(inspect.signature(getattr(owner, attr)).parameters)
        if actual != expected:
            raise ConfigError(
                f"MCP curation seam {owner.__name__}.{attr} has parameters {actual}, "
                f"expected {expected} (mcp {mcp_version})"
            )
    manager = getattr(server, "_tool_manager")
    if not getattr(manager.call_tool, "_cruxible_curated", False):
        raise ConfigError("MCP tools/call is not curated")
    if not getattr(server.list_tools, "_cruxible_curated", False):
        raise ConfigError("MCP tools/list is not curated")
    lowlevel = getattr(server, "_mcp_server")
    for request_type in (mcp_types.ListToolsRequest, mcp_types.CallToolRequest):
        if request_type not in lowlevel.request_handlers:
            raise ConfigError(f"MCP protocol handler missing for {request_type.__name__}")


def validate_runtime_tools(server: FastMCP) -> None:
    validate_tool_permissions(list(_registered_tool_names(server)))
    _validate_curation_seams(server)


def configure_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    configure_structlog()
    server = create_server()
    validate_runtime_tools(server)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
