"""CLI commands for persisted governed server context."""

from __future__ import annotations

import click

from cruxible_core.cli.commands._common import (
    _clear_persisted_cli_context,
    _emit_json,
    _load_persisted_cli_context,
    _persist_cli_context,
    _root_ctx_obj,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.server.config import resolve_server_settings


@click.group("context")
def connect_group() -> None:
    """Manage remembered governed server and instance context."""


@connect_group.command("show")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON.")
@handle_errors
def context_show(output_json: bool) -> None:
    """Show the resolved workspace-aware CLI context and each value's source."""
    obj = _root_ctx_obj()
    payload = {
        "server_url": obj.get("server_url"),
        "server_socket": obj.get("server_socket"),
        "instance_id": obj.get("instance_id"),
        "transport_source": obj.get("target_transport_source", "local"),
        "instance_source": obj.get("target_instance_source", "local"),
        "workspace": obj.get("playbill_workspace"),
        "workspace_source": obj.get("workspace_source", "local"),
        "workspace_binding_path": obj.get("workspace_binding_path"),
        "workspace_attached": bool(obj.get("workspace_attached")),
    }
    if output_json:
        _emit_json(payload)
        return
    transport_source = payload["transport_source"]
    instance_source = payload["instance_source"]
    if payload["server_url"]:
        click.echo(f"Server URL: {payload['server_url']} ({transport_source})")
    elif payload["server_socket"]:
        click.echo(f"Server socket: {payload['server_socket']} ({transport_source})")
    else:
        click.echo(f"Server: local ({transport_source})")
    click.echo(f"Instance ID: {payload['instance_id'] or '<none>'} ({instance_source})")
    attachment = "attached" if payload["workspace_attached"] else "not attached"
    click.echo(f"Workspace: {payload['workspace']} ({payload['workspace_source']}, {attachment})")


@connect_group.command("connect")
@click.option("--server-url", default=None, help="Remote Cruxible server base URL.")
@click.option("--server-socket", default=None, help="Local Cruxible server Unix socket path.")
@click.option("--instance-id", default=None, help="Optional opaque server-mode instance ID.")
@handle_errors
def context_connect(
    server_url: str | None,
    server_socket: str | None,
    instance_id: str | None,
) -> None:
    """Persist the current governed transport and optional instance."""
    existing = _load_persisted_cli_context()
    if server_url is not None or server_socket is not None:
        resolved_url = server_url
        resolved_socket = server_socket
    else:
        resolved_url = existing.server_url
        resolved_socket = existing.server_socket
    settings = resolve_server_settings(
        server_url=resolved_url,
        server_socket=resolved_socket,
        environ={},
    )
    if not settings.enabled:
        raise click.UsageError("Provide --server-url or --server-socket")
    transport_changed = (
        settings.server_url != existing.server_url
        or settings.server_socket != existing.server_socket
    )
    if instance_id is not None:
        resolved_instance_id = instance_id
    elif transport_changed:
        resolved_instance_id = None
    else:
        resolved_instance_id = existing.instance_id
    _persist_cli_context(
        server_url=settings.server_url,
        server_socket=settings.server_socket,
        instance_id=resolved_instance_id,
    )
    click.echo("Remembered governed CLI context.")
    if settings.server_url:
        click.echo(f"Server URL: {settings.server_url}")
    if settings.server_socket:
        click.echo(f"Server socket: {settings.server_socket}")
    if resolved_instance_id:
        click.echo(f"Instance ID: {resolved_instance_id}")


@connect_group.command("use")
@click.argument("instance_id")
@handle_errors
def context_use(instance_id: str) -> None:
    """Set the active governed instance ID."""
    existing = _load_persisted_cli_context()
    if not existing.server_url and not existing.server_socket:
        raise click.UsageError("Set a remembered server first with 'cruxible context connect'")
    _persist_cli_context(
        server_url=existing.server_url,
        server_socket=existing.server_socket,
        instance_id=instance_id,
    )
    click.echo(f"Active instance: {instance_id}")


@connect_group.command("clear")
@handle_errors
def context_clear() -> None:
    """Clear remembered governed CLI context."""
    _clear_persisted_cli_context()
    click.echo("Cleared remembered CLI context.")
