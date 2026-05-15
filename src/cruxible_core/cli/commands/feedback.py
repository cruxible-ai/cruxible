"""CLI commands for feedback, feedback-batch, outcome, and profile lookups."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import click
import yaml

from cruxible_client import contracts
from cruxible_core.cli.commands._common import (
    _dispatch_cli_instance,
    _get_client,
    _require_instance_id,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import handle_errors
from cruxible_core.service import (
    FeedbackItemInput,
    RelationshipTargetInput,
    service_feedback_batch_inputs,
    service_feedback_input,
    service_get_feedback_profile,
    service_get_outcome_profile,
    service_outcome,
)


@click.command("feedback")
@click.option("--receipt", "receipt_id", required=True, help="Receipt ID.")
@click.option(
    "--action",
    required=True,
    type=click.Choice(["approve", "reject", "correct", "flag"]),
    help="Feedback action.",
)
@click.option("--from-type", required=True, help="Source entity type.")
@click.option("--from-id", required=True, help="Source entity ID.")
@click.option("--relationship", required=True, help="Relationship type.")
@click.option("--to-type", required=True, help="Target entity type.")
@click.option("--to-id", required=True, help="Target entity ID.")
@click.option("--edge-key", default=None, type=int, help="Edge key (multi-edge disambiguation).")
@click.option("--reason", default="", help="Reason for feedback.")
@click.option(
    "--corrections",
    default=None,
    help="JSON object of edge property corrections (for action=correct).",
)
@click.option(
    "--source",
    type=click.Choice(["human", "agent"]),
    default="human",
    help="Who produced this feedback (default: human).",
)
@click.option(
    "--group-override",
    is_flag=True,
    default=False,
    help="Stamp edge with group_override property (edge must exist).",
)
@handle_errors
def feedback_cmd(
    receipt_id: str,
    action: str,
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    edge_key: int | None,
    reason: str,
    corrections: str | None,
    source: str,
    group_override: bool,
) -> None:
    """Submit feedback on a specific edge from a query result."""
    try:
        corrections_dict = json.loads(corrections) if corrections else None
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--corrections must be valid JSON") from exc
    if corrections_dict is not None and not isinstance(corrections_dict, dict):
        raise click.BadParameter("--corrections must be a JSON object")

    target = RelationshipTargetInput(
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship,
        to_type=to_type,
        to_id=to_id,
        edge_key=edge_key,
    )

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.feedback(
            instance_id,
            receipt_id=receipt_id,
            action=cast(contracts.FeedbackAction, action),
            source=cast(contracts.FeedbackSource, source),
            from_type=from_type,
            from_id=from_id,
            relationship=relationship,
            to_type=to_type,
            to_id=to_id,
            edge_key=edge_key,
            reason=reason,
            corrections=corrections_dict,
            group_override=group_override,
        ),
        lambda instance: service_feedback_input(
            instance,
            FeedbackItemInput(
                receipt_id=receipt_id,
                action=cast(contracts.FeedbackAction, action),
                target=target,
                reason=reason,
                corrections=corrections_dict,
                group_override=group_override,
            ),
            source=cast(contracts.FeedbackSource, source),
        ),
        allow_local=False,
        command_name="feedback",
    )

    if result.applied:
        click.echo(f"Feedback {result.feedback_id} applied to graph.")
    else:
        click.echo(f"Feedback {result.feedback_id} saved (edge not found in graph).")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@click.command("feedback-batch")
@click.option(
    "--items-file",
    type=click.Path(exists=True),
    default=None,
    help="JSON or YAML file with batch feedback items.",
)
@click.option("--items", "items_json", default=None, help="Inline JSON array of feedback items.")
@click.option(
    "--source",
    type=click.Choice(["human", "agent"]),
    default="human",
    help="Who produced this feedback batch (default: human).",
)
@handle_errors
def feedback_batch_cmd(
    items_file: str | None,
    items_json: str | None,
    source: str,
) -> None:
    """Submit a batch of edge feedback with one top-level receipt."""
    if items_file and items_json:
        raise click.BadParameter("Provide --items-file or --items, not both.")
    if not items_file and not items_json:
        raise click.BadParameter("Provide --items-file or --items.")

    try:
        if items_file:
            raw_items = yaml.safe_load(Path(items_file).read_text())
        else:
            raw_items = json.loads(items_json)  # type: ignore[arg-type]
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise click.BadParameter(f"Items must be valid JSON or YAML: {exc}") from exc

    if not isinstance(raw_items, list):
        raise click.BadParameter("Items must be a top-level array.")

    batch_items = [
        contracts.FeedbackBatchItemInput(
            receipt_id=item["receipt_id"],
            action=item["action"],
            target=contracts.EdgeTargetInput.model_validate(item["target"]),
            reason=item.get("reason", ""),
            corrections=item.get("corrections"),
            group_override=item.get("group_override", False),
        )
        for item in raw_items
    ]

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.feedback_batch(
            instance_id,
            items=batch_items,
            source=cast(contracts.FeedbackSource, source),
        ),
        lambda instance: service_feedback_batch_inputs(
            instance,
            [
                FeedbackItemInput(
                    receipt_id=item.receipt_id,
                    action=item.action,
                    target=RelationshipTargetInput(
                        from_type=item.target.from_type,
                        from_id=item.target.from_id,
                        relationship_type=item.target.relationship,
                        to_type=item.target.to_type,
                        to_id=item.target.to_id,
                        edge_key=item.target.edge_key,
                    ),
                    reason=item.reason,
                    corrections=item.corrections or {},
                    group_override=item.group_override,
                )
                for item in batch_items
            ],
            source=cast(contracts.FeedbackSource, source),
        ),
        allow_local=False,
        command_name="feedback-batch",
    )

    click.echo(f"Batch feedback recorded for {result.applied_count}/{result.total} item(s).")
    click.echo(f"  Feedback IDs: {', '.join(result.feedback_ids)}")
    if result.receipt_id:
        click.echo(f"  Receipt: {result.receipt_id}")


@click.command("outcome")
@click.option("--receipt", "receipt_id", required=True, help="Receipt ID.")
@click.option(
    "--outcome",
    "outcome_value",
    required=True,
    type=click.Choice(["correct", "incorrect", "partial", "unknown"]),
    help="Outcome of the decision.",
)
@click.option("--detail", default=None, help="JSON string with outcome details.")
@handle_errors
def outcome_cmd(receipt_id: str, outcome_value: str, detail: str | None) -> None:
    """Record the outcome of a decision."""
    try:
        detail_dict = json.loads(detail) if detail else None
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--detail must be valid JSON") from exc
    if detail_dict is not None and not isinstance(detail_dict, dict):
        raise click.BadParameter("--detail must be a JSON object")

    result = _dispatch_cli_instance(
        lambda client, instance_id: client.outcome(
            instance_id,
            receipt_id=receipt_id,
            outcome=cast(contracts.OutcomeValue, outcome_value),
            detail=detail_dict,
        ),
        lambda instance: service_outcome(
            instance,
            receipt_id=receipt_id,
            outcome=cast(contracts.OutcomeValue, outcome_value),
            detail=detail_dict,
        ),
        allow_local=False,
        command_name="outcome",
    )
    click.echo(f"Outcome {result.outcome_id} recorded.")


@click.command("feedback-profile")
@click.option("--relationship", "relationship_type", required=True, help="Relationship type.")
@handle_errors
def feedback_profile_cmd(relationship_type: str) -> None:
    """Display the configured feedback profile for one relationship type."""
    client = _get_client()
    if client is not None:
        result = client.get_feedback_profile(_require_instance_id(), relationship_type)
        if not result.found:
            click.echo("Not found.")
            return
        click.echo(yaml.safe_dump(result.profile, sort_keys=False))
        return

    instance = CruxibleInstance.load()
    profile = service_get_feedback_profile(instance, relationship_type)
    if profile is None:
        click.echo("Not found.")
        return
    click.echo(yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False))


@click.command("outcome-profile")
@click.option(
    "--anchor-type",
    required=True,
    type=click.Choice(["receipt", "resolution"]),
    help="Anchor type to resolve.",
)
@click.option("--relationship", "relationship_type", default=None, help="Relationship type.")
@click.option("--workflow", "workflow_name", default=None, help="Workflow name.")
@click.option(
    "--surface-type",
    default=None,
    type=click.Choice(["query", "workflow", "operation"]),
    help="Receipt surface type.",
)
@click.option("--surface-name", default=None, help="Receipt surface name.")
@handle_errors
def outcome_profile_cmd(
    anchor_type: str,
    relationship_type: str | None,
    workflow_name: str | None,
    surface_type: str | None,
    surface_name: str | None,
) -> None:
    """Display the configured outcome profile for one anchor context."""
    client = _get_client()
    if client is not None:
        result = client.get_outcome_profile(
            _require_instance_id(),
            anchor_type=cast(contracts.OutcomeAnchorType, anchor_type),
            relationship_type=relationship_type,
            workflow_name=workflow_name,
            surface_type=surface_type,
            surface_name=surface_name,
        )
        if not result.found:
            click.echo("Not found.")
            return
        click.echo(f"# profile_key: {result.profile_key}")
        click.echo(yaml.safe_dump(result.profile, sort_keys=False))
        return

    instance = CruxibleInstance.load()
    profile_key, profile = service_get_outcome_profile(
        instance,
        anchor_type=cast(contracts.OutcomeAnchorType, anchor_type),
        relationship_type=relationship_type,
        workflow_name=workflow_name,
        surface_type=surface_type,
        surface_name=surface_name,
    )
    if profile is None:
        click.echo("Not found.")
        return
    click.echo(f"# profile_key: {profile_key}")
    click.echo(yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False))
