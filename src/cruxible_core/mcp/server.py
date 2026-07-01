"""FastMCP server factory and entry point."""

from __future__ import annotations

import sys
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from cruxible_core import __version__
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
from cruxible_core.server.config import resolve_server_settings

BASE_INSTRUCTIONS = """\
# cruxible-core

Hard state for AI agents: typed, governed, durable graph state. No LLM inside.
You (the AI agent) provide intelligence; cruxible provides deterministic
execution with proof via receipts.

## Start Here

This server exposes deterministic state-building and query tools.
Workflow guidance belongs client-side in agent skills or playbooks, not in MCP prompts.

**No config yet?**
- inspect the user's data first
- write a YAML config
- `cruxible_validate`
- `cruxible_init`
- `cruxible_lock_workflow`
- `cruxible_run_workflow` / `cruxible_apply_workflow`

**Existing graph?**
- `cruxible_evaluate`
- `cruxible_query`
- `cruxible_query_inline`
- `cruxible_list`
- `cruxible_receipt`
- `cruxible_feedback` / `cruxible_feedback_from_query`
- `cruxible_batch_direct_write` for dry-run/apply of structured direct state payloads

## Permission Modes

The server runs in one of four cumulative permission modes controlled by
the `CRUXIBLE_MODE` environment variable:
- `READ_ONLY`: query, inspect, validate — no graph or config mutations
- `GOVERNED_WRITE`: READ_ONLY + receipt-persisting workflow runs,
  governed proposal, and feedback surfaces
- `GRAPH_WRITE`: GOVERNED_WRITE + raw graph mutation, canonical workflow
  apply, and proposal resolution
- `ADMIN` (default): all tools available including ingest and config mutation

If a tool call is denied, the error message indicates the required mode.

## Config Syntax (YAML)

You must write a YAML config before initializing. Sections:

### entity_types
- Dict keyed by type name. Graph properties default to `type: string` and optional.
- Mark the ID property with `primary_key: true` (on the property, not the entity).
- Use `{}` for optional string fields and `required: true` for required non-ID fields.
- Properties support `enum: [...]`, `enum_ref`, `indexed: true`, and explicit `type`.

Example:
```yaml
entity_types:
  Vehicle:
    properties:
      vehicle_id: {primary_key: true}
      make: {}
  Part:
    properties:
      part_number: {primary_key: true}
      name: {}
```

### relationships
- `name`, `from`/`to` (entity type names)
- `properties` (typed, same as entities), `cardinality` (one|many)
- `reverse_name` (optional reverse relationship name)

### named_queries
- `entry_point` (entity type + optional filter)
- `traversal` steps: `relationship`, `direction` (outgoing|incoming|both),
  `filter`, `constraint`, `max_depth`

### constraints
- Rule expressions, e.g. `replaces.FROM.category == replaces.TO.category`
- `severity`: warning | error

### workflows
- Prefer workflows for deterministic loading and repeatable execution.
- Canonical workflows use `cruxible_lock_workflow`, `cruxible_run_workflow`,
  and `cruxible_apply_workflow`.
- Governed proposal workflows use `cruxible_propose_workflow`.

### ingestion
- Legacy compatibility path for older configs.
- One mapping per data file
- Entity mappings: `entity_type`, `id_column`, `column_map`
- Relationship mappings: `relationship_type`, `from_column`, `to_column`,
  `column_map` (for edge properties)
- `column_map` renames CSV columns to property names: `{csv_column: property_name}`

## Error Convention

Tools raise errors on failure — the MCP protocol returns them
with an error flag. Check tool call success before processing results.
"""


def _build_instructions(
    mode: PermissionMode,
    *,
    curation: ToolCuration,
    advertised: set[str],
) -> str:
    """Build server instructions with a dynamic permission mode section."""
    denied = sorted(name for name, tier in TOOL_PERMISSIONS.items() if mode < tier)
    hidden = sorted(set(TOOL_PERMISSIONS) - advertised - set(denied))

    tool_list = ", ".join(sorted(advertised))
    section = f"\n\n## Current Permission Mode: {mode.name}\n\nAvailable tools: {tool_list}"
    if curation.active:
        section += f"\nActive MCP tool profile: {curation.profile}"
        if curation.allowlist is not None:
            section += f"\nExplicit tool allowlist: {', '.join(sorted(curation.allowlist))}"
    if hidden:
        section += f"\nHidden by MCP curation: {', '.join(hidden)}"
    if denied:
        section += f"\nDenied tools (insufficient mode): {', '.join(denied)}"

    return BASE_INSTRUCTIONS + section


def _registered_tool_names(server: FastMCP) -> set[str]:
    manager = getattr(server, "_tool_manager")
    return {tool.name for tool in manager.list_tools()}


def _install_list_tools_filter(server: FastMCP, advertised: set[str]) -> None:
    """Filter MCP tools/list without removing registered tool handlers."""
    original_list_tools = server.list_tools

    async def list_curated_tools() -> list[Any]:
        tools = await original_list_tools()
        return [tool for tool in tools if tool.name in advertised]

    server.list_tools = list_curated_tools  # type: ignore[method-assign]
    # FastMCP registers the low-level ListToolsRequest handler during __init__.
    # Re-register it so protocol clients see the same curated catalog as
    # in-process server.list_tools() callers.
    lowlevel_server = getattr(server, "_mcp_server")
    lowlevel_server.list_tools()(list_curated_tools)


def create_server() -> FastMCP:
    """Create and configure the cruxible-core MCP server."""
    resolve_server_settings()
    mode = init_permissions()
    server = FastMCP(
        name=f"cruxible-core v{__version__}",
        instructions="",
    )
    registered = register_tools(server)
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
    )
    _install_list_tools_filter(server, advertised)
    # NOTE: Runtime FastMCP parity check is in main(), not here.
    # create_server() must remain safe for async embedders.
    return server


def validate_runtime_tools(server: FastMCP) -> None:
    """Compare FastMCP's actual tool list against TOOL_PERMISSIONS.

    Must be called from a sync context (no running event loop).
    """
    actual_tools = _registered_tool_names(server)
    validate_tool_permissions(list(actual_tools))


def configure_structlog() -> None:
    """Reconfigure structlog for JSON audit output to stderr (production)."""
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
    """Entry point for the cruxible-core MCP server."""
    configure_structlog()
    server = create_server()
    validate_runtime_tools(server)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
