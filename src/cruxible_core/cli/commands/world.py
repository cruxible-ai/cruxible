"""CLI commands for published worlds and pullable overlays."""

from __future__ import annotations

from pathlib import Path

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _dispatch_cli_instance,
    _get_client,
    _print_active_instance_change,
    _print_active_instance_unchanged,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    service_create_world_overlay,
    service_publish_world,
    service_pull_world_apply,
    service_pull_world_preview,
    service_world_status,
)


@click.group("world")
def world_group() -> None:
    """Publish immutable worlds and manage pullable overlays."""


@world_group.command("publish")
@click.option("--transport-ref", required=True, help="Transport ref, e.g. file://... or oci://...")
@click.option("--world-id", required=True, help="Stable published world identifier.")
@click.option("--release-id", required=True, help="User-supplied release identifier.")
@click.option(
    "--compatibility",
    type=click.Choice(["data_only", "additive_schema", "breaking"]),
    default="data_only",
    show_default=True,
    help="Compatibility classification for the published release.",
)
@handle_errors
def world_publish_cmd(
    transport_ref: str,
    world_id: str,
    release_id: str,
    compatibility: contracts.WorldCompatibility,
) -> None:
    """Publish the current root world-model instance as an immutable release bundle."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.world_publish(
            instance_id,
            transport_ref=transport_ref,
            world_id=world_id,
            release_id=release_id,
            compatibility=compatibility,
        ),
        lambda instance: service_publish_world(
            instance,
            transport_ref=transport_ref,
            world_id=world_id,
            release_id=release_id,
            compatibility=compatibility,
        ),
        allow_local=False,
        command_name="world publish",
    )
    click.echo(f"Published {result.manifest.world_id}:{result.manifest.release_id}")
    click.echo(f"  snapshot={result.manifest.snapshot_id}")
    click.echo(f"  compatibility={result.manifest.compatibility}")


@world_group.command("create-overlay")
@click.option("--transport-ref", help="Transport ref, e.g. file://... or oci://...")
@click.option(
    "--world-ref",
    help="World alias, e.g. kev-reference or kev-reference@2026-03-27.",
)
@click.option(
    "--kit",
    help="Apply a checked-in local overlay kit, e.g. kev-triage.",
)
@click.option(
    "--no-kit",
    is_flag=True,
    help="Skip automatic kit application and create a bare overlay.",
)
@click.option(
    "--root-dir",
    default=None,
    help="Workspace root for the new overlay (defaults to current directory in server mode).",
)
@click.option(
    "--activate/--no-activate",
    default=True,
    help="Make the new server overlay the active CLI context instance.",
)
@handle_errors
def create_world_overlay_cmd(
    transport_ref: str | None,
    world_ref: str | None,
    kit: str | None,
    no_kit: bool,
    root_dir: str | None,
    activate: bool,
) -> None:
    """Create a new local overlay instance from a published world release."""
    effective_root_dir = root_dir
    if _get_client() is not None and effective_root_dir is None:
        effective_root_dir = str(Path.cwd())
    result = _dispatch_cli(
        lambda client: client.create_world_overlay(
            root_dir=effective_root_dir or str(Path.cwd()),
            transport_ref=transport_ref,
            world_ref=world_ref,
            kit=kit,
            no_kit=no_kit,
        ),
        lambda: service_create_world_overlay(
            transport_ref=transport_ref,
            world_ref=world_ref,
            kit=kit,
            no_kit=no_kit,
            root_dir=Path(effective_root_dir) if effective_root_dir is not None else Path.cwd(),
        ),
        allow_local=False,
        command_name="world create-overlay",
    )
    instance_id = result.instance_id if isinstance(result, contracts.WorldOverlayResult) else str(
        result.instance.get_root_path()
    )
    click.echo(f"Created overlay for {result.manifest.world_id}:{result.manifest.release_id}")
    click.echo(f"Instance ID: {instance_id}")
    if isinstance(result, contracts.WorldOverlayResult):
        if activate:
            _print_active_instance_change(_activate_server_instance(result.instance_id))
        else:
            _print_active_instance_unchanged()


@world_group.command("status")
@handle_errors
def world_status_cmd() -> None:
    """Show upstream tracking metadata for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.world_status(instance_id),
        service_world_status,
    )
    if result.upstream is None:
        click.echo("This instance is not tracking an upstream published world.")
        return
    click.echo(f"World: {result.upstream.world_id}")
    click.echo(f"Release: {result.upstream.release_id}")
    if result.upstream.requested_source_ref is not None:
        click.echo(f"Requested source: {result.upstream.requested_source_ref}")
    if result.upstream.requested_transport_ref is not None:
        click.echo(f"Requested transport: {result.upstream.requested_transport_ref}")
    click.echo(f"Tracking transport: {result.upstream.transport_ref}")
    click.echo(f"Snapshot: {result.upstream.snapshot_id}")


@world_group.command("pull-preview")
@handle_errors
def world_pull_preview_cmd() -> None:
    """Preview pulling a newer upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.world_pull_preview(instance_id),
        service_pull_world_preview,
    )
    click.echo(f"Current release: {result.current_release_id or '(none)'}")
    click.echo(f"Target release: {result.target_release_id}")
    click.echo(f"Compatibility: {result.compatibility}")
    click.echo(f"Apply digest: {result.apply_digest}")
    click.echo(
        f"Upstream delta: entities={result.upstream_entity_delta:+d} "
        f"edges={result.upstream_edge_delta:+d}"
    )
    if result.lock_changed:
        click.echo("Lock will change.")
    for warning in result.warnings:
        click.secho(f"Warning: {warning}", fg="yellow")
    for conflict in result.conflicts:
        click.secho(f"Conflict: {conflict}", fg="red")


@world_group.command("pull-apply")
@click.option("--apply-digest", required=True, help="Apply digest returned by pull-preview.")
@handle_errors
def world_pull_apply_cmd(apply_digest: str) -> None:
    """Apply a previewed upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.world_pull_apply(
            instance_id,
            expected_apply_digest=apply_digest,
        ),
        lambda instance: service_pull_world_apply(instance, expected_apply_digest=apply_digest),
        allow_local=False,
        command_name="world pull-apply",
    )
    click.echo(f"Pulled release {result.release_id}")
    click.echo(f"Pre-pull snapshot: {result.pre_pull_snapshot_id}")
