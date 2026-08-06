"""CLI reads for instance-local boundary telemetry."""

from __future__ import annotations

import click

from cruxible_core.cli.commands._common import _dispatch_cli_instance, _emit_json, json_option
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import service_telemetry_summary


@click.group("telemetry")
def telemetry_group() -> None:
    """Inspect aggregate traffic crossing core-owned surfaces."""


@telemetry_group.command("summary")
@json_option
@handle_errors
def telemetry_summary_cmd(output_json: bool) -> None:
    """Show per-surface call, error, payload-byte, and duration counters."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.telemetry_summary(instance_id),
        service_telemetry_summary,
    )
    payload = {
        "earliest_recorded_at": (
            result.earliest_recorded_at.isoformat()
            if result.earliest_recorded_at is not None
            else None
        ),
        "dropped_observations": result.dropped_observations,
        "dropped_events": result.dropped_events,
        "counters": [
            {
                "surface_name": counter.surface_name,
                "call_count": counter.call_count,
                "error_count": counter.error_count,
                "total_response_bytes": counter.total_response_bytes,
                "total_duration_ms": counter.total_duration_ms,
                "max_duration_ms": counter.max_duration_ms,
            }
            for counter in result.counters
        ],
    }
    if output_json:
        _emit_json(payload)
        return

    earliest = payload["earliest_recorded_at"] or "none"
    click.echo(f"Earliest recorded: {earliest}")
    # Printed only when non-zero: a standing "dropped: 0" line trains readers to
    # skip it, which is exactly the line they must not skip when it is not zero.
    if result.dropped_observations or result.dropped_events:
        click.echo(
            f"Counters are INCOMPLETE: dropped_observations="
            f"{result.dropped_observations} dropped_events={result.dropped_events}"
        )
    if not result.counters:
        click.echo("No boundary telemetry recorded.")
        return
    for counter in result.counters:
        click.echo(
            f"{counter.surface_name}: calls={counter.call_count} "
            f"errors={counter.error_count} bytes={counter.total_response_bytes} "
            f"total_ms={counter.total_duration_ms:.3f} max_ms={counter.max_duration_ms:.3f}"
        )
