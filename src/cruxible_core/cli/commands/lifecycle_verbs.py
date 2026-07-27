"""CLI noun-verb surfaces for settled claim and entity lifecycle acts."""

from __future__ import annotations

import json
from typing import Any

import click
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.commands._common import _dispatch_cli_instance, _emit_json, json_option
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    service_retire_entity,
    service_retract_claim,
    service_supersede_claim,
    service_supersede_entity,
)


def _local_operator_context():
    """Lazy import: pulling server.auth_managed_entities at CLI import time
    closes a circular import chain (the module observes itself partially
    initialized) on some command import orders."""
    from cruxible_core.server.auth_managed_entities import local_operator_actor_context

    return local_operator_actor_context()


def _evidence_ref(raw: str | None) -> contracts.EvidenceRef | None:
    """Parse ``--evidence-ref`` into the typed contract model, or None.

    The two dispatch branches want different shapes, and all four verbs use them
    IDENTICALLY (an earlier split between the claim and entity verbs was an
    inconsistency, not a design): the HTTP client takes the typed model and
    serializes it wire-safely itself, while the local service takes a plain
    mapping for :func:`normalize_evidence_ref`.
    """
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("must decode to a JSON object")
        return contracts.EvidenceRef.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise click.BadParameter(f"--evidence-ref is invalid: {exc}") from exc


def _emit_lifecycle_result(result: Any, *, output_json: bool) -> None:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif hasattr(result, "claim"):
        payload = {
            "action": result.action,
            "claim": result.claim.model_dump(mode="json"),
            "reason": result.reason,
            "successor": (result.successor.model_dump(mode="json") if result.successor else None),
            "receipt_id": result.receipt_id,
        }
    else:
        payload = {
            "action": result.action,
            "entity": result.entity.model_dump(mode="json"),
            "reason": result.reason,
            "successor": (result.successor.model_dump(mode="json") if result.successor else None),
            "stranded_live_edge_count": result.stranded_live_edge_count,
            "receipt_id": result.receipt_id,
        }
    if output_json:
        _emit_json(payload)
        return
    subject = payload.get("claim") or payload.get("entity") or {}
    identity = subject.get("claim_id") or (
        f"{subject.get('entity_type')}:{subject.get('entity_id')}"
    )
    click.echo(f"{payload['action']} {identity} (receipt {payload.get('receipt_id')})")
    if payload.get("action") == "retire":
        click.echo(f"Still-live attached edges stranded: {payload['stranded_live_edge_count']}")


@click.command("supersede")
@click.argument("claim_id")
@click.argument("successor_claim_id")
@click.option("--reason", required=True, help="Required adjudication reason.")
@click.option("--evidence-ref", default=None, help="Optional evidence reference JSON object.")
@json_option
@handle_errors
def relationship_supersede_cmd(
    claim_id: str,
    successor_claim_id: str,
    reason: str,
    evidence_ref: str | None,
    output_json: bool,
) -> None:
    """Supersede CLAIM_ID with an existing live SUCCESSOR_CLAIM_ID."""
    evidence = _evidence_ref(evidence_ref)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.supersede_claim(
            instance_id,
            claim_id,
            successor_claim_id,
            reason,
            evidence_ref=evidence,
        ),
        lambda instance: service_supersede_claim(
            instance,
            claim_id,
            successor_claim_id,
            reason=reason,
            actor_context=_local_operator_context(),
            evidence_ref=(evidence.model_dump(mode="python") if evidence else None),
        ),
        command_name="relationship supersede",
    )
    _emit_lifecycle_result(result, output_json=output_json)


@click.command("retract")
@click.argument("claim_id")
@click.option("--reason", required=True, help="Required adjudication reason.")
@click.option("--evidence-ref", default=None, help="Optional evidence reference JSON object.")
@json_option
@handle_errors
def relationship_retract_cmd(
    claim_id: str,
    reason: str,
    evidence_ref: str | None,
    output_json: bool,
) -> None:
    """Retract CLAIM_ID without a successor."""
    evidence = _evidence_ref(evidence_ref)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.retract_claim(
            instance_id,
            claim_id,
            reason,
            evidence_ref=evidence,
        ),
        lambda instance: service_retract_claim(
            instance,
            claim_id,
            reason=reason,
            actor_context=_local_operator_context(),
            evidence_ref=(evidence.model_dump(mode="python") if evidence else None),
        ),
        command_name="relationship retract",
    )
    _emit_lifecycle_result(result, output_json=output_json)


@click.command("supersede")
@click.argument("entity_type")
@click.argument("entity_id")
@click.argument("successor_entity_type")
@click.argument("successor_entity_id")
@click.option("--reason", required=True, help="Required adjudication reason.")
@click.option("--evidence-ref", default=None, help="Optional evidence reference JSON object.")
@json_option
@handle_errors
def entity_supersede_cmd(
    entity_type: str,
    entity_id: str,
    successor_entity_type: str,
    successor_entity_id: str,
    reason: str,
    evidence_ref: str | None,
    output_json: bool,
) -> None:
    """Supersede an entity; attached edges do not migrate."""
    evidence = _evidence_ref(evidence_ref)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.supersede_entity(
            instance_id,
            entity_type,
            entity_id,
            successor_entity_type,
            successor_entity_id,
            reason,
            evidence_ref=evidence,
        ),
        lambda instance: service_supersede_entity(
            instance,
            entity_type,
            entity_id,
            successor_entity_type,
            successor_entity_id,
            reason=reason,
            actor_context=_local_operator_context(),
            evidence_ref=(evidence.model_dump(mode="python") if evidence else None),
        ),
        command_name="entity supersede",
    )
    _emit_lifecycle_result(result, output_json=output_json)


@click.command("retire")
@click.argument("entity_type")
@click.argument("entity_id")
@click.option("--reason", required=True, help="Required adjudication reason.")
@click.option("--evidence-ref", default=None, help="Optional evidence reference JSON object.")
@json_option
@handle_errors
def entity_retire_cmd(
    entity_type: str,
    entity_id: str,
    reason: str,
    evidence_ref: str | None,
    output_json: bool,
) -> None:
    """Retire an entity without cascading its attached edges."""
    evidence = _evidence_ref(evidence_ref)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.retire_entity(
            instance_id,
            entity_type,
            entity_id,
            reason,
            evidence_ref=evidence,
        ),
        lambda instance: service_retire_entity(
            instance,
            entity_type,
            entity_id,
            reason=reason,
            actor_context=_local_operator_context(),
            evidence_ref=(evidence.model_dump(mode="python") if evidence else None),
        ),
        command_name="entity retire",
    )
    _emit_lifecycle_result(result, output_json=output_json)


__all__ = [
    "entity_retire_cmd",
    "entity_supersede_cmd",
    "relationship_retract_cmd",
    "relationship_supersede_cmd",
]
