"""FastMCP server factory and entry point."""

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
from cruxible_core.mcp.kit_surface import resolve_kit_surface
from cruxible_core.mcp.permissions import (
    TOOL_PERMISSIONS,
    PermissionMode,
    init_permissions,
    validate_tool_permissions,
)
from cruxible_core.mcp.tools import register_tools
from cruxible_core.server.config import ServerSettings, resolve_server_settings

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
- `READ_ONLY`: query, inspect, validate — no graph or config mutations.
  Reads are not side-effect-free: `cruxible_query` and gate checks persist
  their receipt rows, which is how their evidence survives the call.
- `GOVERNED_WRITE`: READ_ONLY + workflow and procedure runs, governed
  proposals, feedback, attestations, outcomes, decision records, and source
  artifact registration.
- `GRAPH_WRITE`: GOVERNED_WRITE + raw graph mutation, canonical workflow
  apply, governed resolution and lifecycle transitions, and snapshot creation.
- `ADMIN` (default): all tools, including instance lifecycle, backup and
  restore, locks, active-config additions
  (`cruxible_add_constraint`, `cruxible_add_decision_policy`), replacing the
  ACTIVE config wholesale
  (`cruxible_reload_config`, `cruxible_state_pull_apply`), and published-state
  trust boundaries

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

## Relationship State Semantics

- `live` includes active direct/unreviewed relationships and approved relationships.
- `accepted` includes only relationships approved through review.
- `pending` includes staged relationships awaiting review.
- `reviewable` includes both live and pending relationships.
- A direct relationship write is live/unreviewed unless it is written with
  `pending=true`; direct evidence does not make an edge accepted.
- Candidate-group members are review records, not traversable graph edges. They
  become accepted graph relationships only when the group is approved.
- Do not treat pending or candidate-group claims as accepted, and do not approve
  your own claims when the operating policy requires independent review.
"""


def _build_instructions(
    mode: PermissionMode,
    *,
    curation: ToolCuration,
    advertised: set[str],
    transport_error: str | None = None,
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
    if transport_error is not None:
        section += (
            "\n\n## Daemon Transport Unusable\n\n"
            "The tool listing above is static and always answers. The daemon "
            f"transport is NOT usable: {transport_error}\n"
            "Tool CALLS will refuse until the transport is configured; the "
            "listing is not evidence that the daemon is reachable."
        )

    return BASE_INSTRUCTIONS + section


_MAX_ADVERTISED_NAMES_IN_REFUSAL = 12


def _advertised_summary(advertised: set[str]) -> str:
    """Name the advertised tools, or point at tools/list once the list is long."""
    names = sorted(advertised)
    if len(names) > _MAX_ADVERTISED_NAMES_IN_REFUSAL:
        return f"{len(names)} tools are advertised; call tools/list for the catalog"
    return f"Advertised tools: {', '.join(names)}"


def _uncurated_tool_message(
    name: str,
    *,
    mode: PermissionMode,
    curation: ToolCuration,
    advertised: set[str],
) -> str:
    """Teaching refusal for a registered tool that this server does not advertise."""
    required = TOOL_PERMISSIONS.get(name)
    if required is not None and mode < required:
        return (
            f"Tool '{name}' is not available: it requires permission mode "
            f"{required.name}, and this server runs in {mode.name}. Restart the "
            "server with CRUXIBLE_MODE set to a mode at or above "
            f"{required.name}. {_advertised_summary(advertised)}."
        )
    reason = f"the active MCP tool profile '{curation.profile}'"
    fix = "Restart the server with CRUXIBLE_MCP_PROFILE=full to widen the surface"
    if curation.allowlist is not None and name not in curation.allowlist:
        reason = "the explicit CRUXIBLE_MCP_TOOLS allowlist"
        fix = "Restart the server with '{name}' added to CRUXIBLE_MCP_TOOLS, or unset it".format(
            name=name
        )
    return (
        f"Tool '{name}' is not available: it is excluded by {reason}. "
        f"{fix}. {_advertised_summary(advertised)}."
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
    """Enforce curation at BOTH MCP protocol seams: tools/list and tools/call.

    Filtering only the listing left the surface curated in name only: a client
    that already knew a tool name could still call it over the real stdio
    protocol, because the low-level ``tools/call`` handler dispatches straight
    into the FastMCP tool manager. In-process tests missed this precisely
    because they exercised ``server.list_tools()`` and never the wire path.

    What that bypass actually reached, precisely. It skipped BOTH the profile /
    allowlist filter AND the permission-mode filter, because
    :func:`advertised_tool_names` is the only place either is applied to the
    tool surface. LOCAL execution was still refused in depth:
    ``runtime.api`` calls ``check_permission`` inside every gated operation, so
    a local-mode call landed on that floor. The REMOTE path had no such floor —
    ``handlers._dispatch_remote_or_local`` forwards to the HTTP client without
    any local permission check (there is not a single ``check_permission`` call
    anywhere in ``mcp/handlers.py`` or ``mcp/tools.py``; the mode is enforced
    only by ``runtime.api``, which the remote branch never enters). So an MCP
    server started at ``CRUXIBLE_MODE=read_only`` but pointed at a
    ``graph_write``/``admin`` daemon could execute writes over the wire: the
    daemon authorizes what its own credential permits, and the client-side mode
    that was supposed to hold the line was never consulted. That is the
    escalation this gate closes.

    Both seams are closed here, and the call seam is closed inside the tool
    manager so ``server.call_tool()`` and the protocol handler (which delegates
    to it) share one gate rather than two that can drift.
    """
    manager = getattr(server, "_tool_manager")
    # Materialize the immutable advertised catalog during server creation so
    # tools/list only returns local metadata and never initializes call paths.
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
    # FastMCP registers the low-level ListToolsRequest handler during __init__.
    # Re-register it so protocol clients see the same curated catalog as
    # in-process server.list_tools() callers.
    lowlevel_server = getattr(server, "_mcp_server")
    lowlevel_server.list_tools()(list_curated_tools)

    # FastMCP.call_tool() -> ToolManager.call_tool(), and the low-level
    # ListToolsRequest/CallToolRequest handlers were bound to FastMCP's methods
    # during __init__. Gating the manager therefore covers the protocol path and
    # the in-process path with one check.
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
    """Create and configure the cruxible-core MCP server."""
    # Tool LISTING must never depend on daemon transport. A misconfigured or
    # absent transport used to abort create_server(), so the process died before
    # it could answer tools/list and agent hosts saw an empty (or hung) surface.
    # The failure is carried to tools/call instead, where the caller actually
    # needs the daemon and can be taught what to fix.
    try:
        settings = resolve_server_settings()
        transport_error: str | None = None
    except ConfigError as exc:
        settings = ServerSettings()
        transport_error = str(exc)
    mode = init_permissions()
    server = FastMCP(
        name=f"cruxible v{__version__}",
        instructions="",
    )
    # Resolved from LOCAL state only (see mcp.kit_surface): tools/list must keep
    # answering on a host with no reachable daemon, so a self-describing
    # description is never bought with a network dependency at listing time.
    kit_surface = resolve_kit_surface()
    registered = register_tools(
        server,
        offload_sync_calls=settings.enabled,
        kit_surface=kit_surface,
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
    # NOTE: Runtime FastMCP parity check is in main(), not here.
    # create_server() must remain safe for async embedders.
    return server


# The exact parameter lists this server's curation gate is written against.
# Both are PRIVATE FastMCP surfaces, so an ``mcp`` package bump can change them
# without any deprecation. If that happens the gate must fail loudly at startup
# rather than silently forwarding uncurated calls or breaking every tool call.
_CALL_TOOL_SEAM_PARAMS = ("self", "name", "arguments", "context", "convert_result")
_LIST_TOOLS_SEAM_PARAMS = ("self",)


def _mcp_package_version() -> str:
    try:
        return metadata.version("mcp")
    except metadata.PackageNotFoundError:  # pragma: no cover - env without metadata
        return "unknown"


def _validate_curation_seams(server: FastMCP) -> None:
    """Fail at STARTUP if the FastMCP seams the curation gate wraps have moved.

    The gate wraps two private FastMCP internals: ``ToolManager.call_tool`` (the
    single chokepoint both the protocol handler and ``FastMCP.call_tool`` reach)
    and the ``tools/list`` handler registration. A signature or wiring change in
    a new ``mcp`` release would otherwise surface as a total outage on the first
    tool call — or, worse for a security gate, as a silently un-wrapped seam.
    Checked here so ``main()`` refuses to start with a named reason.
    """
    mcp_version = _mcp_package_version()

    for owner, attr, expected in (
        (ToolManager, "call_tool", _CALL_TOOL_SEAM_PARAMS),
        (FastMCP, "list_tools", _LIST_TOOLS_SEAM_PARAMS),
    ):
        actual = tuple(inspect.signature(getattr(owner, attr)).parameters)
        if actual != expected:
            raise ConfigError(
                f"MCP tool curation cannot be enforced: {owner.__name__}.{attr}() "
                f"has parameters {actual}, but the curation gate is written "
                f"against {expected} (mcp package {mcp_version}). Update "
                "cruxible_core.mcp.server._install_tool_curation to match the "
                "new FastMCP seam before serving."
            )

    manager = getattr(server, "_tool_manager")
    if not getattr(manager.call_tool, "_cruxible_curated", False):
        raise ConfigError(
            "MCP tools/call is not curated: the tool manager's call_tool was not "
            f"replaced by the curation gate (mcp package {mcp_version}). Refusing "
            "to serve an uncurated tool surface."
        )
    if not getattr(server.list_tools, "_cruxible_curated", False):
        raise ConfigError(
            "MCP tools/list is not curated: FastMCP.list_tools was not replaced "
            f"by the curation gate (mcp package {mcp_version}). Refusing to serve "
            "an uncurated tool surface."
        )

    lowlevel_server = getattr(server, "_mcp_server")
    for request_type in (mcp_types.ListToolsRequest, mcp_types.CallToolRequest):
        if request_type not in lowlevel_server.request_handlers:
            raise ConfigError(
                f"MCP protocol handler for {request_type.__name__} is not "
                f"registered (mcp package {mcp_version}); the curated surface "
                "would not be served."
            )


def validate_runtime_tools(server: FastMCP) -> None:
    """Compare FastMCP's actual tool list against TOOL_PERMISSIONS.

    Also verifies the curation seams are intact — see
    :func:`_validate_curation_seams`.

    Must be called from a sync context (no running event loop).
    """
    actual_tools = _registered_tool_names(server)
    validate_tool_permissions(list(actual_tools))
    _validate_curation_seams(server)


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
