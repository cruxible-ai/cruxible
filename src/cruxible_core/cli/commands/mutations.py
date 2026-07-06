"""CLI commands for direct mutations and config edits (config reload/add-*)."""

from __future__ import annotations

import json
from collections.abc import Mapping
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
    _require_instance_id,
    json_option,
)
from cruxible_core.cli.main import handle_errors
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.provenance import (
    SOURCE_REF_BATCH_DIRECT_WRITE,
)
from cruxible_core.service import (
    BatchDirectWriteInput,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    SharedEvidenceInput,
    service_adopt_config,
    service_batch_direct_write,
    service_config_status,
    service_refresh_config,
    service_reload_config,
)
from cruxible_core.service.lifecycle_inputs import (
    entity_metadata_with_lifecycle,
    relationship_lifecycle_state,
)


def _field_assignment(raw: str, *, option_name: str) -> tuple[str, str]:
    if "=" not in raw:
        raise click.BadParameter(f"{option_name} must use FIELD=VALUE")
    field, value = raw.split("=", 1)
    normalized_field = field.strip()
    if not normalized_field:
        raise click.BadParameter(f"{option_name} field name must not be blank")
    return normalized_field, value


def _parse_property_assignments(
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
    *,
    initial: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = dict(initial or {})
    for raw in set_values:
        field, value = _field_assignment(raw, option_name="--set")
        if field in properties:
            raise click.BadParameter(f"duplicate property assignment for '{field}'")
        properties[field] = value
    for raw in set_json_values:
        field, value = _field_assignment(raw, option_name="--set-json")
        if field in properties:
            raise click.BadParameter(f"duplicate property assignment for '{field}'")
        try:
            properties[field] = json.loads(value)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--set-json value for '{field}' must be valid JSON") from exc
    return properties


def _parse_props_option(props: str | None) -> dict[str, Any]:
    try:
        properties = json.loads(props) if props else {}
    except json.JSONDecodeError as exc:
        raise click.BadParameter("--props must be valid JSON") from exc
    if not isinstance(properties, dict):
        raise click.BadParameter("--props must be a JSON object")
    return cast(dict[str, Any], properties)


def _parse_property_inputs(
    props: str | None,
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
) -> dict[str, Any]:
    return _parse_property_assignments(
        set_values,
        set_json_values,
        initial=_parse_props_option(props),
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


def _direct_write_group_interaction_payload(interaction: Any) -> dict[str, Any]:
    if isinstance(interaction, contracts.DirectWriteGroupInteraction):
        return interaction.model_dump(mode="json")
    return {
        "relationship_type": interaction.relationship_type,
        "from_type": interaction.from_type,
        "from_id": interaction.from_id,
        "to_type": interaction.to_type,
        "to_id": interaction.to_id,
        "group_id": interaction.group_id,
        "group_status": interaction.group_status,
        "group_signature": interaction.group_signature,
        "source_workflow_name": interaction.source_workflow_name,
        "edge_key": interaction.edge_key,
    }


def _emit_direct_write_group_notices(result_payload: dict[str, Any], *, prefix: str = "") -> None:
    pending_count = len(result_payload.get("pending_conflicts") or [])
    updated_count = len(result_payload.get("updated_group_backed_edges") or [])
    if pending_count:
        click.secho(
            f"{prefix}Notice: {pending_count} pending group conflict(s) detected.",
            fg="yellow",
        )
    if updated_count:
        click.secho(
            f"{prefix}Notice: {updated_count} group-backed edge update(s) detected.",
            fg="yellow",
        )


def _require_server_client(command_name: str) -> tuple[Any, str]:
    client = _common._get_client()
    if client is None:
        raise click.UsageError(f"Local mutation disabled for {command_name}; use server mode.")
    return client, _require_instance_id()


def _resolve_arg_or_option(
    *,
    arg_value: str | None,
    option_value: str | None,
    option_name: str,
    label: str,
) -> str:
    if arg_value and option_value and arg_value != option_value:
        raise click.UsageError(
            f"{label} supplied both positionally and as {option_name} with different values"
        )
    value = arg_value or option_value
    if not value:
        raise click.UsageError(f"Missing {label}")
    return value


def _resolve_entity_identity(
    entity_type: str | None,
    entity_id: str | None,
    *,
    entity_type_option: str | None,
    entity_id_option: str | None,
) -> tuple[str, str]:
    return (
        _resolve_arg_or_option(
            arg_value=entity_type,
            option_value=entity_type_option,
            option_name="--type",
            label="entity type",
        ),
        _resolve_arg_or_option(
            arg_value=entity_id,
            option_value=entity_id_option,
            option_name="--id",
            label="entity id",
        ),
    )


def _resolve_relationship_identity(
    relationship_type: str | None,
    from_type: str | None,
    from_id: str | None,
    to_type: str | None,
    to_id: str | None,
    *,
    relationship_option: str | None,
    from_type_option: str | None,
    from_id_option: str | None,
    to_type_option: str | None,
    to_id_option: str | None,
) -> tuple[str, str, str, str, str]:
    return (
        _resolve_arg_or_option(
            arg_value=relationship_type,
            option_value=relationship_option,
            option_name="--relationship",
            label="relationship type",
        ),
        _resolve_arg_or_option(
            arg_value=from_type,
            option_value=from_type_option,
            option_name="--from-type",
            label="source entity type",
        ),
        _resolve_arg_or_option(
            arg_value=from_id,
            option_value=from_id_option,
            option_name="--from-id",
            label="source entity id",
        ),
        _resolve_arg_or_option(
            arg_value=to_type,
            option_value=to_type_option,
            option_name="--to-type",
            label="target entity type",
        ),
        _resolve_arg_or_option(
            arg_value=to_id,
            option_value=to_id_option,
            option_name="--to-id",
            label="target entity id",
        ),
    )


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
        "pending_conflicts": [
            _direct_write_group_interaction_payload(item) for item in result.pending_conflicts
        ],
        "updated_group_backed_edges": [
            _direct_write_group_interaction_payload(item)
            for item in result.updated_group_backed_edges
        ],
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
                metadata=entity_metadata_with_lifecycle(entity.metadata, entity.lifecycle),
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
                pending=edge.pending,
                evidence_refs=[ref.model_dump(mode="python") for ref in edge.evidence_refs],
                source_evidence=[ref.model_dump(mode="python") for ref in edge.source_evidence],
                evidence_rationale=edge.evidence_rationale,
                shared_evidence_keys=list(edge.shared_evidence_keys),
                lifecycle=relationship_lifecycle_state(edge.lifecycle),
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


def _emit_batch_write_result(
    result: Any,
    *,
    action_label: str,
    dry_run: bool,
    output_json: bool,
) -> None:
    result_payload = _batch_direct_write_result_payload(result)
    if output_json:
        _emit_json(result_payload)
        return
    action = "validated" if dry_run else "applied"
    if result_payload["valid"]:
        click.echo(f"{action_label} {action}.")
    else:
        click.echo(f"{action_label} {action} with validation errors.")
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
    _emit_direct_write_group_notices(result_payload, prefix="  ")


def _run_batch_payload(
    payload: contracts.BatchDirectWritePayload,
    *,
    dry_run: bool,
    command_name: str,
) -> contracts.BatchDirectWriteResult:
    client, instance_id = _require_server_client(command_name)
    return cast(
        contracts.BatchDirectWriteResult,
        client.batch_direct_write(instance_id, payload, dry_run=dry_run),
    )


def _entity_exists(
    entity_type: str,
    entity_id: str,
    *,
    command_name: str,
) -> bool:
    client, instance_id = _require_server_client(command_name)
    result = client.get_entity(instance_id, entity_type, entity_id)
    return bool(result.found)


def _relationship_exists(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    *,
    command_name: str,
) -> bool:
    client, instance_id = _require_server_client(command_name)
    result = client.get_relationship(
        instance_id,
        from_type=from_type,
        from_id=from_id,
        relationship_type=relationship_type,
        to_type=to_type,
        to_id=to_id,
    )
    return bool(result.found)


def _entity_payload(
    entity_type: str,
    entity_id: str,
    properties: Mapping[str, Any],
    lifecycle: contracts.EntityLifecycleInput | None = None,
) -> contracts.BatchDirectWritePayload:
    return contracts.BatchDirectWritePayload(
        entities=[
            contracts.EntityInput(
                entity_type=entity_type,
                entity_id=entity_id,
                properties=dict(properties),
                lifecycle=lifecycle,
            )
        ],
        relationships=[],
        shared_evidence={},
    )


def _build_entity_lifecycle_input(
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
) -> contracts.EntityLifecycleInput | None:
    """Build a typed entity lifecycle input from the CLI lifecycle options."""
    if lifecycle_status is None:
        if lifecycle_reason is not None:
            raise click.UsageError("--lifecycle-reason requires --lifecycle-status")
        return None
    try:
        return contracts.EntityLifecycleInput.model_validate(
            {"status": lifecycle_status, "reason": lifecycle_reason}
        )
    except ValidationError as exc:
        raise click.BadParameter(f"--lifecycle-status is invalid: {exc}") from exc


def _build_relationship_lifecycle_input(
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
) -> contracts.RelationshipLifecycleInput | None:
    """Build a typed relationship lifecycle input from the CLI lifecycle options."""
    if lifecycle_status is None:
        if lifecycle_reason is not None:
            raise click.UsageError("--lifecycle-reason requires --lifecycle-status")
        return None
    try:
        return contracts.RelationshipLifecycleInput.model_validate(
            {"status": lifecycle_status, "reason": lifecycle_reason}
        )
    except ValidationError as exc:
        raise click.BadParameter(f"--lifecycle-status is invalid: {exc}") from exc


_ENTITY_LIFECYCLE_STATUSES = ("live", "superseded", "retired")
_RELATIONSHIP_LIFECYCLE_STATUSES = ("active", "inactive", "superseded", "retracted")


def _relationship_payload(
    relationship_type: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    properties: Mapping[str, Any],
    *,
    evidence_refs: tuple[str, ...],
    source_evidence: tuple[str, ...],
    evidence_rationale: str | None,
    pending: bool = False,
    lifecycle: contracts.RelationshipLifecycleInput | None = None,
) -> contracts.BatchDirectWritePayload:
    return contracts.BatchDirectWritePayload(
        entities=[],
        relationships=[
            contracts.BatchRelationshipInput(
                relationship_type=relationship_type,
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                properties=dict(properties),
                pending=pending,
                evidence_refs=[_parse_evidence_ref(raw) for raw in evidence_refs],
                source_evidence=[_parse_source_evidence(raw) for raw in source_evidence],
                evidence_rationale=evidence_rationale,
                lifecycle=lifecycle,
            )
        ],
        shared_evidence={},
    )


def _validate_relationship_evidence(
    evidence_refs: tuple[str, ...],
    source_evidence: tuple[str, ...],
) -> None:
    for raw in evidence_refs:
        _parse_evidence_ref(raw)
    for raw in source_evidence:
        _parse_source_evidence(raw)


def _require_property_assignments(properties: Mapping[str, Any], *, command_name: str) -> None:
    if not properties:
        raise click.UsageError(f"{command_name} requires at least one --set or --set-json")


@click.command("add")
@click.argument("entity_type", required=False)
@click.argument("entity_id", required=False)
@click.option("--type", "entity_type_option", default=None, help="Entity type.")
@click.option("--id", "entity_id_option", default=None, help="Entity ID.")
@click.option("--props", default=None, help="JSON object of properties.")
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="String property assignment FIELD=VALUE. Repeat for multiple properties.",
)
@click.option(
    "--set-json",
    "set_json_values",
    multiple=True,
    help="Typed JSON property assignment FIELD=JSON. Repeat for multiple properties.",
)
@click.option(
    "--lifecycle-status",
    type=click.Choice(_ENTITY_LIFECYCLE_STATUSES),
    default=None,
    help="Typed entity lifecycle status (live, superseded, retired).",
)
@click.option(
    "--lifecycle-reason",
    default=None,
    help="Optional reason for the lifecycle status (requires --lifecycle-status).",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def add_entity_cmd(
    entity_type: str | None,
    entity_id: str | None,
    entity_type_option: str | None,
    entity_id_option: str | None,
    props: str | None,
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Create one entity using JSON properties or FIELD=VALUE assignments."""
    entity_type, entity_id = _resolve_entity_identity(
        entity_type,
        entity_id,
        entity_type_option=entity_type_option,
        entity_id_option=entity_id_option,
    )
    properties = _parse_property_inputs(props, set_values, set_json_values)
    lifecycle = _build_entity_lifecycle_input(lifecycle_status, lifecycle_reason)
    if _entity_exists(entity_type, entity_id, command_name="entity add"):
        raise DataValidationError(f"Entity {entity_type}:{entity_id} already exists")
    result = _run_batch_payload(
        _entity_payload(entity_type, entity_id, properties, lifecycle),
        dry_run=dry_run,
        command_name="entity add",
    )
    _emit_batch_write_result(
        result,
        action_label=f"Add entity {entity_type}:{entity_id}",
        dry_run=dry_run,
        output_json=output_json,
    )


@click.command("update")
@click.argument("entity_type", required=False)
@click.argument("entity_id", required=False)
@click.option("--type", "entity_type_option", default=None, help="Entity type.")
@click.option("--id", "entity_id_option", default=None, help="Entity ID.")
@click.option("--props", default=None, help="JSON object of properties.")
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="String property assignment FIELD=VALUE. Repeat for multiple properties.",
)
@click.option(
    "--set-json",
    "set_json_values",
    multiple=True,
    help="Typed JSON property assignment FIELD=JSON. Repeat for multiple properties.",
)
@click.option(
    "--lifecycle-status",
    type=click.Choice(_ENTITY_LIFECYCLE_STATUSES),
    default=None,
    help="Typed entity lifecycle status (live, superseded, retired).",
)
@click.option(
    "--lifecycle-reason",
    default=None,
    help="Optional reason for the lifecycle status (requires --lifecycle-status).",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def update_entity_cmd(
    entity_type: str | None,
    entity_id: str | None,
    entity_type_option: str | None,
    entity_id_option: str | None,
    props: str | None,
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Update one existing entity's properties and/or lifecycle state.

    Set ``--lifecycle-status retired`` (or ``superseded``) to move an
    entity onto the entity-lifecycle axis (the canonical soft-delete); it is then
    gated out of live reads while remaining fetchable by id. The typed lifecycle
    write replaces the entity's lifecycle state and preserves other metadata.
    """
    entity_type, entity_id = _resolve_entity_identity(
        entity_type,
        entity_id,
        entity_type_option=entity_type_option,
        entity_id_option=entity_id_option,
    )
    properties = _parse_property_inputs(props, set_values, set_json_values)
    lifecycle = _build_entity_lifecycle_input(lifecycle_status, lifecycle_reason)
    if not properties and lifecycle is None:
        raise click.UsageError(
            "update entity requires at least one --set, --set-json, --props, or --lifecycle-status"
        )
    if not _entity_exists(entity_type, entity_id, command_name="entity update"):
        raise DataValidationError(f"Entity {entity_type}:{entity_id} not found")
    result = _run_batch_payload(
        _entity_payload(entity_type, entity_id, properties, lifecycle),
        dry_run=dry_run,
        command_name="entity update",
    )
    _emit_batch_write_result(
        result,
        action_label=f"Update entity {entity_type}:{entity_id}",
        dry_run=dry_run,
        output_json=output_json,
    )


@click.command("add")
@click.argument("relationship_type", required=False)
@click.argument("from_type", required=False)
@click.argument("from_id", required=False)
@click.argument("to_type", required=False)
@click.argument("to_id", required=False)
@click.option("--from-type", "from_type_option", default=None, help="Source entity type.")
@click.option("--from-id", "from_id_option", default=None, help="Source entity ID.")
@click.option("--relationship", "relationship_option", default=None, help="Relationship type.")
@click.option("--to-type", "to_type_option", default=None, help="Target entity type.")
@click.option("--to-id", "to_id_option", default=None, help="Target entity ID.")
@click.option("--props", default=None, help="JSON object of edge properties.")
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="String relationship property assignment FIELD=VALUE.",
)
@click.option(
    "--set-json",
    "set_json_values",
    multiple=True,
    help="Typed JSON relationship property assignment FIELD=JSON.",
)
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
@click.option(
    "--pending",
    is_flag=True,
    help="Create the relationship as pending review instead of live state.",
)
@click.option(
    "--lifecycle-status",
    type=click.Choice(_RELATIONSHIP_LIFECYCLE_STATUSES),
    default=None,
    help=(
        "Typed edge lifecycle status (active, inactive, superseded, retracted). "
        "Sets only assertion.lifecycle; cannot approve/reject the edge."
    ),
)
@click.option(
    "--lifecycle-reason",
    default=None,
    help="Optional reason for the lifecycle status (requires --lifecycle-status).",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def add_relationship_cmd(
    relationship_type: str | None,
    from_type: str | None,
    from_id: str | None,
    to_type: str | None,
    to_id: str | None,
    relationship_option: str | None,
    from_type_option: str | None,
    from_id_option: str | None,
    to_type_option: str | None,
    to_id_option: str | None,
    props: str | None,
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    source_evidence: tuple[str, ...],
    evidence_rationale: str | None,
    pending: bool,
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Add one relationship using FIELD=VALUE property assignments."""
    relationship_type, from_type, from_id, to_type, to_id = _resolve_relationship_identity(
        relationship_type,
        from_type,
        from_id,
        to_type,
        to_id,
        relationship_option=relationship_option,
        from_type_option=from_type_option,
        from_id_option=from_id_option,
        to_type_option=to_type_option,
        to_id_option=to_id_option,
    )
    properties = _parse_property_inputs(props, set_values, set_json_values)
    _validate_relationship_evidence(evidence_refs, source_evidence)
    lifecycle = _build_relationship_lifecycle_input(lifecycle_status, lifecycle_reason)
    if _relationship_exists(
        relationship_type,
        from_type,
        from_id,
        to_type,
        to_id,
        command_name="relationship add",
    ):
        raise DataValidationError(
            f"Relationship already exists: "
            f"{from_type}:{from_id} -[{relationship_type}]-> {to_type}:{to_id}"
        )
    result = _run_batch_payload(
        _relationship_payload(
            relationship_type,
            from_type,
            from_id,
            to_type,
            to_id,
            properties,
            evidence_refs=evidence_refs,
            source_evidence=source_evidence,
            evidence_rationale=evidence_rationale,
            pending=pending,
            lifecycle=lifecycle,
        ),
        dry_run=dry_run,
        command_name="relationship add",
    )
    _emit_batch_write_result(
        result,
        action_label=(
            f"Add relationship {from_type}:{from_id} -[{relationship_type}]-> {to_type}:{to_id}"
        ),
        dry_run=dry_run,
        output_json=output_json,
    )


@click.command("update")
@click.argument("relationship_type", required=False)
@click.argument("from_type", required=False)
@click.argument("from_id", required=False)
@click.argument("to_type", required=False)
@click.argument("to_id", required=False)
@click.option("--from-type", "from_type_option", default=None, help="Source entity type.")
@click.option("--from-id", "from_id_option", default=None, help="Source entity ID.")
@click.option("--relationship", "relationship_option", default=None, help="Relationship type.")
@click.option("--to-type", "to_type_option", default=None, help="Target entity type.")
@click.option("--to-id", "to_id_option", default=None, help="Target entity ID.")
@click.option("--props", default=None, help="JSON object of edge properties.")
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="String relationship property assignment FIELD=VALUE.",
)
@click.option(
    "--set-json",
    "set_json_values",
    multiple=True,
    help="Typed JSON relationship property assignment FIELD=JSON.",
)
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
@click.option(
    "--lifecycle-status",
    type=click.Choice(_RELATIONSHIP_LIFECYCLE_STATUSES),
    default=None,
    help=(
        "Typed edge lifecycle status (active, inactive, superseded, retracted) "
        "-- e.g. retract a live edge. Sets only assertion.lifecycle; cannot "
        "approve/reject the edge."
    ),
)
@click.option(
    "--lifecycle-reason",
    default=None,
    help="Optional reason for the lifecycle status (requires --lifecycle-status).",
)
@click.option("--dry-run", is_flag=True, help="Validate without mutating graph state.")
@json_option
@handle_errors
def update_relationship_cmd(
    relationship_type: str | None,
    from_type: str | None,
    from_id: str | None,
    to_type: str | None,
    to_id: str | None,
    relationship_option: str | None,
    from_type_option: str | None,
    from_id_option: str | None,
    to_type_option: str | None,
    to_id_option: str | None,
    props: str | None,
    set_values: tuple[str, ...],
    set_json_values: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    source_evidence: tuple[str, ...],
    evidence_rationale: str | None,
    lifecycle_status: str | None,
    lifecycle_reason: str | None,
    dry_run: bool,
    output_json: bool,
) -> None:
    """Update one existing relationship's properties, evidence, or lifecycle.

    Set ``--lifecycle-status retracted`` (or ``superseded``/``inactive``) to move
    the edge off live graph semantics. This typed lifecycle write sets ONLY the
    edge's lifecycle state -- it can never approve or reject the edge (that stays
    exclusive to the governed feedback / group-resolve paths).
    """
    relationship_type, from_type, from_id, to_type, to_id = _resolve_relationship_identity(
        relationship_type,
        from_type,
        from_id,
        to_type,
        to_id,
        relationship_option=relationship_option,
        from_type_option=from_type_option,
        from_id_option=from_id_option,
        to_type_option=to_type_option,
        to_id_option=to_id_option,
    )
    properties = _parse_property_inputs(props, set_values, set_json_values)
    _validate_relationship_evidence(evidence_refs, source_evidence)
    lifecycle = _build_relationship_lifecycle_input(lifecycle_status, lifecycle_reason)
    if not (properties or evidence_refs or source_evidence or evidence_rationale or lifecycle):
        raise click.UsageError(
            "update relationship requires at least one --set, --set-json, "
            "--evidence-ref, --source-evidence, --evidence-rationale, or --lifecycle-status"
        )
    if not _relationship_exists(
        relationship_type,
        from_type,
        from_id,
        to_type,
        to_id,
        command_name="relationship update",
    ):
        raise DataValidationError(
            f"Relationship not found: "
            f"{from_type}:{from_id} -[{relationship_type}]-> {to_type}:{to_id}"
        )
    result = _run_batch_payload(
        _relationship_payload(
            relationship_type,
            from_type,
            from_id,
            to_type,
            to_id,
            properties,
            evidence_refs=evidence_refs,
            source_evidence=source_evidence,
            evidence_rationale=evidence_rationale,
            lifecycle=lifecycle,
        ),
        dry_run=dry_run,
        command_name="relationship update",
    )
    _emit_batch_write_result(
        result,
        action_label=(
            f"Update relationship {from_type}:{from_id} -[{relationship_type}]-> {to_type}:{to_id}"
        ),
        dry_run=dry_run,
        output_json=output_json,
    )


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
    _emit_direct_write_group_notices(result_payload, prefix="  ")


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
    raise click.UsageError("Local mutation disabled for config add-constraint; use server mode.")


@click.command("reload")
@click.option("--config", "config_path", default=None, help="Retired; refused with guidance.")
@handle_errors
def reload_config_cmd(config_path: str | None) -> None:
    """Validate the active config (validate-only; replace/repoint are retired)."""
    if config_path is not None:
        raise click.UsageError(
            "`config reload --config` is retired: reload is validate-only and "
            "never repoints an instance config. Deliver source-layer updates "
            "with `cruxible config refresh`, or migrate a materialized "
            "instance once with `cruxible config adopt --kit <ref>`."
        )
    remote = _common._get_client() is not None
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.reload_config(instance_id),
        lambda instance: service_reload_config(instance),
        allow_local=False,
        command_name="config reload",
    )
    status = "updated" if result.updated else "validated"
    if remote:
        click.echo(f"Config {status} on server.")
    else:
        click.echo(f"Config {status}: {result.config_path}")
    for warning in result.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")


_REFRESH_CLASSIFICATION_COLORS = {"weakened": "red", "tightened": "green"}


@click.command("refresh")
@handle_errors
def refresh_config_cmd() -> None:
    """Recompose the config from the instance source pointer and swap it in.

    Refresh takes no config path: it re-resolves the layers declared in the
    instance's config-source.yaml, classifies the governance diff (weakening
    refreshes require admin), rebuilds the workflow lock, and receipts the
    swap. Any failing step leaves the previous config serving.
    """
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.config_refresh(instance_id),
        lambda instance: service_refresh_config(instance),
        allow_local=False,
        command_name="config refresh",
    )
    color = _REFRESH_CLASSIFICATION_COLORS.get(result.classification)
    click.secho(f"Config refreshed: {result.classification}", fg=color, bold=True)
    click.echo(f"  before: {result.before_composed_digest}")
    click.echo(f"  after:  {result.after_composed_digest}")
    for layer in _layer_dicts(result.layers):
        click.echo(f"  layer [{layer['kind']}] {layer['ref']} ({layer['digest']})")
    _echo_governance_diff(result.governance_changes)
    if result.receipt_id:
        click.echo(f"Receipt: {result.receipt_id}")
    for warning in result.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")


def _layer_dicts(layers: list[Any]) -> list[dict[str, str]]:
    """Normalize service dataclass / client model layer records to dicts."""
    return [
        layer if isinstance(layer, dict) else layer.model_dump(mode="python") for layer in layers
    ]


def _echo_governance_diff(lines: list[str]) -> None:
    if lines:
        click.secho("Governance diff:", bold=True)
        for line in lines:
            click.secho(f"  {line}", fg="red" if "[weakening]" in line else None)
    else:
        click.echo("Governance diff: no governance-relevant changes.")


@click.command("status")
@json_option
@handle_errors
def status_config_cmd(output_json: bool) -> None:
    """Show serving/receipted/source config digests and classify any drift.

    Read-only: reports the serving composed digest, the source pointer layers
    with their current digests, and whether recomposing the source NOW yields
    a digest different from the last receipted init/refresh/adopt (drift),
    with the governance classification of that drift. Pre-pointer instances
    report their materialized status with no drift computation.
    """
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.config_status(instance_id),
        lambda instance: service_config_status(instance),
        command_name="config status",
    )
    layers = _layer_dicts(result.layers)
    if output_json:
        _emit_json(
            {
                "source": result.source,
                "serving_composed_digest": result.serving_composed_digest,
                "receipted_composed_digest": result.receipted_composed_digest,
                "pointer_digest": result.pointer_digest,
                "layers": layers,
                "recomposed_digest": result.recomposed_digest,
                "drift": result.drift,
                "drift_classification": result.drift_classification,
                "drift_changes": result.drift_changes,
                "serving_matches_receipt": result.serving_matches_receipt,
            }
        )
        return
    click.echo(f"Config source: {result.source}")
    click.echo(f"  serving digest:   {result.serving_composed_digest}")
    click.echo(f"  receipted digest: {result.receipted_composed_digest or '(none)'}")
    if result.pointer_digest is None:
        click.echo(
            "Drift is computed for source-pointer instances only; migrate with "
            "`cruxible config adopt --kit <ref>`."
        )
        return
    click.echo(f"  pointer digest:   {result.pointer_digest}")
    for layer in layers:
        click.echo(f"  layer [{layer['kind']}] {layer['ref']} ({layer['digest']})")
    if result.drift:
        click.secho(
            f"Drift: recomposing the source now yields {result.recomposed_digest} "
            f"(classification: {result.drift_classification})",
            fg="red",
            bold=True,
        )
        for line in result.drift_changes:
            click.secho(f"  {line}", fg="red" if "[weakening]" in line else None)
        click.echo("Deliver it with `cruxible config refresh`.")
    else:
        click.echo("No drift: recomposing the source now matches the last receipted digest.")
    if result.serving_matches_receipt is False:
        click.secho(
            "Warning: the serving composition does not match the last receipted "
            "digest; mutations fail closed until `cruxible config refresh`.",
            fg="yellow",
        )


@click.command("adopt")
@click.option(
    "--kit",
    "kits",
    multiple=True,
    required=True,
    help="Kit layer ref (repeatable), the same refs `init --kit` accepts.",
)
@click.option(
    "--fragment",
    default=None,
    help="Optional instance-delta fragment path (contained in the instance root).",
)
@click.option("--yes", is_flag=True, help="Accept the shown diff without prompting.")
@handle_errors
def adopt_config_cmd(kits: tuple[str, ...], fragment: str | None, yes: bool) -> None:
    """Migrate a materialized instance to a config source pointer (one-time, admin).

    Composes the declared layers, shows the FULL diff against the currently
    served materialized config (the accumulated drift since init) with its
    governance classification, and on explicit acceptance writes
    config-source.yaml, re-materializes the kit dirs, rebuilds the workflow
    lock, receipts the adopt, and retires config.yaml as
    config.materialized.bak. Any failure leaves the instance as it was.
    """
    kit_refs = list(kits)
    preview = _dispatch_cli_instance(
        lambda client, instance_id: client.config_adopt(
            instance_id, kits=kit_refs, fragment=fragment, accept=False
        ),
        lambda instance: service_adopt_config(instance, kits=kit_refs, fragment=fragment),
        allow_local=False,
        command_name="config adopt",
    )
    click.secho(
        f"Adopt preview: {preview.classification}",
        fg=_REFRESH_CLASSIFICATION_COLORS.get(preview.classification),
        bold=True,
    )
    click.echo(f"  serving (materialized): {preview.before_composed_digest}")
    click.echo(f"  proposed (composed):    {preview.after_composed_digest}")
    for layer in _layer_dicts(preview.layers):
        click.echo(f"  layer [{layer['kind']}] {layer['ref']} ({layer['digest']})")
    if preview.config_diff:
        click.secho("Full config diff (accumulated drift since init):", bold=True)
        for line in preview.config_diff:
            click.echo(f"  {line}")
    else:
        click.echo("Full config diff: the composed source matches the served config exactly.")
    _echo_governance_diff(preview.governance_changes)
    for warning in preview.warnings:
        click.secho(f"  Warning: {warning}", fg="yellow")
    if not yes:
        click.confirm(
            "Write the source pointer and retire the materialized config?",
            default=False,
            abort=True,
        )
    result = _dispatch_cli_instance(
        lambda client, instance_id: client.config_adopt(
            instance_id, kits=kit_refs, fragment=fragment, accept=True
        ),
        lambda instance: service_adopt_config(
            instance, kits=kit_refs, fragment=fragment, accept=True
        ),
        allow_local=False,
        command_name="config adopt",
    )
    click.secho(f"Config adopted: {result.classification}", fg="green", bold=True)
    click.echo(f"  pointer digest: {result.pointer_digest}")
    click.echo(f"  composed digest: {result.after_composed_digest}")
    click.echo(f"  lock: {result.lock_path}")
    if result.config_backup_path:
        click.echo(f"  retired config: {result.config_backup_path}")
    if result.receipt_id:
        click.echo(f"Receipt: {result.receipt_id}")
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

    client = _common._get_client()
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
    raise click.UsageError(
        "Local mutation disabled for config add-decision-policy; use server mode."
    )
