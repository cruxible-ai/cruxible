"""Mutation service functions — add_entities and add_relationships."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import EvidenceRef, RelationshipEvidence
from cruxible_core.graph.operations import (
    ValidatedEntity,
    ValidatedRelationship,
    apply_entity,
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
from cruxible_core.service.evidence import resolve_evidence_refs
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.types import (
    AddEntityResult,
    AddRelationshipResult,
    BatchDirectWriteInput,
    BatchDirectWriteResult,
    BatchRelationshipWriteInput,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
)


@dataclass
class _PreparedBatchRelationship:
    validated: ValidatedRelationship
    relationship: RelationshipInstance
    evidence_refs: list[EvidenceRef]


@dataclass
class _PreparedBatchDirectWrite:
    graph: EntityGraph
    entities: list[ValidatedEntity]
    relationships: list[_PreparedBatchRelationship]
    validation_errors: list[str]
    validation_warnings: list[str]
    evidence_sources_used: list[str]


def _entity_from_input(value: EntityWriteInput) -> EntityInstance:
    return EntityInstance(
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        properties=value.properties,
        metadata=value.metadata,
    )


def _relationship_from_input(
    instance: InstanceProtocol,
    value: RelationshipWriteInput,
) -> RelationshipInstance:
    evidence_refs = resolve_evidence_refs(
        instance,
        evidence_refs=value.evidence_refs,
        source_evidence=value.source_evidence,
    )
    metadata = RelationshipMetadata()
    if evidence_refs or value.evidence_rationale is not None:
        metadata = RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=evidence_refs,
                rationale=value.evidence_rationale,
            )
        )
    return RelationshipInstance(
        from_type=value.from_type,
        from_id=value.from_id,
        relationship_type=value.relationship_type,
        to_type=value.to_type,
        to_id=value.to_id,
        properties=value.properties,
        metadata=metadata,
    )


def _shared_evidence_input(value: SharedEvidenceInput | Mapping[str, Any]) -> SharedEvidenceInput:
    if isinstance(value, SharedEvidenceInput):
        return value
    return SharedEvidenceInput(
        evidence_refs=value.get("evidence_refs", ()),
        source_evidence=value.get("source_evidence", ()),
    )


def _relationship_from_batch_input(
    instance: InstanceProtocol,
    value: BatchRelationshipWriteInput,
    shared_evidence: Mapping[str, SharedEvidenceInput | Mapping[str, Any]],
) -> tuple[RelationshipInstance, list[EvidenceRef]]:
    evidence_refs: list[EvidenceRef | Mapping[str, Any]] = []
    source_evidence: list[Any] = []
    for key in value.shared_evidence_keys:
        shared = shared_evidence.get(key)
        if shared is None:
            raise DataValidationError(f"shared_evidence key '{key}' not found")
        shared_input = _shared_evidence_input(shared)
        evidence_refs.extend(shared_input.evidence_refs)
        source_evidence.extend(shared_input.source_evidence)
    evidence_refs.extend(value.evidence_refs)
    source_evidence.extend(value.source_evidence)
    resolved_refs = resolve_evidence_refs(
        instance,
        evidence_refs=evidence_refs,
        source_evidence=source_evidence,
    )
    metadata = RelationshipMetadata()
    if resolved_refs or value.evidence_rationale is not None:
        metadata = RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=resolved_refs,
                rationale=value.evidence_rationale,
            )
        )
    return (
        RelationshipInstance(
            from_type=value.from_type,
            from_id=value.from_id,
            relationship_type=value.relationship_type,
            to_type=value.to_type,
            to_id=value.to_id,
            properties=value.properties,
            metadata=metadata,
        ),
        resolved_refs,
    )


def _record_evidence_sources(
    evidence_sources: list[str],
    evidence_seen: set[str],
    refs: Sequence[EvidenceRef],
) -> None:
    for ref in refs:
        if ref.source not in evidence_seen:
            evidence_seen.add(ref.source)
            evidence_sources.append(ref.source)


def _prepare_batch_direct_write(
    instance: InstanceProtocol,
    payload: BatchDirectWriteInput,
    *,
    builder: Any | None = None,
) -> _PreparedBatchDirectWrite:
    config = instance.load_config()
    graph = EntityGraph.from_dict(instance.load_graph().to_dict())
    errors: list[str] = []
    warnings: list[str] = []
    evidence_sources: list[str] = []
    evidence_seen: set[str] = set()
    entity_seen: set[tuple[str, str]] = set()
    relationship_seen: set[tuple[str, str, str, str, str]] = set()
    validated_entities: list[ValidatedEntity] = []
    validated_relationships: list[_PreparedBatchRelationship] = []

    for index, entity in enumerate(payload.entities, start=1):
        entity_key = (entity.entity_type, entity.entity_id)
        if entity_key in entity_seen:
            message = f"Entity {index}: duplicate in batch {entity.entity_type}:{entity.entity_id}"
            errors.append(message)
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"entity": index, "error": "duplicate in batch"},
                )
            continue
        try:
            validated_entity = validate_entity(
                config,
                graph,
                entity.entity_type,
                entity.entity_id,
                entity.properties,
                metadata=entity.metadata,
            )
        except DataValidationError as exc:
            errors.append(f"Entity {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"entity": index, "error": str(exc)},
                )
            continue
        entity_seen.add(entity_key)
        validated_entities.append(validated_entity)
        apply_entity(graph, validated_entity)
        if builder:
            builder.record_validation(
                passed=True,
                detail={"entity_type": entity.entity_type, "entity_id": entity.entity_id},
            )

    for index, relationship in enumerate(payload.relationships, start=1):
        try:
            edge, refs = _relationship_from_batch_input(
                instance,
                relationship,
                payload.shared_evidence,
            )
        except DataValidationError as exc:
            errors.append(f"Relationship {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": str(exc)},
                )
            continue
        relationship_key = edge.identity_tuple()
        if relationship_key in relationship_seen:
            errors.append(
                f"Relationship {index}: duplicate in batch "
                f"{edge.from_type}:{edge.from_id} "
                f"-[{edge.relationship_type}]-> "
                f"{edge.to_type}:{edge.to_id}"
            )
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": "duplicate in batch"},
                )
            continue
        try:
            validated_relationship = validate_relationship(
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
            errors.append(f"Relationship {index}: {exc}")
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": str(exc)},
                )
            continue
        validated_relationship.relationship.metadata = edge.metadata
        relationship_seen.add(relationship_key)
        validated_relationships.append(
            _PreparedBatchRelationship(
                validated=validated_relationship,
                relationship=edge,
                evidence_refs=refs,
            )
        )
        _record_evidence_sources(evidence_sources, evidence_seen, refs)
        if builder:
            builder.record_validation(
                passed=True,
                detail={
                    "from": f"{edge.from_type}:{edge.from_id}",
                    "to": f"{edge.to_type}:{edge.to_id}",
                    "relationship": edge.relationship_type,
                },
            )

    return _PreparedBatchDirectWrite(
        graph=graph,
        entities=validated_entities,
        relationships=validated_relationships,
        validation_errors=errors,
        validation_warnings=warnings,
        evidence_sources_used=evidence_sources,
    )


def _batch_direct_write_result(
    prepared: _PreparedBatchDirectWrite,
    *,
    dry_run: bool,
    receipt_id: str | None = None,
) -> BatchDirectWriteResult:
    return BatchDirectWriteResult(
        dry_run=dry_run,
        valid=not prepared.validation_errors,
        entities_added=sum(1 for item in prepared.entities if not item.is_update),
        entities_updated=sum(1 for item in prepared.entities if item.is_update),
        relationships_added=sum(
            1 for item in prepared.relationships if not item.validated.is_update
        ),
        relationships_updated=sum(
            1 for item in prepared.relationships if item.validated.is_update
        ),
        validation_errors=list(prepared.validation_errors),
        validation_warnings=list(prepared.validation_warnings),
        evidence_sources_used=list(prepared.evidence_sources_used),
        receipt_id=receipt_id,
    )


def service_batch_direct_write(
    instance: InstanceProtocol,
    payload: BatchDirectWriteInput,
    *,
    dry_run: bool = False,
    source: str = "batch_direct_write",
    source_ref: str = "batch-direct-write",
) -> BatchDirectWriteResult:
    """Validate or apply one direct entity/relationship write payload."""
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity.entity_type for entity in payload.entities],
        relationship_types=[
            relationship.relationship_type for relationship in payload.relationships
        ],
    )

    if dry_run:
        prepared = _prepare_batch_direct_write(instance, payload)
        return _batch_direct_write_result(prepared, dry_run=True)

    with mutation_receipt(
        instance,
        "batch_direct_write",
        {
            "entity_count": len(payload.entities),
            "relationship_count": len(payload.relationships),
            "shared_evidence_count": len(payload.shared_evidence),
            "source": source,
        },
    ) as ctx:
        builder = ctx.builder
        prepared = _prepare_batch_direct_write(instance, payload, builder=builder)
        if prepared.validation_errors:
            raise DataValidationError(
                f"Batch direct write validation failed with "
                f"{len(prepared.validation_errors)} error(s)",
                errors=prepared.validation_errors,
            )

        touched_entities = []
        for entity_item in prepared.entities:
            persisted_entity = prepared.graph.get_entity(
                entity_item.entity.entity_type,
                entity_item.entity.entity_id,
            )
            if persisted_entity is not None:
                touched_entities.append(persisted_entity)
            if builder:
                builder.record_entity_write(
                    entity_item.entity.entity_type,
                    entity_item.entity.entity_id,
                    is_update=entity_item.is_update,
                )

        touched_relationships = []
        for relationship_item in prepared.relationships:
            edge = relationship_item.relationship
            apply_relationship(prepared.graph, relationship_item.validated, source, source_ref)
            persisted_relationship = prepared.graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if persisted_relationship is not None:
                touched_relationships.append(persisted_relationship)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload()
                            for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = (
                            edge.metadata.evidence.rationale
                        )
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=relationship_item.validated.is_update,
                    detail=evidence_detail,
                )

        save_graph_for_mutation(
            instance,
            prepared.graph,
            entities=touched_entities,
            relationships=touched_relationships,
            uow=ctx.uow,
        )
        ctx.set_result(_batch_direct_write_result(prepared, dry_run=False))

    result = ctx.result
    assert isinstance(result, BatchDirectWriteResult)
    return result


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
        touched_entities = []
        for validated in pending:
            apply_entity(graph, validated)
            persisted = graph.get_entity(
                validated.entity.entity_type,
                validated.entity.entity_id,
            )
            if persisted is not None:
                touched_entities.append(persisted)
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

        save_graph_for_mutation(
            instance,
            graph,
            entities=touched_entities,
            relationships=[],
            uow=ctx.uow,
        )
        ctx.set_result(AddEntityResult(added=added, updated=updated))

    result = ctx.result
    assert isinstance(result, AddEntityResult)
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
        [_relationship_from_input(instance, relationship) for relationship in relationships],
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
            key = edge.identity_tuple()
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
        touched_relationships = []
        for validated, edge in pending:
            apply_relationship(graph, validated, source, source_ref)
            persisted = graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
            )
            if persisted is not None:
                touched_relationships.append(persisted)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload()
                            for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = (
                            edge.metadata.evidence.rationale
                        )
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=validated.is_update,
                    detail=evidence_detail,
                )
            if validated.is_update:
                updated += 1
            else:
                added += 1

        save_graph_for_mutation(
            instance,
            graph,
            entities=[],
            relationships=touched_relationships,
            uow=ctx.uow,
        )
        ctx.set_result(AddRelationshipResult(added=added, updated=updated))

    result = ctx.result
    assert isinstance(result, AddRelationshipResult)
    return result
