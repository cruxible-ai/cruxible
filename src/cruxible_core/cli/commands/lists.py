"""CLI commands for list subgroup and export."""

from __future__ import annotations

import csv
from pathlib import Path

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _entities_from_payload,
    _feedback_from_payload,
    _outcomes_from_payload,
    _require_local_instance,
    console,
    json_option,
)
from cruxible_core.cli.formatting import (
    edges_table,
    entities_table,
    feedback_table,
    outcomes_table,
    receipts_table,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.errors import ConfigError
from cruxible_core.service import service_export_edges, service_list, service_list_traces


@click.group("list")
def list_group() -> None:
    """List entities, receipts, or feedback."""


@list_group.command("entities")
@click.option("--type", "entity_type", required=True, help="Entity type to list.")
@click.option("--limit", default=50, help="Max entities to show.")
@json_option
@handle_errors
def list_entities(entity_type: str, limit: int, output_json: bool) -> None:
    """List entities of a given type."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="entities",
            entity_type=entity_type,
            limit=limit,
        ),
        lambda instance: service_list(instance, "entities", entity_type=entity_type, limit=limit),
    )
    entities = (
        _entities_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        _emit_json(
            {
                "items": [e.model_dump(mode="python") for e in entities],
                "total": result.total,
            }
        )
        return
    console.print(entities_table(entities, entity_type))
    click.echo(f"{len(entities)} entity(ies) shown.")


@list_group.command("receipts")
@click.option("--query-name", default=None, help="Filter by query name.")
@click.option("--operation-type", default=None, help="Filter by operation type.")
@click.option("--limit", default=50, help="Max receipts to show.")
@json_option
@handle_errors
def list_receipts(
    query_name: str | None,
    operation_type: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """List receipt summaries."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="receipts",
            query_name=query_name,
            operation_type=operation_type,
            limit=limit,
        ),
        lambda instance: service_list(
            instance,
            "receipts",
            query_name=query_name,
            operation_type=operation_type,
            limit=limit,
        ),
    )
    if output_json:
        _emit_json(
            {
                "items": result.items,
                "total": result.total,
            }
        )
        return
    console.print(receipts_table(result.items))
    click.echo(f"{len(result.items)} receipt(s) shown.")


@list_group.command("traces")
@click.option("--workflow", "workflow_name", default=None, help="Filter by workflow name.")
@click.option("--provider", "provider_name", default=None, help="Filter by provider name.")
@click.option("--limit", default=100, type=click.IntRange(min=1), help="Max traces to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def list_traces(
    workflow_name: str | None,
    provider_name: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List provider execution trace summaries."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_traces(
            instance_id,
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_traces(
            instance,
            workflow_name=workflow_name,
            provider_name=provider_name,
            limit=limit,
            offset=offset,
        ),
    )
    traces = result.items
    if output_json:
        _emit_json({"items": traces, "total": result.total})
        return
    if not traces:
        click.echo("No traces found.")
        return
    for trace in traces:
        click.echo(
            "{trace_id}  {workflow_name}:{step_id}  {provider_name}  {created_at}".format(**trace)
        )
    click.echo(f"{result.total} trace(s) shown.")


@list_group.command("feedback")
@click.option("--receipt", "receipt_id", default=None, help="Filter by receipt ID.")
@click.option("--limit", default=50, help="Max records to show.")
@json_option
@handle_errors
def list_feedback(receipt_id: str | None, limit: int, output_json: bool) -> None:
    """List feedback records."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="feedback",
            receipt_id=receipt_id,
            limit=limit,
        ),
        lambda instance: service_list(instance, "feedback", receipt_id=receipt_id, limit=limit),
    )
    records = (
        _feedback_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        _emit_json(
            {
                "items": [r.model_dump(mode="python") for r in records],
                "total": result.total,
            }
        )
        return
    console.print(feedback_table(records))
    click.echo(f"{len(records)} record(s) shown.")


@list_group.command("outcomes")
@click.option("--receipt", "receipt_id", default=None, help="Filter by receipt ID.")
@click.option("--limit", default=50, help="Max records to show.")
@json_option
@handle_errors
def list_outcomes(receipt_id: str | None, limit: int, output_json: bool) -> None:
    """List outcome records."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="outcomes",
            receipt_id=receipt_id,
            limit=limit,
        ),
        lambda instance: service_list(instance, "outcomes", receipt_id=receipt_id, limit=limit),
    )
    records = (
        _outcomes_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        _emit_json(
            {
                "items": [r.model_dump(mode="python") for r in records],
                "total": result.total,
            }
        )
        return
    console.print(outcomes_table(records))
    click.echo(f"{len(records)} record(s) shown.")


@list_group.command("edges")
@click.option("--relationship", default=None, help="Filter by relationship type.")
@click.option("--limit", default=50, help="Max edges to show.")
@json_option
@handle_errors
def list_edges(relationship: str | None, limit: int, output_json: bool) -> None:
    """List edges in the graph."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="edges",
            relationship_type=relationship,
            limit=limit,
        ),
        lambda instance: service_list(
            instance,
            "edges",
            relationship_type=relationship,
            limit=limit,
        ),
    )
    if output_json:
        _emit_json(
            {
                "items": result.items,
                "total": result.total,
            }
        )
        return
    console.print(edges_table(result.items))
    click.echo(f"{len(result.items)} edge(s) shown.")


@click.group("export")
def export_group() -> None:
    """Export graph data to files."""


@export_group.command("edges")
@click.option(
    "--output",
    "-o",
    required=True,
    type=click.Path(dir_okay=False, writable=True),
    help="Output file path.",
)
@click.option("--relationship", default=None, help="Filter by relationship type.")
@click.option(
    "--exclude-rejected",
    is_flag=True,
    default=False,
    help="Exclude edges with rejected assertion review state.",
)
@handle_errors
def export_edges(output: str, relationship: str | None, exclude_rejected: bool) -> None:
    """Export all edges to CSV."""
    instance = _require_local_instance("export edges")
    result = service_export_edges(
        instance,
        relationship=relationship,
        exclude_rejected=exclude_rejected,
    )

    path = Path(output)
    try:
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.fieldnames)
            writer.writeheader()
            writer.writerows(result.rows)
    except OSError as exc:
        raise ConfigError(f"Failed to write {path}: {exc}") from exc

    click.echo(f"Exported {result.count} edge(s) to {path}")
