"""Built-in workflow steps for materializing governed graph changes.

These helpers split canonical writes into two phases. ``make_*`` steps convert
workflow rows into typed in-memory artifacts with duplicate diagnostics.
``apply_*`` steps validate those artifacts against config and graph state, then
either preview the writes on a cloned graph or persist them during apply mode.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.config.schema import CoreConfig, MakeEntitiesSpec, MakeRelationshipsSpec
from cruxible_core.errors import DataValidationError, QueryExecutionError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import (
    EvidenceRef,
    RelationshipEvidence,
    merge_evidence_ref_objects,
)
from cruxible_core.graph.operations import (
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.workflow.refs import resolve_value
from cruxible_core.workflow.step_helpers import (
    MAX_DUPLICATE_EXAMPLES,
    resolve_step_items,
)
from cruxible_core.workflow.types import (
    ApplyEntitiesPreview,
    ApplyRelationshipsPreview,
    EntitySet,
    RelationshipSet,
)


def make_entity_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeEntitiesSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> EntitySet:
    """Build an entity-upsert artifact from workflow rows.

    The ``make_entities`` step resolves one entity id and property payload per
    source item. It verifies that the target entity type exists, dedupes repeated
    entity ids with first-wins semantics, and records duplicate diagnostics for
    preview/receipt output.

    Property schema validation is intentionally deferred to ``apply_entities`` so
    this step stays a pure artifact-construction step.
    """
    if spec.entity_type not in config.entity_types:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown entity type '{spec.entity_type}'"
        )
    items = resolve_step_items(spec.items, input_payload, step_outputs)
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
            if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
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


def make_relationship_set(
    config: CoreConfig,
    step_id: str,
    spec: MakeRelationshipsSpec,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
) -> RelationshipSet:
    """Build a relationship-upsert artifact from workflow rows.

    The ``make_relationships`` step resolves relationship endpoints and
    properties for one configured relationship type. It checks that produced
    endpoint types match the configured relationship direction, dedupes repeated
    relationship tuples with first-wins semantics, and keeps duplicate
    diagnostics for debugging.

    Endpoint existence and relationship property validation are deferred to
    ``apply_relationships``, where graph state is available.
    """
    rel_schema = config.get_relationship(spec.relationship_type)
    if rel_schema is None:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' references unknown relationship '{spec.relationship_type}'"
        )
    items = resolve_step_items(spec.items, input_payload, step_outputs)
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    relationships: list[RelationshipInstance] = []
    duplicate_input_count = 0
    conflicting_duplicate_count = 0
    duplicate_examples: list[dict[str, Any]] = []
    for item in items:
        relationship_evidence: RelationshipEvidence | None = None
        if spec.evidence is not None:
            evidence_refs: list[EvidenceRef] = []
            rationale: str | None = None
            if spec.evidence.refs is not None:
                evidence_refs = _resolve_evidence_refs(
                    step_id,
                    spec.evidence.refs,
                    input_payload,
                    step_outputs,
                    item,
                )
            if spec.evidence.rationale is not None:
                resolved_rationale = resolve_value(
                    spec.evidence.rationale,
                    input_payload,
                    step_outputs,
                    item_payload=item,
                    allow_item=True,
                )
                if resolved_rationale is not None:
                    rationale = str(resolved_rationale)
            relationship_evidence = RelationshipEvidence(
                evidence_refs=evidence_refs,
                rationale=rationale,
                source_step_ids=[step_id],
            )
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
        if relationship_evidence is not None:
            member.metadata = RelationshipMetadata(evidence=relationship_evidence)
        if member.from_type != rel_schema.from_entity or member.to_type != rel_schema.to_entity:
            raise QueryExecutionError(
                f"Workflow step '{step_id}' produced relationship types "
                f"{member.from_type}->{member.to_type} which do not match "
                f"'{spec.relationship_type}' ({rel_schema.from_entity}->{rel_schema.to_entity})"
            )
        key = member.identity_tuple()
        if key in seen:
            duplicate_input_count += 1
            conflicting = seen[key] != member.properties
            if conflicting:
                conflicting_duplicate_count += 1
            if len(duplicate_examples) < MAX_DUPLICATE_EXAMPLES:
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


def apply_entity_set(
    instance: InstanceProtocol,
    graph: EntityGraph,
    step_id: str,
    raw_entity_set: Any,
    receipt_builder: ReceiptBuilder,
    *,
    persist_writes: bool,
    parent_id: str | None,
) -> ApplyEntitiesPreview:
    """Validate and preview or persist entity writes for a canonical workflow.

    The ``apply_entities`` step consumes an ``EntitySet``, enforces ownership,
    validates every entity against the current config, and then applies
    create/update/noop decisions to the provided graph object. The executor
    passes a cloned graph for canonical previews and commits that graph to live
    state only in apply mode. ``persist_writes`` controls receipt write-node
    recording, not graph mutation.

    All validation is completed before any entity write is applied.
    """
    entity_set = EntitySet.model_validate(raw_entity_set)

    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity_set.entity_type],
    )
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


def apply_relationship_set(
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
    """Validate and preview or persist relationship writes for a workflow.

    The ``apply_relationships`` step consumes a ``RelationshipSet``, enforces
    ownership, validates every relationship against config and graph state, and
    then applies create/update/noop decisions to the provided graph object. The
    executor passes a cloned graph for canonical previews and commits that graph
    to live state only in apply mode. ``persist_writes`` controls receipt
    write-node recording, not graph mutation. Relationship writes go through
    ``apply_relationship`` so relationship metadata is handled by the shared
    graph operation.

    All validation is completed before any relationship write is applied.
    """
    relationship_set = RelationshipSet.model_validate(raw_relationship_set)

    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[relationship_set.relationship_type],
    )
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
        validated.relationship.metadata = rel.metadata
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
        evidence_changed = (
            rel.metadata.evidence is not None
            and existing.metadata.evidence != rel.metadata.evidence
        )
        if existing.properties != rel.properties or evidence_changed:
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
    """Return true when a patch-style entity update would change stored properties."""
    return any(current.get(key) != value for key, value in new_properties.items())


def _resolve_evidence_refs(
    step_id: str,
    template: Any,
    input_payload: dict[str, Any],
    step_outputs: dict[str, Any],
    item: Any,
) -> list[EvidenceRef]:
    resolved = resolve_value(
        template,
        input_payload,
        step_outputs,
        item_payload=item,
        allow_item=True,
    )
    if resolved is None:
        return []
    refs = resolved if isinstance(resolved, list) else [resolved]
    try:
        evidence_refs: list[EvidenceRef] = merge_evidence_ref_objects(refs)
        return evidence_refs
    except (TypeError, ValueError) as exc:
        raise QueryExecutionError(
            f"Workflow step '{step_id}' evidence refs must be evidence objects"
        ) from exc


def compute_apply_digest(
    plan: Any,
    head_snapshot_id: str | None,
    apply_previews: dict[str, Any],
) -> str | None:
    """Compute the preview/apply identity digest for a canonical workflow.

    The digest binds the workflow name, normalized input, lock digest, current
    head snapshot, and sorted apply previews. ``service_apply_workflow`` uses it
    to ensure the apply request matches the preview the caller inspected.
    """
    if plan.workflow_type != "canonical" or not apply_previews:
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
