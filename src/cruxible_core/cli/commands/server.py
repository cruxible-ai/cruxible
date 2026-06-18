"""CLI commands for live daemon status and diagnostics."""

from __future__ import annotations

import click

from cruxible_core.cli.commands._common import _emit_json, _get_client
from cruxible_core.cli.main import handle_errors


@click.group("server")
def server_group() -> None:
    """Inspect live daemon state."""


@server_group.command("info")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def server_info_cmd(output_json: bool) -> None:
    """Show live daemon metadata such as transport policy and state dir."""
    client = _get_client()
    if client is None:
        raise click.UsageError(
            "server info requires server mode; set --server-url or --server-socket"
        )
    result = client.server_info()
    if output_json:
        _emit_json(result.model_dump(mode="python"))
        return
    click.echo(f"Version: {result.version}")
    click.echo(f"Server required: {'yes' if result.server_required else 'no'}")
    click.echo(f"Auth enabled: {'yes' if result.auth_enabled else 'no'}")
    click.echo(f"Auth required: {'yes' if result.auth_required else 'no'}")
    click.echo(f"State dir: {result.state_dir}")
    click.echo(f"Instances: {result.instance_count}")
