"""``cruxible mcp``: the stdio MCP server, reachable from the ``cruxible`` script.

Registry launchers run a PyPI package through its own name (``uvx cruxible``),
so the server needs a subcommand there as well as the ``cruxible-mcp`` script.
Both run the same entry point and read the same environment; the root CLI's
remembered context is not passed to the server.
"""

from __future__ import annotations

import click


@click.command("mcp")
def mcp_cmd() -> None:
    """Serve the Cruxible MCP tools over stdio."""
    from cruxible_core.mcp.server import main

    main()
