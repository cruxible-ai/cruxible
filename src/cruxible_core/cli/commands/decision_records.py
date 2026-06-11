"""CLI commands for decision-record lifecycle and event inspection."""

from __future__ import annotations

from typing import Any, cast

import click

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.decision.types import DecisionEvent, DecisionRecord
from cruxible_core.service import (
    service_abandon_decision_record,
    service_create_decision_record,
    service_finalize_decision_record,
    service_get_decision_record,
    service_list_decision_events,
    service_list_decision_records,
)


def _record_payload(record: DecisionRecord | dict[str, Any]) -> dict[str, Any]:
    if isinstance(record, dict):
        return record
    return record.model_dump(mode="python")


def _event_payload(event: DecisionEvent | dict[str, Any]) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    return event.model_dump(mode="python")


def _result_record_payload(result: Any) -> dict[str, Any]:
    return _record_payload(result.record)


def _result_events_payload(result: Any) -> list[dict[str, Any]]:
    return [_event_payload(event) for event in result.events]


def _echo_record(record: dict[str, Any]) -> None:
    click.echo(f"{record['decision_record_id']} [{record['status']}] {record['question']}")
    if record.get("subject_type") or record.get("subject_id"):
        click.echo(f"Subject: {record.get('subject_type') or ''}:{record.get('subject_id') or ''}")
    if record.get("decision_class"):
        click.echo(f"Decision class: {record['decision_class']}")
    if record.get("final_decision"):
        click.echo(f"Final decision: {record['final_decision']}")
    if record.get("rationale"):
        click.echo(f"Rationale: {record['rationale']}")
    if record.get("abandoned_reason"):
        click.echo(f"Abandoned reason: {record['abandoned_reason']}")


@click.group("decision-record")
def decision_records_cmd() -> None:
    """Manage decision records and their logged receipts."""


@decision_records_cmd.command("create")
@click.option("--question", required=True, help="Question or decision being evaluated.")
@click.option("--subject-type", default=None, help="Optional subject type.")
@click.option("--subject-id", default=None, help="Optional subject identifier.")
@click.option(
    "--opened-by",
    type=click.Choice(["human", "agent", "service"]),
    default="human",
    show_default=True,
)
@json_option
@handle_errors
def create_cmd(
    question: str,
    subject_type: str | None,
    subject_id: str | None,
    opened_by: str,
    output_json: bool,
) -> None:
    """Create an open decision record."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.create_decision_record(
            instance_id,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
        ),
        lambda instance: service_create_decision_record(
            instance,
            question=question,
            subject_type=subject_type,
            subject_id=subject_id,
            opened_by=opened_by,
        ),
    )
    payload = _result_record_payload(result)
    if output_json:
        _emit_json({"record": payload})
        return
    _echo_record(payload)


@decision_records_cmd.command("get")
@click.option("--id", "decision_record_id", required=True, help="Decision record ID.")
@click.option("--events/--no-events", "include_events", default=True, show_default=True)
@json_option
@handle_errors
def get_cmd(decision_record_id: str, include_events: bool, output_json: bool) -> None:
    """Fetch one decision record."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.get_decision_record(
            instance_id,
            decision_record_id,
            include_events=include_events,
        ),
        lambda instance: service_get_decision_record(
            instance,
            decision_record_id,
            include_events=include_events,
        ),
    )
    record = _result_record_payload(result)
    events = _result_events_payload(result)
    if output_json:
        _emit_json({"record": record, "events": events})
        return
    _echo_record(record)
    if include_events:
        click.echo(f"Events: {len(events)}")
        for event in events:
            click.echo(
                f"  {event['sequence']}: {event['command']} "
                f"{event['status']} receipt={event.get('receipt_id') or '-'}"
            )


@decision_records_cmd.command("list")
@click.option("--status", type=click.Choice(["open", "finalized", "abandoned"]), default=None)
@click.option("--subject-type", default=None)
@click.option("--subject-id", default=None)
@click.option(
    "--decision-class",
    type=click.Choice(["recommended", "rejected", "deferred", "escalated"]),
    default=None,
)
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def list_cmd(
    status: str | None,
    subject_type: str | None,
    subject_id: str | None,
    decision_class: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List decision records."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_decision_records(
            instance_id,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_decision_records(
            instance,
            status=status,
            subject_type=subject_type,
            subject_id=subject_id,
            decision_class=decision_class,
            limit=limit,
            offset=offset,
        ),
    )
    records = [_record_payload(record) for record in cast(Any, result).items]
    if output_json:
        _emit_json({"items": records})
        return
    if not records:
        click.echo("No decision records found.")
        return
    for record in records:
        click.echo(f"{record['decision_record_id']} [{record['status']}] {record['question']}")


@decision_records_cmd.command("events")
@click.option("--id", "decision_record_id", default=None, help="Decision record ID.")
@click.option("--receipt", "receipt_id", default=None, help="Receipt ID.")
@click.option("--trace", "trace_id", default=None, help="Trace ID.")
@click.option("--status", type=click.Choice(["success", "error"]), default=None)
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0), help="Rows to skip.")
@json_option
@handle_errors
def events_cmd(
    decision_record_id: str | None,
    receipt_id: str | None,
    trace_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List decision-record events."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_decision_events(
            instance_id,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_decision_events(
            instance,
            decision_record_id=decision_record_id,
            receipt_id=receipt_id,
            trace_id=trace_id,
            status=status,
            limit=limit,
            offset=offset,
        ),
    )
    events = [_event_payload(event) for event in cast(Any, result).items]
    if output_json:
        _emit_json({"items": events})
        return
    if not events:
        click.echo("No decision events found.")
        return
    for event in events:
        click.echo(
            f"{event['decision_record_id']} #{event['sequence']} "
            f"{event['command']} {event['status']} receipt={event.get('receipt_id') or '-'}"
        )


@decision_records_cmd.command("finalize")
@click.option("--id", "decision_record_id", required=True, help="Decision record ID.")
@click.option("--final-decision", required=True, help="Final decision text.")
@click.option(
    "--decision-class",
    required=True,
    type=click.Choice(["recommended", "rejected", "deferred", "escalated"]),
)
@click.option("--rationale", default="", help="Decision rationale.")
@json_option
@handle_errors
def finalize_cmd(
    decision_record_id: str,
    final_decision: str,
    decision_class: contracts.DecisionClass,
    rationale: str,
    output_json: bool,
) -> None:
    """Finalize an open decision record."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.finalize_decision_record(
            instance_id,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        ),
        lambda instance: service_finalize_decision_record(
            instance,
            decision_record_id,
            final_decision=final_decision,
            decision_class=decision_class,
            rationale=rationale,
        ),
    )
    payload = _result_record_payload(result)
    if output_json:
        _emit_json({"record": payload})
        return
    _echo_record(payload)


@decision_records_cmd.command("abandon")
@click.option("--id", "decision_record_id", required=True, help="Decision record ID.")
@click.option("--reason", default="", help="Reason for abandoning the record.")
@json_option
@handle_errors
def abandon_cmd(decision_record_id: str, reason: str, output_json: bool) -> None:
    """Abandon an open decision record."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.abandon_decision_record(
            instance_id,
            decision_record_id,
            reason=reason,
        ),
        lambda instance: service_abandon_decision_record(
            instance,
            decision_record_id,
            reason=reason,
        ),
    )
    payload = _result_record_payload(result)
    if output_json:
        _emit_json({"record": payload})
        return
    _echo_record(payload)
