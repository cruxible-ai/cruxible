"""CLI commands for add-entity, add-relationship, add-constraint,
add-decision-policy, and reload-config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import click
import yaml
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_core.cli.commands import _common
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _emit_json,
    _get_client,
    _read_validation_yaml_or_error,
    _require_instance_id,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.graph.provenance import (
    SOURCE_REF_ADD_RELATIONSHIP,
    SOURCE_REF_BATCH_DIRECT_WRITE,
)
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_batch_direct_write,
    service_reload_config,
)


def _parse_json_object(raw: str, *, option_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"{option_name} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise click.BadParameter(f"{option_name} must be a JSON object")
    return cast(dict[str, Any], value)


def _parse_evidence_ref(raw: str) -> contracts.EvidenceRef:
    try:
        return contracts.EvidenceRef.model_validate(
            _parse_json_object(raw, option_name="--evidence-ref")
        )
    except ValidationError as exc:
        raise click.BadParameter(f"--evidence-ref is invalid: {exc}") from exc


def _parse_source_evidence(raw: str) -> contracts.SourceEvidenceInput:
    try:
        return contracts.SourceEvidenceInput.model_validate(
            _parse_json_object(raw, option_name="--source-evidence")
        )
    except ValidationError as exc:
        raise click.BadParameter(f"--source-evidence is invalid: {exc}") from exc


def _load_batch_direct_write_payload(path: Path) -> contracts.BatchDirectWritePayload:
    try:
        raw = click.get_text_stream("stdin").read() if str(path) == "-" else path.read_text()
        payload = yaml.safe_load(raw)
    except OSError as exc:
        raise click.BadParameter(f"Could not read --payload-file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise click.BadParameter(f"--payload-file is not valid YAML/JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.BadParameter("--payload-file must contain a JSON/YAML object")
    try:
        return contracts.BatchDirectWritePayload.model_validate(payload)
    except ValidationError as exc:
        raise click.BadParameter(f"--payload-file is invalid: {exc}") from exc


def _batch_direct_write_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, contracts.BatchDirectWriteResult):
        return result.model_dump(mode="json")
    return {
        "dry_run": result.dry_run,
        "valid": result.valid,
        "entities_added": result.entities_added,
        "entities_updated": result.entities_updated,
        "relationships_added": result.relationships_added,
        "relationships_updated": result.relationships_updated,
        "validation_errors": list(result.validation_errors),
        "validation_warnings": list(result.validation_warnings),
        "evidence_sources_used": list(result.evidence_sources_used),
        "receipt_id": result.receipt_id,
    }


def _contract_batch_payload_to_service(
    payload: contracts.BatchDirectWritePayload,
) -> BatchDirectWriteInput:
    return BatchDirectWriteInput(
        entities=[
            EntityWriteInput(
                entity_type=entity.entity_type,
                entity_id=entity.entity_id,
                properties=entity.properties,
                metadata=entity.metadata,
            )
            for entity in payload.entities
        ],
        relationships=[
            BatchRelationshipWriteInput(
                from_type=edge.from_type,
                from_id=edge.from_id,
                relationship_type=edge.relationship_type,
                to_type=edge.to_type,
                to_id=edge.to_id,
                properties=edge.properties,
                evidence_refs=[ref.model_dump(mode="python") for ref in edge.evidence_refs],
                source_evidence=[ref.model_dump(mode="python") for ref in edge.source_evidence],
                evidence_rationale=edge.evidence_rationale,
                shared_evidence_keys=list(edge.shared_evidence_keys),
            )
            for edge in payload.relationships
        ],
        shared_evidence={
            key: SharedEvidenceInput(
                evidence_refs=[ref.model_dump(mode="python") for ref in evidence.evidence_refs],
                source_evidence=[ref.model_dump(mode="python") for ref in evidence.source_evidence],
            )
            for key, evidence in payload.shared_evidence.items()
        },
    )


@click.command("add-entity")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--id", "entity_id", required=True, help="Entity ID.")
@click.option("--props", default=None, help="JSON object of properties.")
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@handle_errors
def add_entity_cmd(entity_type: str, entity_id: str, props: str | None, dry_run: bool) -> None:
    """Add or update an entity in the graph."""
    try:
        properties = json.loads(props) if props else {}
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--props must be valid JSON") from exc
    if not isinstance(properties, dict):
        raise click.BadParameter("--props must be a JSON object")

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.add_entities(
            instance_id,
            [
                contracts.EntityInput(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    properties=properties,
                )
            ],
            dry_run=dry_run,
        ),
        lambda instance: service_add_entity_inputs(
            instance,
            [
                EntityWriteInput(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    properties=properties,
                )
            ],
            dry_run=dry_run,
        ),
        allow_local=False,
        command_name="entity add",
    )

    label = f"{entity_type}:{entity_id}"
    updated = (
        result.entities_updated > 0
        if isinstance(result, contracts.AddEntityResult)
        else result.updated
    )
    verb = "updated" if updated else "added"
    if dry_run:
        click.echo(f"Dry run: entity {label} would be {verb}.")
    elif updated:
        click.echo(f"Entity {label} updated.")
    else:
        click.echo(f"Entity {label} added.")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@click.command("add-relationship")
@click.option("--from-type", required=True, help="Source entity type.")
@click.option("--from-id", required=True, help="Source entity ID.")
@click.option("--relationship", required=True, help="Relationship type.")
@click.option("--to-type", required=True, help="Target entity type.")
@click.option("--to-id", required=True, help="Target entity ID.")
@click.option("--props", default=None, help="JSON object of edge properties.")
@click.option(
    "--evidence-ref",
    "evidence_refs",
    multiple=True,
    help="JSON evidence ref object. Repeat to attach multiple refs.",
)
@click.option(
    "--source-evidence",
    "source_evidence",
    multiple=True,
    help="JSON source-evidence locator. Repeat to attach multiple locators.",
)
@click.option(
    "--evidence-rationale",
    default=None,
    help="Optional rationale for the attached relationship evidence.",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@handle_errors
def add_relationship_cmd(
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    props: str | None,
    evidence_refs: tuple[str, ...],
    source_evidence: tuple[str, ...],
    evidence_rationale: str | None,
    dry_run: bool,
) -> None:
    """Add or update a relationship in the graph."""
    try:
        properties = json.loads(props) if props else {}
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--props must be valid JSON") from exc
    if not isinstance(properties, dict):
        raise click.BadParameter("--props must be a JSON object")
    parsed_evidence_refs = [_parse_evidence_ref(raw) for raw in evidence_refs]
    parsed_source_evidence = [_parse_source_evidence(raw) for raw in source_evidence]
    local_evidence_refs = [item.model_dump(mode="python") for item in parsed_evidence_refs]
    local_source_evidence = [item.model_dump(mode="python") for item in parsed_source_evidence]

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.add_relationships(
            instance_id,
            [
                contracts.RelationshipInput(
                    from_type=from_type,
                    from_id=from_id,
                    relationship_type=relationship,
                    to_type=to_type,
                    to_id=to_id,
                    properties=properties,
                    evidence_refs=parsed_evidence_refs,
                    source_evidence=parsed_source_evidence,
                    evidence_rationale=evidence_rationale,
                )
            ],
            dry_run=dry_run,
        ),
        lambda instance: service_add_relationship_inputs(
            instance,
            [
                RelationshipWriteInput(
                    from_type=from_type,
                    from_id=from_id,
                    relationship_type=relationship,
                    to_type=to_type,
                    to_id=to_id,
                    properties=properties,
                    evidence_refs=local_evidence_refs,
                    source_evidence=local_source_evidence,
                    evidence_rationale=evidence_rationale,
                )
            ],
            source="cli_add",
            source_ref=SOURCE_REF_ADD_RELATIONSHIP,
            dry_run=dry_run,
        ),
        allow_local=False,
        command_name="relationship add",
    )

    edge_label = f"{from_type}:{from_id} -[{relationship}]-> {to_type}:{to_id}"
    verb = "updated" if result.updated else "added"
    if dry_run:
        click.echo(f"Dry run: relationship would be {verb}: {edge_label}")
    elif result.updated:
        click.echo(f"Relationship updated: {edge_label}")
    else:
        click.echo(f"Relationship added: {edge_label}")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@click.command("batch-direct-write")
@click.option(
    "--payload-file",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help=(
        "JSON or YAML payload containing entities, relationships, and shared_evidence. "
        "Use '-' to read stdin."
    ),
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def batch_direct_write_cmd(
    payload_file: Path,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Validate or apply a direct batch graph write payload."""
    payload = _load_batch_direct_write_payload(payload_file)
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.batch_direct_write(
            instance_id,
            payload,
            dry_run=dry_run,
        ),
        lambda instance: service_batch_direct_write(
            instance,
            _contract_batch_payload_to_service(payload),
            dry_run=dry_run,
            source="cli_batch_direct_write",
            source_ref=SOURCE_REF_BATCH_DIRECT_WRITE,
        ),
        allow_local=False,
        command_name="batch-direct-write",
    )
    result_payload = _batch_direct_write_result_payload(result)
    if output_json:
        _emit_json(result_payload)
        return
    action = "validated" if dry_run else "applied"
    if result_payload["valid"]:
        click.echo(f"Batch direct write {action}.")
    else:
        click.echo(f"Batch direct write {action} with validation errors.")
    click.echo(
        "  Entities: "
        f"{result_payload['entities_added']} added, "
        f"{result_payload['entities_updated']} updated"
    )
    click.echo(
        "  Relationships: "
        f"{result_payload['relationships_added']} added, "
        f"{result_payload['relationships_updated']} updated"
    )
    if result_payload["evidence_sources_used"]:
        click.echo("  Evidence sources: " + ", ".join(result_payload["evidence_sources_used"]))
    for warning in result_payload["validation_warnings"]:
        click.secho(f"  Warning: {warning}", fg="yellow")
    for error in result_payload["validation_errors"]:
        click.secho(f"  Error: {error}", fg="red")
    if result_payload["receipt_id"]:
        click.echo(f"  Receipt: {result_payload['receipt_id']}")


@click.command("add-constraint")
@click.option("--name", required=True, help="Constraint name.")
@click.option("--rule", required=True, help="Constraint rule expression.")
@click.option(
    "--severity",
    type=click.Choice(["warning", "error"]),
    default="warning",
    help="Severity level (default: warning).",
)
@click.option("--description", default=None, help="Optional description.")
@handle_errors
def add_constraint_cmd(
    name: str,
    rule: str,
    severity: str,
    description: str | None,
) -> None:
    """Add a constraint rule to the config."""
    client = _common._get_client()
    if client is not None:
        result = client.add_constraint(
            _require_instance_id(),
            name=name,
            rule=rule,
            severity=cast(contracts.ConstraintSeverity, severity),
            description=description,
        )
        click.echo(f"Constraint '{result.name}' added to config.")
        for warning in result.warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")
        return
    raise click.UsageError("Local mutation disabled for add-constraint; use server mode.")


@click.command("reload-config")
@click.option("--config", "config_path", default=None, help="Optional new config path.")
@handle_errors
def reload_config_cmd(config_path: str | None) -> None:
    """Validate the active config or repoint the instance to a new config file."""
    remote = _common._get_client() is not None
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.reload_config(
            instance_id,
            config_yaml=(
                _read_validation_yaml_or_error(config_path) if config_path is not None else None
            ),
        ),
        lambda instance: service_reload_config(instance, config_path=config_path),
        allow_local=False,
        command_name="reload-config",
    )
    status = "updated" if result.updated else "validated"
    if remote:
        click.echo(f"Config {status} on server.")
    else:
        click.echo(f"Config {status}: {result.config_path}")
    for warning in result.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")


@click.command("add-decision-policy")
@click.option("--name", required=True, help="Decision policy name.")
@click.option(
    "--applies-to",
    required=True,
    type=click.Choice(["query", "workflow"]),
    help="Policy application surface.",
)
@click.option("--relationship", "relationship_type", required=True, help="Relationship type.")
@click.option(
    "--effect",
    required=True,
    type=click.Choice(["suppress", "require_review"]),
    help="Policy effect.",
)
@click.option("--query-name", default=None, help="Named query for query policies.")
@click.option("--workflow-name", default=None, help="Workflow name for workflow policies.")
@click.option("--match", default="{}", help="JSON object for exact-match selectors.")
@click.option("--description", default=None, help="Optional description.")
@click.option("--rationale", default="", help="Policy rationale.")
@click.option("--expires-at", default=None, help="Optional ISO timestamp/date.")
@handle_errors
def add_decision_policy_cmd(
    name: str,
    applies_to: str,
    relationship_type: str,
    effect: str,
    query_name: str | None,
    workflow_name: str | None,
    match: str,
    description: str | None,
    rationale: str,
    expires_at: str | None,
) -> None:
    """Add a decision policy to the config."""
    try:
        match_dict = json.loads(match)
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--match must be valid JSON") from exc
    if not isinstance(match_dict, dict):
        raise click.BadParameter("--match must be a JSON object")

    client = _get_client()
    if client is not None:
        result = client.add_decision_policy(
            _require_instance_id(),
            name=name,
            applies_to=cast(contracts.DecisionPolicyAppliesTo, applies_to),
            relationship_type=relationship_type,
            effect=cast(contracts.DecisionPolicyEffect, effect),
            match=contracts.DecisionPolicyMatchInput.model_validate(match_dict),
            description=description,
            rationale=rationale,
            query_name=query_name,
            workflow_name=workflow_name,
            expires_at=expires_at,
        )
        click.echo(f"Decision policy '{result.name}' added to config.")
        for warning in result.warnings:
            click.secho(f"  Warning: {warning}", fg="yellow")
        return
    raise click.UsageError("Local mutation disabled for add-decision-policy; use server mode.")
