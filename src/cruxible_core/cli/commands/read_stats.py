"""Latency-sensitive graph statistics CLI command."""

from __future__ import annotations

from typing import Any

import click

from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    console,
    json_option,
)
from cruxible_core.cli.main import handle_errors


def _local_stats(instance: Any) -> Any:
    from cruxible_core.service import service_stats

    return service_stats(instance)


@click.command("stats")
@json_option
@handle_errors
def stats_cmd(output_json: bool) -> None:
    """Display entity and relationship counts for this instance."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.stats(instance_id),
        _local_stats,
    )
    entity_count = result.entity_count
    edge_count = result.edge_count
    entity_counts = result.entity_counts
    relationship_counts = result.relationship_counts
    status_counts = result.status_counts
    head_snapshot_id = result.head_snapshot_id
    read_revision = result.read_revision
    if output_json:
        _emit_json(
            {
                "entity_count": entity_count,
                "edge_count": edge_count,
                "entity_counts": entity_counts,
                "relationship_counts": relationship_counts,
                "status_counts": status_counts,
                "head_snapshot_id": head_snapshot_id,
                "read_revision": read_revision,
            }
        )
        return
    click.echo(f"Graph: {entity_count} entities, {edge_count} edges")
    click.echo(f"Read revision: {read_revision}")
    if head_snapshot_id:
        click.echo(f"Head snapshot: {head_snapshot_id}")
    from cruxible_core.cli.formatting import stats_table

    console.print(stats_table(entity_counts, relationship_counts))
