"""CLI subcommands for outcome resolution contracts.

These attach to the EXISTING ``cruxible outcome`` group (declared in
``feedback.py`` for the legacy ``record``/``profile``/``analyze`` lane) through
the lazy command registry in ``cli/main.py``; the legacy subcommands are
untouched and retire separately.
"""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
from typing import Any, cast

import click
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _list_envelope,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.resolution_contracts.types import ContractQueue, ContractStatus
from cruxible_core.service import (
    service_list_resolution_contracts,
    service_outcome_queue,
)


def _parse_object(raw: str, *, option: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"{option} must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise click.BadParameter(f"{option} must be a JSON object")
    return cast(dict[str, Any], payload)


def _parse_evidence_refs(raw_refs: tuple[str, ...]) -> list[contracts.EvidenceRef]:
    refs = []
    for raw in raw_refs:
        payload = _parse_object(raw, option="--evidence-ref")
        try:
            refs.append(contracts.EvidenceRef.model_validate(payload))
        except ValidationError as exc:
            raise click.BadParameter(f"--evidence-ref is invalid: {exc}") from exc
    return refs


def _result_payload(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return cast(dict[str, Any], result.model_dump(mode="json", exclude_none=True))
    return cast(dict[str, Any], result)


def _list_items(result: Any) -> list[dict[str, Any]]:
    return [
        (item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item)
        for item in result.items
    ]


@click.command("open")
@click.option("--entity-type", required=True, help="Subject entity type.")
@click.option("--entity-id", required=True, help="Subject entity ID.")
@click.option("--description", required=True, help="Free-text success criterion.")
@click.option("--check-at", required=True, help="ISO-8601 time when the outcome is first checked.")
@click.option("--expires-at", required=True, help="ISO-8601 time when the contract expires.")
@click.option("--measurement", required=True, help="JSON measurement declaration.")
@click.option("--idempotency-key", default=None, help="Retry-safe caller key.")
@json_option
@handle_errors
def outcome_open(
    entity_type: str,
    entity_id: str,
    description: str,
    check_at: str,
    expires_at: str,
    measurement: str,
    idempotency_key: str | None,
    output_json: bool,
) -> None:
    """Open a resolution contract on a subject before it is accepted."""
    parsed_measurement = _parse_object(measurement, option="--measurement")
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.open_outcome_contract(
            instance_id,
            entity_type=entity_type,
            entity_id=entity_id,
            description=description,
            check_at=check_at,
            expires_at=expires_at,
            measurement=parsed_measurement,
            idempotency_key=idempotency_key,
        ),
        lambda _instance: None,
        allow_local=False,
        command_name="outcome open",
    )
    payload = _result_payload(result)
    if output_json:
        _emit_json(payload)
        return
    contract = payload["contract"]
    click.echo(f"Resolution contract {contract['contract_id']} opened.")
    click.echo(f"  Subject: {contract['entity_type']}:{contract['entity_id']}")
    click.echo(f"  Check at: {contract['declaration']['check_at']}")
    click.echo(f"  Expires: {contract['declaration']['expires_at']}")
    if payload.get("idempotent_replay"):
        click.echo("  Idempotent replay of the original contract.")
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


@click.command("resolve")
@click.argument("contract_id")
@click.option(
    "--verdict",
    type=click.Choice(["satisfied", "contradicted", "indeterminate"]),
    required=True,
)
@click.option("--observed-at", required=True, help="ISO-8601 time when the world was observed.")
@click.option(
    "--evidence-ref",
    "evidence_refs",
    multiple=True,
    help="JSON evidence ref. Required for satisfied and contradicted; repeatable.",
)
@click.option("--note", default=None, help="Observation note; required for contradicted.")
@click.option(
    "--query-receipt",
    "resolving_query_receipt_id",
    default=None,
    help="Receipt of the query run that observed the outcome.",
)
@click.option(
    "--attestation",
    "resolving_attestation_ids",
    multiple=True,
    help="Attestation id backing the verdict; repeatable.",
)
@json_option
@handle_errors
def outcome_resolve(
    contract_id: str,
    verdict: str,
    observed_at: str,
    evidence_refs: tuple[str, ...],
    note: str | None,
    resolving_query_receipt_id: str | None,
    resolving_attestation_ids: tuple[str, ...],
    output_json: bool,
) -> None:
    """Record what reality said about one activated resolution contract."""
    parsed_evidence = _parse_evidence_refs(evidence_refs)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.resolve_outcome(
            instance_id,
            contract_id,
            verdict=cast(contracts.ResolutionVerdict, verdict),
            observed_at=observed_at,
            evidence_refs=parsed_evidence,
            note=note,
            resolving_query_receipt_id=resolving_query_receipt_id,
            resolving_attestation_ids=list(resolving_attestation_ids),
        ),
        lambda _instance: None,
        allow_local=False,
        command_name="outcome resolve",
    )
    payload = _result_payload(result)
    if output_json:
        _emit_json(payload)
        return
    resolution = payload["resolution"]
    click.echo(
        f"Contract {contract_id} resolved {resolution['verdict']} "
        f"(resolution {resolution['resolution_id']}, sequence {resolution['sequence']})."
    )
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


@click.command("dispose")
@click.argument("resolution_id")
@click.option("--verdict", type=click.Choice(["upheld", "overturned"]), required=True)
@click.option("--note", default=None, help="Reviewer note.")
@json_option
@handle_errors
def outcome_dispose(
    resolution_id: str,
    verdict: str,
    note: str | None,
    output_json: bool,
) -> None:
    """Uphold or overturn a recorded outcome; an overturn re-opens the contract."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.dispose_outcome_resolution(
            instance_id,
            resolution_id,
            verdict=cast(contracts.ResolutionDispositionVerdict, verdict),
            note=note,
        ),
        lambda _instance: None,
        allow_local=False,
        command_name="outcome dispose",
    )
    payload = _result_payload(result)
    if output_json:
        _emit_json(payload)
        return
    disposition = payload["disposition"]
    click.echo(
        f"Resolution {resolution_id} disposition "
        f"{disposition['disposition_id']} recorded: {verdict}."
    )
    if verdict == "overturned":
        click.echo("  Contract re-opened for one new resolution.")
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


@click.command("list")
@click.option("--entity-type", default=None, help="Filter to one subject entity type.")
@click.option("--entity-id", default=None, help="Filter to one subject entity ID.")
@click.option(
    "--status",
    type=click.Choice(["prepared", "open", "resolved"]),
    default=None,
    help="Filter the returned page by derived status.",
)
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0))
@json_option
@handle_errors
def outcome_list(
    entity_type: str | None,
    entity_id: str | None,
    status: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List resolution contracts with status, activation, and standing answer."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_outcome_contracts(
            instance_id,
            entity_type=entity_type,
            entity_id=entity_id,
            status=cast(contracts.ContractStatus | None, status),
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_resolution_contracts(
            instance,
            entity_type=entity_type,
            entity_id=entity_id,
            status=cast(ContractStatus | None, status),
            limit=limit,
            offset=offset,
        ),
    )
    items = _list_items(result)
    if output_json:
        _emit_json(
            {
                "items": items,
                **_list_envelope(result, item_count=len(items), limit=limit, offset=offset),
            }
        )
        return
    for item in items:
        contract = item["contract"]
        markers = []
        if item.get("expired"):
            markers.append("expired")
        if item.get("subject_content_drifted"):
            markers.append("subject_content_drifted")
        if not item.get("subject_present"):
            markers.append("subject_absent")
        suffix = f" ({', '.join(markers)})" if markers else ""
        click.echo(
            f"{contract['contract_id']} {item['status']} "
            f"{contract['entity_type']}:{contract['entity_id']} "
            f"check_at={contract['declaration']['check_at']}{suffix}"
        )
    click.echo(f"{len(items)} of {result.total} contract(s) shown.")


@click.command("due")
@click.option(
    "--queue",
    type=click.Choice(["due", "overdue", "contradicted"]),
    default="due",
    help="Which attention queue to read.",
)
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0))
@json_option
@handle_errors
def outcome_due(
    queue: str,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List outcomes due for checking, overdue, or contradicted and undisposed."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.outcome_due(
            instance_id,
            queue=cast(contracts.ContractQueue, queue),
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_outcome_queue(
            instance,
            queue=cast(ContractQueue, queue),
            limit=limit,
            offset=offset,
        ),
    )
    items = _list_items(result)
    if output_json:
        _emit_json(
            {
                "items": items,
                **_list_envelope(result, item_count=len(items), limit=limit, offset=offset),
            }
        )
        return
    for item in items:
        marker = " overdue" if item.get("overdue") else ""
        click.echo(
            f"{item['contract_id']} {item['entity_type']}:{item['entity_id']} "
            f"check_at={item['check_at']}{marker} — {item['description']}"
        )
    click.echo(f"{len(items)} of {result.total} queued contract(s) shown.")


__all__ = [
    "outcome_dispose",
    "outcome_due",
    "outcome_list",
    "outcome_open",
    "outcome_resolve",
]
