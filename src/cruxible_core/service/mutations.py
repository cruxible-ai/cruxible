"""Mutation service functions — add_entities and add_relationships."""

from __future__ import annotations

from collections.abc import Sequence

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.operations import (
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.service.mutation_receipts import (
    MutationReceiptContext,
    mutation_receipt,
    save_graph_for_mutation,
)
from cruxible_core.service.types import (
    AddEntityResult,
    AddRelationshipResult,
    EntityWriteInput,
    RelationshipWriteInput,
)


def _entity_from_input(value: EntityWriteInput) -> EntityInstance:
    return EntityInstance(
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        properties=value.properties,
        metadata=value.metadata,
    )


def _relationship_from_input(value: RelationshipWriteInput) -> RelationshipInstance:
    return RelationshipInstance(
        from_type=value.from_type,
        from_id=value.from_id,
        relationship_type=value.relationship_type,
        to_type=value.to_type,
        to_id=value.to_id,
        properties=value.properties,
    )


def service_add_entity_inputs(
    instance: InstanceProtocol,
    entities: Sequence[EntityWriteInput],
    *,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Normalize entity write inputs, then add or update graph entities."""
    return service_add_entities(
        instance,
        [_entity_from_input(entity) for entity in entities],
        _create_receipt=_create_receipt,
    )


def service_add_entities(
    instance: InstanceProtocol,
    entities: Sequence[EntityInstance],
    *,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Add or update entities in the graph (batch upsert).

    Validates all entities first, then applies atomically.
    Raises DataValidationError on duplicates within the batch or schema violations.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity.entity_type for entity in entities],
    )
    config = instance.load_config()
    graph = instance.load_graph()

    ctx: MutationReceiptContext[AddEntityResult]
    with mutation_receipt(
        instance,
        "add_entity",
        {"count": len(entities)},
        enabled=_create_receipt,
    ) as ctx:
        builder = ctx.builder
        errors: list[str] = []
        batch_seen: set[tuple[str, str]] = set()
        pending = []

        for i, ent in enumerate(entities, start=1):
            key = (ent.entity_type, ent.entity_id)
            if key in batch_seen:
                errors.append(f"Entity {i}: duplicate in batch {ent.entity_type}:{ent.entity_id}")
                if builder:
                    builder.record_validation(
                        passed=False,
                        detail={"entity": i, "error": "duplicate in batch"},
                    )
                continue

            try:
                validated = validate_entity(
                    config,
                    graph,
                    ent.entity_type,
                    ent.entity_id,
                    ent.properties,
                    metadata=ent.metadata,
                )
            except DataValidationError as exc:
                errors.append(f"Entity {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"entity": i, "error": str(exc)})
                continue

            batch_seen.add(key)
            pending.append(validated)
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={"entity_type": ent.entity_type, "entity_id": ent.entity_id},
                )

        if errors:
            raise DataValidationError(
                f"Entity validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        added = 0
        updated = 0
        for validated in pending:
            apply_entity(graph, validated)
            if builder:
                builder.record_entity_write(
                    validated.entity.entity_type,
                    validated.entity.entity_id,
                    is_update=validated.is_update,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        save_graph_for_mutation(instance, graph)
        ctx.set_result(AddEntityResult(added=added, updated=updated))

    result = ctx.result
    assert result is not None
    return result


def service_add_relationship_inputs(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipWriteInput],
    source: str,
    source_ref: str,
    *,
    _create_receipt: bool = True,
) -> AddRelationshipResult:
    """Normalize relationship write inputs, then add or update graph relationships."""
    return service_add_relationships(
        instance,
        [_relationship_from_input(relationship) for relationship in relationships],
        source=source,
        source_ref=source_ref,
        _create_receipt=_create_receipt,
    )


def service_add_relationships(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipInstance],
    source: str,
    source_ref: str,
    *,
    _create_receipt: bool = True,
) -> AddRelationshipResult:
    """Add or update relationships in the graph (batch upsert).

    Validates all relationships first, then applies atomically.
    New edges get provenance stamped. Updated edges merge domain properties and
    preserve existing relationship metadata.
    Raises DataValidationError on duplicates within the batch or schema violations.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        relationship_types=[relationship.relationship_type for relationship in relationships],
    )
    config = instance.load_config()
    graph = instance.load_graph()

    ctx: MutationReceiptContext[AddRelationshipResult]
    with mutation_receipt(
        instance,
        "add_relationship",
        {"count": len(relationships), "source": source},
        enabled=_create_receipt,
    ) as ctx:
        builder = ctx.builder
        errors: list[str] = []
        batch_seen: set[tuple[str, str, str, str, str]] = set()
        pending = []

        for i, edge in enumerate(relationships, start=1):
            key = (
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if key in batch_seen:
                errors.append(
                    f"Edge {i}: duplicate in batch "
                    f"{edge.from_type}:{edge.from_id} "
                    f"-[{edge.relationship_type}]-> "
                    f"{edge.to_type}:{edge.to_id}"
                )
                if builder:
                    builder.record_validation(
                        passed=False, detail={"edge": i, "error": "duplicate in batch"}
                    )
                continue

            try:
                validated = validate_relationship(
                    config,
                    graph,
                    edge.from_type,
                    edge.from_id,
                    edge.relationship_type,
                    edge.to_type,
                    edge.to_id,
                    edge.properties,
                )
            except DataValidationError as exc:
                errors.append(f"Edge {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"edge": i, "error": str(exc)})
                continue

            validated.relationship.metadata = edge.metadata
            batch_seen.add(key)
            pending.append((validated, edge))
            if builder:
                builder.record_validation(
                    passed=True,
                    detail={
                        "from": f"{edge.from_type}:{edge.from_id}",
                        "to": f"{edge.to_type}:{edge.to_id}",
                        "relationship": edge.relationship_type,
                    },
                )

        if errors:
            raise DataValidationError(
                f"Relationship validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        added = 0
        updated = 0
        for validated, edge in pending:
            apply_relationship(graph, validated, source, source_ref)
            if builder:
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=validated.is_update,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        save_graph_for_mutation(instance, graph)
        ctx.set_result(AddRelationshipResult(added=added, updated=updated))

    result = ctx.result
    assert result is not None
    return result
