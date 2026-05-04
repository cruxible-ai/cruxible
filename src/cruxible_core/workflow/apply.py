"""Built-in workflow steps for materializing graph changes."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cruxible_core.config.schema import CoreConfig, MakeEntitiesSpec, MakeRelationshipsSpec
from cruxible_core.errors import DataValidationError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import (
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import (
    SYSTEM_OWNED_PROPERTIES,
    EntityInstance,
    RelationshipInstance,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    _MAX_DUPLICATE_EXAMPLES,
    _resolve_step_items,
)
from cruxible_core.workflow.types import (
    ApplyEntitiesPreview,
    ApplyRelationshipsPreview,
    EntitySet,
    RelationshipSet,
)


def _make_entity_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeEntitiesSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> EntitySet:
    if spec.entity_type not in config.entity_types:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown entity type '{spec.entity_type}'"
        )
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[str, dict[str, Any]] = {}
    entities: list[EntityInstance] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []
    for item in items:
        entity_id = str(
            resolve_value(
                spec.entity_id,
                input_payload,
                step_outputs,
                item_payload=item,
                allow_item=True,
            )
        )
        properties = resolve_value(
            spec.properties,
            input_payload,
            step_outputs,
            item_payload=item,
            allow_item=True,
        )
        if entity_id in seen:
            duplicate_input_count += 1
            conflicting = seen[entity_id] != properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < _MAX_DUPLICATE_EXAMPLES:
                example = {
                    "entity_id": entity_id,
                    "conflicting": conflicting,
                }
                if conflicting:
                    example["first_properties"] = seen[entity_id]
                    example["duplicate_properties"] = properties
                duplicate_examples.append(example)
            continue
        seen[entity_id] = properties
        entities.append(
            EntityInstance(
                entity_type=spec.entity_type,
                entity_id=entity_id,
                properties=properties,
            )
        )
    return EntitySet(
        entity_type=spec.entity_type,
        entities=entities,
        duplicate_input_count=duplicate_input_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
        duplicate_examples=duplicate_examples,
    )


def _make_relationship_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeRelationshipsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipSet:
    rel_schema = config.get_relationship(spec.relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{spec.relationship_type}'"
        )
    items = _resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    relationships: list[RelationshipInstance] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []
    for item in items:
        member = RelationshipInstance.model_validate(
            {
                "relationship_type": spec.relationship_type,
                "from_type": resolve_value(
                    spec.from_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "from_id": resolve_value(
                    spec.from_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_type": resolve_value(
                    spec.to_type,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "to_id": resolve_value(
                    spec.to_id,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
                "properties": resolve_value(
                    spec.properties,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                ),
            }
        )
        if member.from_type != rel_schema.from_entity or member.to_type != rel_schema.to_entity:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced relationship types "
                f"{member.from_type}->{member.to_type} which do not match "
                f"'{spec.relationship_type}' ({rel_schema.from_entity}->{rel_schema.to_entity})"
            )
        key = (
            spec.relationship_type,
            member.from_type,
            member.from_id,
            member.to_type,
            member.to_id,
        )
        if key in seen:
            duplicate_input_count += 1
            conflicting = seen[key] != member.properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < _MAX_DUPLICATE_EXAMPLES:
                example = {
                    "from_type": member.from_type,
                    "from_id": member.from_id,
                    "to_type": member.to_type,
                    "to_id": member.to_id,
                    "relationship_type": spec.relationship_type,
                    "conflicting": conflicting,
                }
                if conflicting:
                    example["first_properties"] = seen[key]
                    example["duplicate_properties"] = member.properties
                duplicate_examples.append(example)
            continue
        seen[key] = member.properties
        relationships.append(member)
    return RelationshipSet(
        relationship_type=spec.relationship_type,
        relationships=relationships,
        duplicate_input_count=duplicate_input_count,
        conflicting_duplicate_count=conflicting_duplicate_count,
        duplicate_examples=duplicate_examples,
    )


def _apply_entity_set(
    instance: InstanceProtocol,
    graph: EntityGraph,
    step_id: str,
    raw_entity_set: Any,
    receipt_builder: ReceiptBuilder,
    *,
    persist_writes: bool,
    parent_id: str | None,
) -> ApplyEntitiesPreview:
    entity_set = EntitySet.model_validate(raw_entity_set)
    from cruxible_core.service._ownership import check_type_ownership

    check_type_ownership(instance, entity_types=[entity_set.entity_type])
    config = instance.load_config()
    create_count = 0
    update_count = 0
    noop_count = 0
    validated_entities = []
    errors: list[str] = []
    for entity in entity_set.entities:
        try:
            validated = validate_entity(
                config,
                graph,
                entity_set.entity_type,
                entity.entity_id,
                entity.properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(f"{entity_set.entity_type}:{entity.entity_id}: {detail}")
            continue
        validated_entities.append(validated)

    if errors:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' entity property validation failed: " + "; ".join(errors)
        )

    for validated in validated_entities:
        entity = validated.entity
        existing = graph.get_entity(entity_set.entity_type, entity.entity_id)
        if existing is None:
            create_count += 1
            graph.add_entity(entity)
            if persist_writes:
                receipt_builder.record_entity_write(
                    entity_set.entity_type,
                    entity.entity_id,
                    is_update=False,
                    parent_id=parent_id,
                )
            continue
        if _would_update_entity(existing.properties, entity.properties):
            update_count += 1
            graph.update_entity_properties(
                entity_set.entity_type,
                entity.entity_id,
                dict(entity.properties),
            )
            if persist_writes:
                receipt_builder.record_entity_write(
                    entity_set.entity_type,
                    entity.entity_id,
                    is_update=True,
                    parent_id=parent_id,
                )
            continue
        noop_count += 1
    return ApplyEntitiesPreview(
        entity_type=entity_set.entity_type,
        create_count=create_count,
        update_count=update_count,
        noop_count=noop_count,
        duplicate_input_count=entity_set.duplicate_input_count,
        conflicting_duplicate_count=entity_set.conflicting_duplicate_count,
        duplicate_examples=entity_set.duplicate_examples,
    )


def _apply_relationship_set(
    instance: InstanceProtocol,
    graph: EntityGraph,
    workflow_name: str,
    step_id: str,
    raw_relationship_set: Any,
    receipt_builder: ReceiptBuilder,
    *,
    persist_writes: bool,
    parent_id: str | None,
) -> ApplyRelationshipsPreview:
    relationship_set = RelationshipSet.model_validate(raw_relationship_set)
    from cruxible_core.service._ownership import check_type_ownership

    check_type_ownership(instance, relationship_types=[relationship_set.relationship_type])
    config = instance.load_config()
    create_count = 0
    update_count = 0
    noop_count = 0
    validated_relationships = []
    errors: list[str] = []
    for rel in relationship_set.relationships:
        try:
            validated = validate_relationship(
                config,
                graph,
                rel.from_type,
                rel.from_id,
                relationship_set.relationship_type,
                rel.to_type,
                rel.to_id,
                rel.properties,
            )
        except DataValidationError as exc:
            detail = "; ".join(exc.errors) if exc.errors else str(exc)
            errors.append(
                f"{rel.from_type}:{rel.from_id}-[{relationship_set.relationship_type}]->"
                f"{rel.to_type}:{rel.to_id}: {detail}"
            )
            continue
        validated_relationships.append(validated)

    if errors:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' relationship property validation failed: "
            + "; ".join(errors)
        )

    source_ref = f"workflow:{workflow_name}:{step_id}"
    for validated in validated_relationships:
        rel = validated.relationship
        existing = graph.get_relationship(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            relationship_set.relationship_type,
        )
        if existing is None:
            create_count += 1
            apply_relationship(graph, validated, "workflow_apply", source_ref)
            if persist_writes:
                receipt_builder.record_relationship_write(
                    rel.from_type,
                    rel.from_id,
                    rel.to_type,
                    rel.to_id,
                    relationship_set.relationship_type,
                    is_update=False,
                    parent_id=parent_id,
                )
            continue
        existing_domain_properties = {
            key: value
            for key, value in existing.properties.items()
            if key not in SYSTEM_OWNED_PROPERTIES
        }
        if existing_domain_properties != rel.properties:
            update_count += 1
            apply_relationship(graph, validated, "workflow_apply", source_ref)
            if persist_writes:
                receipt_builder.record_relationship_write(
                    rel.from_type,
                    rel.from_id,
                    rel.to_type,
                    rel.to_id,
                    relationship_set.relationship_type,
                    is_update=True,
                    parent_id=parent_id,
                )
            continue
        noop_count += 1
    return ApplyRelationshipsPreview(
        relationship_type=relationship_set.relationship_type,
        create_count=create_count,
        update_count=update_count,
        noop_count=noop_count,
        duplicate_input_count=relationship_set.duplicate_input_count,
        conflicting_duplicate_count=relationship_set.conflicting_duplicate_count,
        duplicate_examples=relationship_set.duplicate_examples,
    )


def _would_update_entity(current: dict[str, Any], new_properties: dict[str, Any]) -> bool:
    return any(current.get(key) != value for key, value in new_properties.items())


def _compute_apply_digest(
    plan: Any,
    head_snapshot_id: str | None,
    apply_previews: dict[str, Any],
) -> str | None:
    if not plan.canonical or not apply_previews:
        return None
    payload = {
        "workflow": plan.workflow,
        "input": plan.input_payload,
        "lock_digest": plan.lock_digest,
        "head_snapshot_id": head_snapshot_id,
        "apply_previews": {key: apply_previews[key] for key in sorted(apply_previews)},
    }
    dumped = json.dumps(payload, sort_keys=True, default=str)
    return f"sha256:{hashlib.sha256(dumped.encode()).hexdigest()}"
