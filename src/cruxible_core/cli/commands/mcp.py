"""``cruxible mcp``: the stdio MCP server, reachable from the ``cruxible`` script.

Registry launchers run a PyPI package through its own name (``uvx cruxible``),
so the server needs a subcommand there as well as the ``cruxible-mcp`` script.
Both run the same entry point and read the same environment; the root CLI's
remembered context is not passed to the server.
"""

from __future__ import annotations

import click


@click.command("mcp")
@click.option(
    "--env-file",
    "env_file",
    default=None,
    help=(
        "Load Cruxible settings from a file (for example DIR/cruxible.env, also "
        "CRUXIBLE_ENV_FILE). Process environment variables take precedence."
    ),
)
@click.pass_context
def mcp_cmd(ctx: click.Context, env_file: str | None) -> None:
    """Serve the Cruxible MCP tools over stdio."""
    from cruxible_core.mcp.server import main

    root_env_file = (
        ctx.obj["mcp_env_file"] if isinstance(ctx.obj, dict) and "mcp_env_file" in ctx.obj else None
    )
    selected = env_file if env_file is not None else root_env_file
    main(argv=(), env_file=selected)
