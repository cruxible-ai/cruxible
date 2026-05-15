"""CLI commands for add-entity, add-relationship, add-constraint,
add-decision-policy, and reload-config."""

from __future__ import annotations

import json
from typing import cast

import click

from cruxible_client import contracts
from cruxible_core.cli.commands import _common
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _get_client,
    _read_validation_yaml_or_error,
    _require_instance_id,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    EntityWriteInput,
    RelationshipWriteInput,
    service_add_entity_inputs,
    service_add_relationship_inputs,
    service_reload_config,
)


@click.command("add-entity")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--id", "entity_id", required=True, help="Entity ID.")
@click.option("--props", default=None, help="JSON object of properties.")
@handle_errors
def add_entity_cmd(entity_type: str, entity_id: str, props: str | None) -> None:
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
        ),
        allow_local=False,
        command_name="add-entity",
    )

    label = f"{entity_type}:{entity_id}"
    if result.updated:
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
@handle_errors
def add_relationship_cmd(
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    props: str | None,
) -> None:
    """Add or update a relationship in the graph."""
    try:
        properties = json.loads(props) if props else {}
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--props must be valid JSON") from exc
    if not isinstance(properties, dict):
        raise click.BadParameter("--props must be a JSON object")

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.add_relationships(
            instance_id,
            [
                contracts.RelationshipInput(
                    from_type=from_type,
                    from_id=from_id,
                    relationship=relationship,
                    to_type=to_type,
                    to_id=to_id,
                    properties=properties,
                )
            ],
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
                )
            ],
            source="cli_add",
            source_ref="add-relationship",
        ),
        allow_local=False,
        command_name="add-relationship",
    )

    edge_label = f"{from_type}:{from_id} -[{relationship}]-> {to_type}:{to_id}"
    if result.updated:
        click.echo(f"Relationship updated: {edge_label}")
    else:
        click.echo(f"Relationship added: {edge_label}")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


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
                _read_validation_yaml_or_error(config_path)
                if config_path is not None
                else None
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
