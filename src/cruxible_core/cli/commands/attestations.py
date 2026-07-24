"""CLI commands for immutable claim attestations."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
from typing import Any, cast

import click
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.attestation.types import AttestationStance, ClaimKey
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _list_envelope,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import service_attestation_queue, service_list_attestations


@click.group("attest")
def attest_group() -> None:
    """Record and review observations against relationship claims."""


def _parse_object(raw: str | None, *, option: str) -> dict[str, Any] | None:
    if raw is None:
        return None
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
        assert payload is not None
        try:
            refs.append(contracts.EvidenceRef.model_validate(payload))
        except ValidationError as exc:
            raise click.BadParameter(f"--evidence-ref is invalid: {exc}") from exc
    return refs


def _parse_claim(raw: str | None) -> tuple[ClaimKey | None, dict[str, str | None]]:
    empty: dict[str, str | None] = {
        "relationship_type": None,
        "from_type": None,
        "from_id": None,
        "to_type": None,
        "to_id": None,
    }
    payload = _parse_object(raw, option="--claim")
    if payload is None:
        return None, empty
    required = set(empty)
    missing = sorted(required - payload.keys())
    extra = sorted(payload.keys() - required)
    if missing or extra or any(not isinstance(payload.get(key), str) for key in required):
        raise click.BadParameter(
            "--claim requires exactly relationship_type, from_type, from_id, to_type, and to_id"
        )
    values: dict[str, str | None] = {key: cast(str, payload[key]) for key in empty}
    return (
        (
            cast(str, values["relationship_type"]),
            cast(str, values["from_type"]),
            cast(str, values["from_id"]),
            cast(str, values["to_type"]),
            cast(str, values["to_id"]),
        ),
        values,
    )


def _result_payload(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return cast(dict[str, Any], result.model_dump(mode="json", exclude_none=True))
    return cast(dict[str, Any], result)


def _list_items(result: Any) -> list[dict[str, Any]]:
    return [
        (item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item)
        for item in result.items
    ]


@attest_group.command("record")
@click.option("--relationship", "relationship_type", required=True)
@click.option("--from-type", required=True)
@click.option("--from-id", required=True)
@click.option("--to-type", required=True)
@click.option("--to-id", required=True)
@click.option(
    "--stance",
    type=click.Choice(["support", "contradict", "unsure"]),
    required=True,
)
@click.option("--observed-at", required=True, help="ISO-8601 time when the world was observed.")
@click.option(
    "--evidence-ref",
    "evidence_refs",
    multiple=True,
    help="JSON evidence ref. Required for support and contradict; repeatable.",
)
@click.option("--edge-key", type=int, default=None)
@click.option("--properties", default=None, help="JSON properties for absent support only.")
@click.option("--note", default=None, help="Optional observation note; encouraged for unsure.")
@click.option("--idempotency-key", default=None, help="Retry-safe caller key.")
@json_option
@handle_errors
def attest_record(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    stance: str,
    observed_at: str,
    evidence_refs: tuple[str, ...],
    edge_key: int | None,
    properties: str | None,
    note: str | None,
    idempotency_key: str | None,
    output_json: bool,
) -> None:
    """Record one observation against a relationship claim."""
    parsed_evidence = _parse_evidence_refs(evidence_refs)
    parsed_properties = _parse_object(properties, option="--properties")
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.attest(
            instance_id,
            relationship_type=relationship_type,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            stance=cast(contracts.AttestationStance, stance),
            observed_at=observed_at,
            evidence_refs=parsed_evidence,
            edge_key=edge_key,
            properties=parsed_properties,
            note=note,
            idempotency_key=idempotency_key,
        ),
        lambda _instance: None,
        allow_local=False,
        command_name="attest record",
    )
    payload = _result_payload(result)
    if output_json:
        _emit_json(payload)
        return
    attestation = payload["attestation"]
    click.echo(f"Attestation {attestation['attestation_id']} recorded.")
    click.echo(f"  Stance: {attestation['stance']}")
    if payload.get("created_claim"):
        click.echo("  Pending claim created.")
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


@attest_group.command("list")
@click.option("--claim", default=None, help="Claim coordinates as one JSON object.")
@click.option(
    "--stance",
    type=click.Choice(["support", "contradict", "unsure"]),
    default=None,
)
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0))
@json_option
@handle_errors
def attest_list(
    claim: str | None,
    stance: str | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List immutable attestation history."""
    claim_key, coordinates = _parse_claim(claim)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.list_attestations(
            instance_id,
            **coordinates,
            stance=cast(contracts.AttestationStance | None, stance),
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_list_attestations(
            instance,
            claim_key=claim_key,
            stance=cast(AttestationStance | None, stance),
            limit=limit,
            offset=offset,
        ),
    )
    items = _list_items(result)
    if output_json:
        _emit_json(
            {
                "items": items,
                **_list_envelope(
                    result,
                    item_count=len(items),
                    limit=limit,
                    offset=offset,
                ),
            }
        )
        return
    for item in items:
        record = item["attestation"]
        markers = []
        if item.get("unresolved_target"):
            markers.append("unresolved_target")
        if item.get("edge_key_mismatch"):
            markers.append("edge_key_mismatch")
        if item.get("stale_content"):
            markers.append("stale_content")
        suffix = f" ({', '.join(markers)})" if markers else ""
        click.echo(
            f"{record['attestation_id']} {record['stance']} "
            f"observed={record['observed_at']}{suffix}"
        )
    click.echo(f"{len(items)} of {result.total} attestation(s) shown.")


@attest_group.command("queue")
@click.option("--limit", default=100, type=click.IntRange(min=1))
@click.option("--offset", default=0, type=click.IntRange(min=0))
@json_option
@handle_errors
def attest_queue(limit: int, offset: int, output_json: bool) -> None:
    """List live claims with open current-content contradictions."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.attestation_queue(
            instance_id,
            limit=limit,
            offset=offset,
        ),
        lambda instance: service_attestation_queue(instance, limit=limit, offset=offset),
    )
    items = _list_items(result)
    if output_json:
        _emit_json(
            {
                "items": items,
                **_list_envelope(
                    result,
                    item_count=len(items),
                    limit=limit,
                    offset=offset,
                ),
            }
        )
        return
    for item in items:
        click.echo(
            f"{item['from_type']}:{item['from_id']} "
            f"-[{item['relationship_type']}]-> {item['to_type']}:{item['to_id']} "
            f"open={item['open_contradict_count']} "
            f"actors={item['distinct_contradicting_actor_count']}"
        )
    click.echo(f"{len(items)} of {result.total} queued claim(s) shown.")


@attest_group.command("resolve")
@click.argument("attestation_id")
@click.option(
    "--verdict",
    type=click.Choice(["upheld", "corrected", "invalidated"]),
    required=True,
)
@click.option("--note", default=None)
@click.option("--follow-up-receipt", "follow_up_receipt_id", default=None)
@json_option
@handle_errors
def attest_resolve(
    attestation_id: str,
    verdict: str,
    note: str | None,
    follow_up_receipt_id: str | None,
    output_json: bool,
) -> None:
    """Append a reviewer disposition to one attestation."""
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.resolve_attestation(
            instance_id,
            attestation_id,
            verdict=cast(contracts.AttestationVerdict, verdict),
            note=note,
            follow_up_receipt_id=follow_up_receipt_id,
        ),
        lambda _instance: None,
        allow_local=False,
        command_name="attest resolve",
    )
    payload = _result_payload(result)
    if output_json:
        _emit_json(payload)
        return
    disposition = payload["disposition"]
    click.echo(
        f"Attestation {attestation_id} disposition "
        f"{disposition['disposition_id']} recorded: {verdict}."
    )
    if payload.get("receipt_id"):
        click.echo(f"  Receipt: {payload['receipt_id']}")


__all__ = [
    "attest_group",
    "attest_list",
    "attest_queue",
    "attest_record",
    "attest_resolve",
]
