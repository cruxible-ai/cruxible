"""CLI commands for published states and pullable overlays."""

from __future__ import annotations

import json
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
    service_state_diff,
    service_state_diff_artifact,
    service_state_health,
    service_state_status,
)


@click.group("state")
def state_group() -> None:
    """Compare, publish, and track state across coordinates."""


_DIFF_COORDINATE_HELP = (
    "Coordinates are 'current', a snapshot id (snap_ + 16 hex, exactly as "
    "`cruxible snapshot list` prints it), 'upstream' (the verified materialized "
    "tracked release), or 'origin' (clone provenance). A release you have not "
    "pulled is NOT a coordinate -- materialize it with `cruxible state "
    "pull-apply` first; pull-preview owns transport and foreign-byte "
    "verification."
)


@state_group.command("diff", epilog=_DIFF_COORDINATE_HELP)
@click.argument("from_coordinate", required=False)
@click.argument("to_coordinate", required=False)
@click.option(
    "--section",
    "sections",
    multiple=True,
    type=click.Choice(["entities", "edges", "procedures"]),
    help="Restrict the diff to these sections (repeatable).",
)
@click.option(
    "--entity-type", "entity_types", multiple=True, help="Restrict entities to these types."
)
@click.option(
    "--relationship-type",
    "relationship_types",
    multiple=True,
    help="Restrict edges to these relationship types.",
)
@click.option(
    "--bucket",
    "buckets",
    multiple=True,
    type=click.Choice(["added", "removed", "changed", "ambiguous", "identity_conflict"]),
    help="Report only these buckets (counts stay whole).",
)
@click.option("--changed-only", is_flag=True, default=False, help="Suppress added/removed items.")
@click.option(
    "--max-items",
    type=int,
    default=None,
    help="Per-bucket cap for the returned view; the persisted artifact is never capped.",
)
@click.option(
    "--artifact",
    "artifact_digest",
    default=None,
    help="Re-read a persisted diff artifact by its diff_digest instead of computing one.",
)
@json_option
@handle_errors
def state_diff_cmd(
    from_coordinate: str | None,
    to_coordinate: str | None,
    sections: tuple[str, ...],
    entity_types: tuple[str, ...],
    relationship_types: tuple[str, ...],
    buckets: tuple[str, ...],
    changed_only: bool,
    max_items: int | None,
    artifact_digest: str | None,
    output_json: bool,
) -> None:
    """Diff state between two coordinates.

    With no arguments this is parent-of-head to current: "what did the last
    committed transition do, plus anything since". `commit_graph_snapshot`
    advances live state in the same boundary that writes the snapshot, so
    head-to-current would be the empty diff by construction.
    """
    if artifact_digest is not None:
        artifact = _dispatch_cli_instance(
            lambda client, instance_id: client.state_diff_artifact(instance_id, artifact_digest),
            lambda instance: service_state_diff_artifact(instance, artifact_digest),
        )
        _emit_json(_as_payload(artifact.content))
        return

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_diff(
            instance_id,
            from_coordinate=from_coordinate,
            to_coordinate=to_coordinate,
            sections=list(sections) or None,
            entity_types=list(entity_types) or None,
            relationship_types=list(relationship_types) or None,
            buckets=list(buckets) or None,
            changed_only=changed_only,
            max_items_per_bucket=max_items,
        ),
        lambda instance: service_state_diff(
            instance,
            from_coordinate=from_coordinate,
            to_coordinate=to_coordinate,
            sections=tuple(sections) or None,
            entity_types=tuple(entity_types) or None,
            relationship_types=tuple(relationship_types) or None,
            buckets=tuple(buckets) or None,
            changed_only=changed_only,
            **({"max_items_per_bucket": max_items} if max_items is not None else {}),
        ),
    )
    if output_json:
        _emit_json(_as_payload(result))
        return
    _render_state_diff(_as_payload(result))


def _as_payload(result: Any) -> dict[str, Any]:
    """Normalize the server contract model or the local dataclass into a dict."""
    if hasattr(result, "model_dump"):
        return dict(result.model_dump(mode="json"))
    if is_dataclass(result) and not isinstance(result, type):
        return dict(asdict(result))
    return dict(result)


def _render_state_diff(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    click.echo(
        f"{payload['from_coordinate']['kind']}"
        f"({_coordinate_label(payload['from_coordinate'])}) -> "
        f"{payload['to_coordinate']['kind']}"
        f"({_coordinate_label(payload['to_coordinate'])})"
    )
    if payload.get("default_basis"):
        click.echo(f"  default basis: {payload['default_basis']}")
    click.echo(f"  diff digest:   {payload['diff_digest']}")
    click.echo(f"  artifact:      {payload['artifact_ref']['path']}")
    click.echo(f"  trust:         {payload['artifact_trust']}  liveness: {payload['liveness']}")
    if payload["normalizations"]:
        click.echo(f"  normalizations: {', '.join(payload['normalizations'])}")
    if not payload["artifact_complete"]:
        click.secho(
            "  returned view is BOUNDED: this is not a reviewable plan; read the "
            "artifact with --artifact for the complete body.",
            fg="yellow",
        )
    click.echo(
        "  totals: "
        f"added={summary['added']} removed={summary['removed']} "
        f"changed={summary['changed']} (annotation_only={summary['annotation_only']}) "
        f"unchanged={summary['unchanged']} "
        f"ambiguous={summary['ambiguous_from']}/{summary['ambiguous_to']} "
        f"identity_conflict={summary['identity_conflict']}"
    )
    for entry in payload["omitted_sections"]:
        click.secho(
            f"  omitted section '{entry['section']}': {entry['side']} side is "
            f"{entry['from_status'] if entry['side'] != 'to' else entry['to_status']}",
            fg="yellow",
        )
    for name, section in sorted(payload["sections"].items()):
        counts = section["counts"]
        click.echo(f"{name}:")
        click.echo(
            f"  added={counts['added']} removed={counts['removed']} "
            f"changed={counts['changed']} unchanged={counts['unchanged']}"
        )
        for bucket, accounting in sorted(section["view"].items()):
            if accounting["truncated"]:
                click.secho(
                    f"  {bucket}: showing {accounting['returned']} of {accounting['total']}",
                    fg="yellow",
                )
        diagnostics = section.get("diagnostics") or {}
        excluded = diagnostics.get("excluded_boundary_stubs")
        if excluded and (excluded["from"] or excluded["to"]):
            click.echo(f"  boundary stubs excluded: from={excluded['from']} to={excluded['to']}")
        for type_name, tally in sorted(_counts_by_type(section).items()):
            click.echo(f"  {type_name}: +{tally['added']} -{tally['removed']} ~{tally['changed']}")
        for item in section["changed"][:10]:
            click.echo(f"  ~ {_item_label(item)} [{', '.join(item['channels'])}]")
            for change in (item.get("properties") or {}).get("changes", [])[:5]:
                click.echo(
                    f"      {change['property']}: "
                    f"{_value_label(change['from_value'])} -> "
                    f"{_value_label(change['to_value'])}"
                )
        for item in section["added"][:10]:
            click.echo(f"  + {_item_label(item)}")
        for item in section["removed"][:10]:
            click.echo(f"  - {_item_label(item)}")


def _counts_by_type(section: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Added/removed/changed per entity or relationship type, from the shown items."""
    tallies: dict[str, dict[str, int]] = {}
    for bucket in ("added", "removed", "changed"):
        for item in section[bucket]:
            type_name = item.get("relationship_type") or item.get("entity_type") or "procedure"
            tally = tallies.setdefault(type_name, {"added": 0, "removed": 0, "changed": 0})
            tally[bucket] += 1
    return tallies


def _value_label(value: Any) -> str:
    if isinstance(value, dict) and value.get("elided") is True:
        return f"<elided {value['byte_count']}B {value['value_digest']}>"
    return json.dumps(value, default=str)


def _coordinate_label(coordinate: dict[str, Any]) -> str:
    identity = coordinate.get("identity") or {}
    for key in ("snapshot_id", "release_id", "head_snapshot_id"):
        if identity.get(key):
            return str(identity[key])
    return str(coordinate.get("spec", "?"))


def _item_label(item: dict[str, Any]) -> str:
    if "procedure_id" in item:
        return str(item["procedure_id"])
    if "entity_type" in item:
        return f"{item['entity_type']}:{item['entity_id']}"
    return (
        f"{item['from_type']}:{item['from_id']} -[{item['relationship_type']}]-> "
        f"{item['to_type']}:{item['to_id']}"
    )


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
    for warning in result.warnings:
        click.echo(f"Warning: {warning}", err=True)
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
    click.echo(f"  withdrawn:      {groups['withdrawn_count']}")
    click.echo(f"  resolved:       {groups['resolved_count']}")
    click.echo(f"  total:          {groups['total_count']}")
    click.echo(
        f"  unresolved age: oldest={_fmt_age(groups['oldest_unresolved_age_seconds'])} "
        f"newest={_fmt_age(groups['newest_unresolved_age_seconds'])}"
    )

    click.echo("Signals: supports pending review under the evidence guard")
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


_REPAIR_HELP = (
    "Repair mode: re-apply the release ALREADY tracked, to restore a "
    "materialized upstream that was damaged locally. Normally refused as a "
    "no-op; the local copy's digest verification is skipped because that is "
    "the check the damage trips. Claim ids are preserved."
)


@state_group.command("pull-preview")
@click.option("--repair", is_flag=True, default=False, help=_REPAIR_HELP)
@handle_errors
def state_pull_preview_cmd(repair: bool) -> None:
    """Preview pulling a newer upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_pull_preview(
            instance_id,
            force_repair=repair,
        ),
        lambda instance: service_pull_state_preview(instance, force_repair=repair),
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
@click.option("--repair", is_flag=True, default=False, help=_REPAIR_HELP)
@handle_errors
def state_pull_apply_cmd(apply_digest: str, repair: bool) -> None:
    """Apply a previewed upstream release into the current overlay."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.state_pull_apply(
            instance_id,
            expected_apply_digest=apply_digest,
            force_repair=repair,
        ),
        lambda instance: service_pull_state_apply(
            instance,
            expected_apply_digest=apply_digest,
            force_repair=repair,
        ),
        allow_local=False,
        command_name="state pull-apply",
    )
    click.echo(f"Pulled release {result.release_id}")
    click.echo(f"Pre-pull snapshot: {result.pre_pull_snapshot_id}")
