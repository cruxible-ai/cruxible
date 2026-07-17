"""CLI commands for list subgroup and export."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, cast

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _echo_continuation_hint,
    _emit_json,
    _entities_from_payload,
    _feedback_from_payload,
    _list_envelope,
    _local_accept_continuation,
    _local_mint_continuation,
    _outcomes_from_payload,
    _require_local_instance,
    console,
    continuation_option,
    json_option,
    profile_option,
    state_option,
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
from cruxible_core.query.continuation import cursor_int
from cruxible_core.query.profiles import ReadProfile, profile_list_items
from cruxible_core.service import service_export_edges, service_list, service_list_traces
from cruxible_core.service.types import ListResult as ServiceListResult


def _parse_where_options(values: tuple[str, ...]) -> dict[str, dict[str, Any]] | None:
    if not values:
        return None
    where: dict[str, dict[str, Any]] = {}
    for raw in values:
        if ":in=" in raw:
            field, raw_value = raw.split(":in=", 1)
            operator = "in"
            value: Any = raw_value.split(",") if raw_value else []
        elif "~" in raw:
            field, value = raw.split("~", 1)
            operator = "contains"
        elif "=" in raw:
            field, value = raw.split("=", 1)
            operator = "eq"
        else:
            raise click.BadParameter("where must use FIELD=VALUE, FIELD~VALUE, or FIELD:in=A,B")
        if not field:
            raise click.BadParameter("where field must be non-empty")
        operators = where.setdefault(field, {})
        if operator in operators:
            raise click.BadParameter(f"duplicate where operator for field '{field}'")
        operators[operator] = value
    return where


@click.group("list")
def list_group() -> None:
    """List entities, receipts, or feedback."""


@list_group.command("entities")
@click.option("--type", "entity_type", required=True, help="Entity type to list.")
@click.option("--field", "fields", multiple=True, help="Property field to include. Repeatable.")
@click.option(
    "--where",
    "where_values",
    multiple=True,
    help="Property predicate: FIELD=VALUE, FIELD~VALUE, or FIELD:in=A,B. Repeatable.",
)
@click.option("--limit", default=50, help="Max entities to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@state_option
@profile_option
@continuation_option
@json_option
@handle_errors
def list_entities(
    entity_type: str,
    fields: tuple[str, ...],
    where_values: tuple[str, ...],
    limit: int,
    offset: int,
    state: str | None,
    profile: str,
    continuation: str | None,
    output_json: bool,
) -> None:
    """List entities of a given type."""
    projected_fields = list(fields) or None
    where = _parse_where_options(where_values)
    visibility_state = cast("contracts.QueryVisibilityState | None", state)
    filters: dict[str, Any] = {
        "resource_type": "entities",
        "entity_type": entity_type,
        "where": where,
        "relationship_state": visibility_state,
    }

    def _local_fetch(instance: Any) -> tuple[ServiceListResult, str | None]:
        local_offset = offset
        token = _local_accept_continuation(
            instance, surface="list", filters=filters, continuation=continuation
        )
        if token is not None:
            local_offset = cursor_int(token, "offset")
        result = service_list(
            instance,
            "entities",
            entity_type=entity_type,
            limit=limit,
            offset=local_offset,
            relationship_state=visibility_state,
            where=where,
            fields=projected_fields,
        )
        token_out = None
        if result.truncated and result.items:
            token_out = _local_mint_continuation(
                instance,
                surface="list",
                filters=filters,
                cursor={"offset": result.offset + len(result.items)},
            )
        return result, token_out

    result, continuation_token = _dispatch_cli_instance(
        lambda client, instance_id: (lambda r: (r, r.continuation_token))(
            client.list(
                instance_id,
                resource_type="entities",
                entity_type=entity_type,
                limit=limit,
                offset=offset,
                relationship_state=visibility_state,
                where=where,
                fields=projected_fields,
                continuation=continuation,
            )
        ),
        _local_fetch,
    )
    entities = (
        _entities_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        item_dicts = profile_list_items(
            [e.model_dump(mode="python") for e in entities],
            "entities",
            cast(ReadProfile, profile),
        )
        _emit_json(
            {
                "items": item_dicts,
                **_list_envelope(result, item_count=len(item_dicts), limit=limit, offset=offset),
                "continuation_token": continuation_token,
            }
        )
        return
    console.print(entities_table(entities, entity_type))
    click.echo(f"{len(entities)} entity(ies) shown.")
    _echo_continuation_hint(continuation_token)


@list_group.command("receipts")
@click.option("--query-name", default=None, help="Filter by query name.")
@click.option("--operation-type", default=None, help="Filter by operation type.")
@click.option("--limit", default=50, help="Max receipts to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def list_receipts(
    query_name: str | None,
    operation_type: str | None,
    limit: int,
    offset: int,
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
            offset=offset,
        ),
        lambda instance: service_list(
            instance,
            "receipts",
            query_name=query_name,
            operation_type=operation_type,
            limit=limit,
            offset=offset,
        ),
    )
    if output_json:
        _emit_json(
            {
                "items": result.items,
                **_list_envelope(result, item_count=len(result.items), limit=limit, offset=offset),
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
        _emit_json(
            {
                "items": traces,
                **_list_envelope(result, item_count=len(traces), limit=limit, offset=offset),
            }
        )
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
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def list_feedback(receipt_id: str | None, limit: int, offset: int, output_json: bool) -> None:
    """List feedback records."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="feedback",
            receipt_id=receipt_id,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list(
            instance, "feedback", receipt_id=receipt_id, limit=limit, offset=offset
        ),
    )
    records = (
        _feedback_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        item_dicts = [r.model_dump(mode="python") for r in records]
        _emit_json(
            {
                "items": item_dicts,
                **_list_envelope(result, item_count=len(item_dicts), limit=limit, offset=offset),
            }
        )
        return
    console.print(feedback_table(records))
    click.echo(f"{len(records)} record(s) shown.")


@list_group.command("outcomes")
@click.option("--receipt", "receipt_id", default=None, help="Filter by receipt ID.")
@click.option("--limit", default=50, help="Max records to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def list_outcomes(receipt_id: str | None, limit: int, offset: int, output_json: bool) -> None:
    """List outcome records."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list(
            instance_id,
            resource_type="outcomes",
            receipt_id=receipt_id,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list(
            instance, "outcomes", receipt_id=receipt_id, limit=limit, offset=offset
        ),
    )
    records = (
        _outcomes_from_payload(result.items)
        if isinstance(result, contracts.ListResult)
        else result.items
    )
    if output_json:
        item_dicts = [r.model_dump(mode="python") for r in records]
        _emit_json(
            {
                "items": item_dicts,
                **_list_envelope(result, item_count=len(item_dicts), limit=limit, offset=offset),
            }
        )
        return
    console.print(outcomes_table(records))
    click.echo(f"{len(records)} record(s) shown.")


@list_group.command("edges")
@click.option("--relationship", default=None, help="Filter by relationship type.")
@click.option(
    "--where",
    "where_values",
    multiple=True,
    help="Relationship property predicate: FIELD=VALUE, FIELD~VALUE, or FIELD:in=A,B. Repeatable.",
)
@click.option("--limit", default=50, help="Max edges to show.")
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@state_option
@profile_option
@continuation_option
@json_option
@handle_errors
def list_edges(
    relationship: str | None,
    where_values: tuple[str, ...],
    limit: int,
    offset: int,
    state: str | None,
    profile: str,
    continuation: str | None,
    output_json: bool,
) -> None:
    """List edges in the graph.

    By default every stored edge is returned (the inspection contract). Pass
    ``--state`` to gate edges by review+lifecycle: ``live`` hides
    rejected/closed edges, ``not-live`` surfaces exactly those, ``all`` returns
    everything.
    """
    where = _parse_where_options(where_values)
    visibility_state = cast("contracts.QueryVisibilityState | None", state)
    filters: dict[str, Any] = {
        "resource_type": "edges",
        "relationship_type": relationship,
        "where": where,
        "relationship_state": visibility_state,
    }

    def _local_fetch(instance: Any) -> tuple[ServiceListResult, str | None]:
        local_offset = offset
        token = _local_accept_continuation(
            instance, surface="list", filters=filters, continuation=continuation
        )
        if token is not None:
            local_offset = cursor_int(token, "offset")
        result = service_list(
            instance,
            "edges",
            relationship_type=relationship,
            limit=limit,
            offset=local_offset,
            relationship_state=visibility_state,
            where=where,
        )
        token_out = None
        if result.truncated and result.items:
            token_out = _local_mint_continuation(
                instance,
                surface="list",
                filters=filters,
                cursor={"offset": result.offset + len(result.items)},
            )
        return result, token_out

    result, continuation_token = _dispatch_cli_instance(
        lambda client, instance_id: (lambda r: (r, r.continuation_token))(
            client.list(
                instance_id,
                resource_type="edges",
                relationship_type=relationship,
                limit=limit,
                offset=offset,
                relationship_state=visibility_state,
                where=where,
                continuation=continuation,
            )
        ),
        _local_fetch,
    )
    if output_json:
        _emit_json(
            {
                "items": profile_list_items(result.items, "edges", cast(ReadProfile, profile)),
                **_list_envelope(result, item_count=len(result.items), limit=limit, offset=offset),
                "continuation_token": continuation_token,
            }
        )
        return
    console.print(edges_table(result.items))
    click.echo(f"{len(result.items)} edge(s) shown.")
    _echo_continuation_hint(continuation_token)


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
        relationship_type=relationship,
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
