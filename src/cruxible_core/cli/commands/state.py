"""CLI commands for published states and pullable overlays."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _activate_server_instance,
    _dispatch_cli,
    _dispatch_cli_instance,
    _emit_json,
    _get_client,
    _print_active_instance_change,
    _print_active_instance_unchanged,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    service_create_state_overlay,
    service_publish_state,
    service_pull_state_apply,
    service_pull_state_preview,
    service_state_health,
    service_state_status,
)


@click.group("state")
def state_group() -> None:
    """Publish immutable states and manage pullable overlays."""


@state_group.command("publish")
@click.option("--transport-ref", required=True, help="Transport ref, e.g. file://... or oci://...")
@click.option("--state-id", required=True, help="Stable published state identifier.")
@click.option("--release-id", required=True, help="User-supplied release identifier.")
@click.option(
    "--compatibility",
    type=click.Choice(["data_only", "additive_schema", "breaking"]),
    default="data_only",
    show_default=True,
    help="Compatibility classification for the published release.",
)
@handle_errors
def state_publish_cmd(
    transport_ref: str,
    state_id: str,
    release_id: str,
    compatibility: contracts.StateCompatibility,
) -> None:
    """Publish the current root state instance as an immutable release bundle."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_publish(
            instance_id,
            transport_ref=transport_ref,
            state_id=state_id,
            release_id=release_id,
            compatibility=compatibility,
        ),
        lambda instance: service_publish_state(
            instance,
            transport_ref=transport_ref,
            state_id=state_id,
            release_id=release_id,
            compatibility=compatibility,
        ),
        allow_local=False,
        command_name="state publish",
    )
    click.echo(f"Published {result.manifest.state_id}:{result.manifest.release_id}")
    click.echo(f"  snapshot={result.manifest.snapshot_id}")
    click.echo(f"  compatibility={result.manifest.compatibility}")


@state_group.command("create-overlay")
@click.option("--transport-ref", help="Transport ref, e.g. file://... or oci://...")
@click.option(
    "--state-ref",
    help="State alias, e.g. kev-reference or kev-reference@2026-03-27.",
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
def create_state_overlay_cmd(
    transport_ref: str | None,
    state_ref: str | None,
    kit: str | None,
    no_kit: bool,
    root_dir: str | None,
    activate: bool,
) -> None:
    """Create a new local overlay instance from a published state release."""
    effective_root_dir = root_dir
    if _get_client() is not None and effective_root_dir is None:
        effective_root_dir = str(Path.cwd())
    result = _dispatch_cli(
        lambda client: client.create_state_overlay(
            root_dir=effective_root_dir or str(Path.cwd()),
            transport_ref=transport_ref,
            state_ref=state_ref,
            kit=kit,
            no_kit=no_kit,
        ),
        lambda: service_create_state_overlay(
            transport_ref=transport_ref,
            state_ref=state_ref,
            kit=kit,
            no_kit=no_kit,
            root_dir=Path(effective_root_dir) if effective_root_dir is not None else Path.cwd(),
        ),
        allow_local=False,
        command_name="state create-overlay",
    )
    instance_id = (
        result.instance_id
        if isinstance(result, contracts.StateOverlayResult)
        else str(result.instance.get_root_path())
    )
    click.echo(f"Created overlay for {result.manifest.state_id}:{result.manifest.release_id}")
    click.echo(f"Instance ID: {instance_id}")
    if isinstance(result, contracts.StateOverlayResult):
        if activate:
            _print_active_instance_change(_activate_server_instance(result.instance_id))
        else:
            _print_active_instance_unchanged()


@state_group.command("status")
@handle_errors
def state_status_cmd() -> None:
    """Show upstream tracking metadata for the current instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_status(instance_id),
        service_state_status,
    )
    if result.upstream is None:
        click.echo("This instance is not tracking an upstream published state.")
        return
    click.echo(f"State: {result.upstream.state_id}")
    click.echo(f"Release: {result.upstream.release_id}")
    if result.upstream.requested_source_ref is not None:
        click.echo(f"Requested source: {result.upstream.requested_source_ref}")
    if result.upstream.requested_transport_ref is not None:
        click.echo(f"Requested transport: {result.upstream.requested_transport_ref}")
    click.echo(f"Tracking transport: {result.upstream.transport_ref}")
    click.echo(f"Snapshot: {result.upstream.snapshot_id}")


def _state_health_payload(result: Any) -> dict[str, Any]:
    """Normalize the server contract or local service dataclass into JSON dict."""
    if isinstance(result, contracts.StateHealthResult):
        return result.model_dump(mode="json")
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return dict(result)


@state_group.command("health")
@json_option
@handle_errors
def state_health_cmd(output_json: bool) -> None:
    """Show read-only deterministic state-health maintenance signals."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_health(instance_id),
        service_state_health,
    )
    payload = _state_health_payload(result)
    if output_json:
        _emit_json(payload)
        return

    groups = payload["groups"]
    signals = payload["signals"]
    provenance = payload["provenance"]
    freshness = payload["freshness"]
    integrity = payload["integrity"]

    click.echo(f"Captured at: {payload['captured_at']}")
    click.echo(f"Head snapshot: {payload.get('head_snapshot_id') or '(none)'}")

    click.echo("Groups:")
    click.echo(f"  pending_review: {groups['pending_review_count']}")
    click.echo(f"  applying:       {groups['applying_count']}")
    click.echo(f"  auto_resolved:  {groups['auto_resolved_count']}")
    click.echo(f"  resolved:       {groups['resolved_count']}")
    click.echo(f"  total:          {groups['total_count']}")
    click.echo(
        f"  unresolved age: oldest={_fmt_age(groups['oldest_unresolved_age_seconds'])} "
        f"newest={_fmt_age(groups['newest_unresolved_age_seconds'])}"
    )

    click.echo("Signals:")
    unevidenced_support = signals["unevidenced_support_by_source"]
    if unevidenced_support:
        click.echo("  unevidenced_support_by_source:")
        for source, count in sorted(unevidenced_support.items()):
            click.echo(f"    {source}: {count}")
    else:
        click.echo("  unevidenced_support_by_source: -")

    click.echo("Provenance (edges):")
    click.echo(f"  direct_write:   {provenance['direct_write_edge_count']}")
    click.echo(f"  group_backed:   {provenance['group_backed_edge_count']}")
    click.echo(f"  other_source:   {provenance['other_source_edge_count']}")
    click.echo(f"  total:          {provenance['total_edge_count']}")

    click.echo("Freshness:")
    click.echo(
        f"  source_artifacts: {freshness['source_artifact_count']} "
        f"(oldest {_fmt_age(freshness['oldest_source_artifact_age_seconds'])}s)"
    )
    click.echo(
        f"  provider_traces:  {freshness['provider_trace_count']} "
        f"(oldest {_fmt_age(freshness['oldest_provider_trace_age_seconds'])}s)"
    )
    click.echo(f"  config_compatible: {freshness['config_compatible']}")
    for warning in freshness["config_warnings"]:
        click.secho(f"    warning: {warning}", fg="yellow")

    click.echo("Integrity:")
    click.echo(f"  orphan_entities:  {integrity['orphan_entity_count']}")
    click.echo(f"  unused_entity_types:       {', '.join(integrity['unused_entity_types']) or '-'}")
    click.echo(
        f"  unused_relationship_types: {', '.join(integrity['unused_relationship_types']) or '-'}"
    )
    click.echo(f"  configuration_locked: {integrity['configuration_locked']}")


def _fmt_age(value: Any) -> str:
    """Render an age-in-seconds value for the table, or '-' when None."""
    if value is None:
        return "-"
    return f"{value:.0f}"


@state_group.command("pull-preview")
@handle_errors
def state_pull_preview_cmd() -> None:
    """Preview pulling a newer upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_pull_preview(instance_id),
        service_pull_state_preview,
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


@state_group.command("pull-apply")
@click.option("--apply-digest", required=True, help="Apply digest returned by pull-preview.")
@handle_errors
def state_pull_apply_cmd(apply_digest: str) -> None:
    """Apply a previewed upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_pull_apply(
            instance_id,
            expected_apply_digest=apply_digest,
        ),
        lambda instance: service_pull_state_apply(instance, expected_apply_digest=apply_digest),
        allow_local=False,
        command_name="state pull-apply",
    )
    click.echo(f"Pulled release {result.release_id}")
    click.echo(f"Pre-pull snapshot: {result.pre_pull_snapshot_id}")
