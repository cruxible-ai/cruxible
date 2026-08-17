"""Mutation service functions — add_entities and add_relationships."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cruxible_core.config.ownership import check_upstream_type_ownership
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.assertion_state import RelationshipLifecycleState
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.entity_identity import EntityIdentityWarning
from cruxible_core.graph.evidence import EvidenceRef, RelationshipEvidence
from cruxible_core.graph.operations import (
    ValidatedEntity,
    ValidatedRelationship,
    apply_entity,
    apply_relationship,
    validate_entity,
    validate_relationship,
)
from cruxible_core.graph.property_diffs import property_value_changes
from cruxible_core.graph.provenance import SOURCE_REF_BATCH_DIRECT_WRITE
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
)
from cruxible_core.instance_protocol import InstanceProtocol, ResolutionContractStoreProtocol
from cruxible_core.playbill.actor_context import GovernedActorContext, dump_actor_context
from cruxible_core.service.direct_write_policy import refuse_governed_source_at_direct_write_entry
from cruxible_core.service.evidence import resolve_evidence_refs
from cruxible_core.service.mutation_guards import (
    ContractActivationIntent,
    GuardEvaluation,
    build_guard_write_delta,
    evaluate_mutation_guards,
    evaluate_relationship_mutation_guards,
    receipt_creation_actor_resolver,
    record_contract_activations,
    record_guard_evaluation,
)
from cruxible_core.service.mutation_proposals import (
    batch_direct_write_proposal,
    build_proposal,
    entity_instance_member,
    relationship_instance_member,
)
from cruxible_core.service.mutation_receipts import mutation_receipt, save_graph_for_mutation
from cruxible_core.service.types import (
    AddEntityResult,
    AddRelationshipResult,
    BatchDirectWriteInput,
    BatchDirectWriteResult,
    BatchRelationshipWriteInput,
    DirectWriteGroupInteraction,
    EntityWriteInput,
    RelationshipWriteInput,
    SharedEvidenceInput,
)

BATCH_MISSING_ENDPOINT_HINT = "create the entity first or include it in the same batch"
"""Recovery appended to a batch direct-write dangling-endpoint rejection.

A batch carries entities AND relationships, so a missing endpoint has two
recoveries the caller can act on without another failed round trip: write the
entity in a prior call, or add it to this batch's ``entities``.
"""

ADD_RELATIONSHIP_MISSING_ENDPOINT_HINT = (
    "create the entity first, or use cruxible_batch_direct_write "
    "to write the entity and the relationship in one call"
)
"""Recovery for a relationship-only write, which cannot carry the entity itself."""


@dataclass
class _PreparedBatchRelationship:
    validated: ValidatedRelationship
    relationship: RelationshipInstance
    evidence_refs: list[EvidenceRef]
    pending: bool = False
    # Typed, review-SAFE lifecycle override; applied to ``assertion.lifecycle``
    # only (see ``apply_relationship``). ``None`` leaves lifecycle at its add/
    # update default.
    lifecycle: RelationshipLifecycleState | None = None


@dataclass
class _PreparedBatchDirectWrite:
    graph: EntityGraph
    entities: list[ValidatedEntity]
    entity_write_details: dict[tuple[str, str], dict[str, Any]]
    relationships: list[_PreparedBatchRelationship]
    validation_errors: list[str]
    validation_warnings: list[str]
    identity_warnings: list[EntityIdentityWarning]
    evidence_sources_used: list[str]
    interactions: _DirectWriteGroupInteractions
    contract_activations: tuple[ContractActivationIntent, ...] = ()


@dataclass
class _DirectWriteGroupInteractions:
    """Frozen empty compatibility fields after legacy group retirement."""

    pending_conflicts: list[DirectWriteGroupInteraction]
    updated_group_backed_edges: list[DirectWriteGroupInteraction]


def _entity_property_change_detail(
    graph: EntityGraph,
    validated: ValidatedEntity,
    *,
    actor_context: GovernedActorContext | None = None,
) -> dict[str, Any]:
    entity = validated.entity
    dumped_actor = dump_actor_context(actor_context)
    previous = graph.get_entity(entity.entity_type, entity.entity_id)
    previous_properties = previous.properties if previous is not None else {}
    if validated.is_update:
        property_changes = property_value_changes(
            entity.properties,
            previous_properties,
            include_added=True,
            include_removed=False,
        )
        change_kind = "updated"
    else:
        property_changes = property_value_changes(
            entity.properties,
            {},
            include_added=True,
            include_removed=False,
        )
        change_kind = "created"

    detail: dict[str, Any] = {
        "change_kind": change_kind,
        "property_changes": [
            {
                "property": change.property,
                "from_value": change.from_value,
                "to_value": change.to_value,
            }
            for change in property_changes
        ],
    }
    if dumped_actor is not None:
        detail["actor_context"] = dumped_actor
    return detail


def _empty_direct_write_group_interactions() -> _DirectWriteGroupInteractions:
    """Return the frozen empty result after candidate-group authority retired."""

    return _DirectWriteGroupInteractions(
        pending_conflicts=[],
        updated_group_backed_edges=[],
    )


def _entity_from_input(value: EntityWriteInput) -> EntityInstance:
    return EntityInstance(
        entity_type=value.entity_type,
        entity_id=value.entity_id,
        properties=value.properties,
        metadata=EntityMetadata.from_metadata(value.metadata),
    )


def _relationship_from_input(
    instance: InstanceProtocol,
    value: RelationshipWriteInput,
) -> tuple[RelationshipInstance, bool]:
    evidence_refs = resolve_evidence_refs(
        instance,
        evidence_refs=value.evidence_refs,
        source_evidence=value.source_evidence,
        citation_handles=value.citation_handles,
    )
    metadata = RelationshipMetadata()
    if evidence_refs or value.evidence_rationale is not None:
        metadata = RelationshipMetadata(
            evidence=RelationshipEvidence(
                evidence_refs=evidence_refs,
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
        value.pending,
    )


def _shared_evidence_input(value: SharedEvidenceInput | Mapping[str, Any]) -> SharedEvidenceInput:
    if isinstance(value, SharedEvidenceInput):
        return value
    return SharedEvidenceInput(
        evidence_refs=value.get("evidence_refs", ()),
        source_evidence=value.get("source_evidence", ()),
        citation_handles=value.get("citation_handles", ()),
    )


def _relationship_from_batch_input(
    instance: InstanceProtocol,
    value: BatchRelationshipWriteInput,
    shared_evidence: Mapping[str, SharedEvidenceInput | Mapping[str, Any]],
) -> tuple[RelationshipInstance, list[EvidenceRef]]:
    evidence_refs: list[EvidenceRef | Mapping[str, Any]] = []
    source_evidence: list[Any] = []
    citation_handles: list[str] = []
    for key in value.shared_evidence_keys:
        shared = shared_evidence.get(key)
        if shared is None:
            raise DataValidationError(f"shared_evidence key '{key}' not found")
        shared_input = _shared_evidence_input(shared)
        evidence_refs.extend(shared_input.evidence_refs)
        source_evidence.extend(shared_input.source_evidence)
        citation_handles.extend(shared_input.citation_handles)
    evidence_refs.extend(value.evidence_refs)
    source_evidence.extend(value.source_evidence)
    citation_handles.extend(value.citation_handles)
    resolved_refs = resolve_evidence_refs(
        instance,
        evidence_refs=evidence_refs,
        source_evidence=source_evidence,
        citation_handles=citation_handles,
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
    source: str,
    source_ref: str,
    actor_context: GovernedActorContext | None = None,
    builder: Any | None = None,
    resolution_contract_store: ResolutionContractStoreProtocol | None = None,
) -> _PreparedBatchDirectWrite:
    # Seam check, ahead of every validation and every write: a batch direct
    # write may not name a governed verb as its provenance source. Placed in
    # prepare so the dry-run preview and the applied path refuse identically.
    refuse_governed_source_at_direct_write_entry(source, entry_point="batch_direct_write")
    config = instance.load_config()
    current_graph = instance.load_graph()
    graph = EntityGraph.from_dict(deepcopy(current_graph.to_dict()))
    errors: list[str] = []
    warnings: list[str] = []
    identity_warnings: list[EntityIdentityWarning] = []
    evidence_sources: list[str] = []
    evidence_seen: set[str] = set()
    entity_seen: set[tuple[str, str]] = set()
    relationship_seen: set[tuple[str, str, str, str, str]] = set()
    validated_entities: list[ValidatedEntity] = []
    entity_write_details: dict[tuple[str, str], dict[str, Any]] = {}
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
            identity_warning = apply_entity(
                graph,
                validated_entity,
                config=config,
                source=source,
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
        entity_write_details[entity_key] = _entity_property_change_detail(
            current_graph,
            validated_entity,
            actor_context=actor_context,
        )
        validation_detail: dict[str, Any] = {
            "entity_type": entity.entity_type,
            "entity_id": entity.entity_id,
        }
        if identity_warning is not None:
            identity_warnings.append(identity_warning)
            similar = identity_warning.similar_existing_entity.to_payload()
            entity_write_details[entity_key]["similar_existing_entity"] = similar
            validation_detail["similar_existing_entity"] = similar
        if builder:
            builder.record_validation(
                passed=True,
                detail=validation_detail,
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
                missing_endpoint_hint=BATCH_MISSING_ENDPOINT_HINT,
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
        if relationship.pending and validated_relationship.is_update:
            errors.append(
                f"Relationship {index}: pending relationship writes can only create new edges"
            )
            if builder:
                builder.record_validation(
                    passed=False,
                    detail={"relationship": index, "error": "pending update not allowed"},
                )
            continue
        relationship_seen.add(relationship_key)
        validated_relationships.append(
            _PreparedBatchRelationship(
                validated=validated_relationship,
                relationship=edge,
                evidence_refs=refs,
                pending=relationship.pending,
                lifecycle=relationship.lifecycle,
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

    proposed_guard_graph = graph
    if validated_relationships:
        # Apply the validated edges to a throwaway graph through the shared
        # chokepoint. This serves two purposes: (1) it surfaces a
        # DirectWriteRefusedError for proposal_only types here, in prepare, so
        # both the dry-run preview and the live write refuse identically (entity
        # refusal already happens in prepare via apply_entity above); and (2) it
        # yields the post-write proposed graph the mutation guards evaluate
        # against. The guard error collection below still runs only when guards
        # are configured.
        proposed_guard_graph = EntityGraph.from_dict(deepcopy(graph.to_dict()))
        for relationship_item in validated_relationships:
            apply_relationship(
                proposed_guard_graph,
                relationship_item.validated,
                source=source,
                source_ref=source_ref,
                config=config,
                pending=relationship_item.pending,
                lifecycle=relationship_item.lifecycle,
            )

    try:
        guard_evaluation = evaluate_mutation_guards(
            config,
            current_graph=current_graph,
            proposed_graph=proposed_guard_graph,
            entities=validated_entities,
            actor_context=actor_context,
            write_delta=build_guard_write_delta(
                validated_entities,
                [item.validated for item in validated_relationships],
            ),
            creation_actor_resolver=receipt_creation_actor_resolver(instance),
            resolution_contract_store=resolution_contract_store,
        )
    except DataValidationError as exc:
        guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
    if builder:
        record_guard_evaluation(builder, guard_evaluation)
    errors.extend(guard_evaluation.messages)

    try:
        relationship_guard_evaluation = evaluate_relationship_mutation_guards(
            instance,
            config,
            current_graph=current_graph,
            relationships=[item.validated for item in validated_relationships],
        )
    except DataValidationError as exc:
        relationship_guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
    if builder:
        record_guard_evaluation(builder, relationship_guard_evaluation)
    errors.extend(relationship_guard_evaluation.messages)

    interactions = _empty_direct_write_group_interactions()

    if errors:
        raise DataValidationError(
            f"Batch direct write validation failed with {len(errors)} error(s)",
            errors=errors,
        )

    return _PreparedBatchDirectWrite(
        graph=graph,
        entities=validated_entities,
        entity_write_details=entity_write_details,
        relationships=validated_relationships,
        validation_errors=errors,
        validation_warnings=warnings,
        identity_warnings=identity_warnings,
        evidence_sources_used=evidence_sources,
        interactions=interactions,
        contract_activations=guard_evaluation.contract_activations,
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
        relationships_updated=sum(1 for item in prepared.relationships if item.validated.is_update),
        validation_errors=list(prepared.validation_errors),
        validation_warnings=list(prepared.validation_warnings),
        identity_warnings=list(prepared.identity_warnings),
        evidence_sources_used=list(prepared.evidence_sources_used),
        pending_conflicts=list(prepared.interactions.pending_conflicts),
        updated_group_backed_edges=list(prepared.interactions.updated_group_backed_edges),
        receipt_id=receipt_id,
    )


def service_batch_direct_write(
    instance: InstanceProtocol,
    payload: BatchDirectWriteInput,
    *,
    dry_run: bool = False,
    source: str = "batch_direct_write",
    source_ref: str = SOURCE_REF_BATCH_DIRECT_WRITE,
    actor_context: GovernedActorContext | None = None,
) -> BatchDirectWriteResult:
    """Validate or apply one direct entity/relationship write payload.

    ``dry_run`` previews: refusals raised here (pending-edge, write-policy,
    validation) carry identical semantics to the applied path but are NOT
    receipted — a preview persists nothing, and receipts record what happened,
    not what was previewed.
    """
    check_upstream_type_ownership(
        instance.get_upstream_metadata(),
        entity_types=[entity.entity_type for entity in payload.entities],
        relationship_types=[
            relationship.relationship_type for relationship in payload.relationships
        ],
    )

    if dry_run:
        # A preview opens no unit of work, so it reads eligibility on its own
        # connection. That is safe precisely because it writes nothing: the
        # activation intents it produces are discarded, so no contract is
        # consumed by a write that never happened.
        preview_contract_store = instance.get_resolution_contract_store()
        try:
            prepared = _prepare_batch_direct_write(
                instance,
                payload,
                source=source,
                source_ref=source_ref,
                actor_context=actor_context,
                resolution_contract_store=preview_contract_store,
            )
        finally:
            preview_contract_store.close()
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
        actor_context=actor_context,
    ) as ctx:
        builder = ctx.builder
        if builder:
            proposal, subjects = batch_direct_write_proposal(payload, source=source)
            builder.record_proposal(proposal, subjects=subjects)
        prepared = _prepare_batch_direct_write(
            instance,
            payload,
            source=source,
            source_ref=source_ref,
            actor_context=actor_context,
            builder=builder,
            resolution_contract_store=(
                ctx.uow.resolution_contracts if ctx.uow is not None else None
            ),
        )
        if ctx.uow is not None and prepared.contract_activations:
            record_contract_activations(
                ctx.uow.resolution_contracts,
                prepared.contract_activations,
                acceptance_receipt_id=builder.receipt_id if builder else None,
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
                detail = prepared.entity_write_details.get(
                    (entity_item.entity.entity_type, entity_item.entity.entity_id),
                )
                builder.record_entity_write(
                    entity_item.entity.entity_type,
                    entity_item.entity.entity_id,
                    is_update=entity_item.is_update,
                    detail=detail,
                )

        config = instance.load_config()
        touched_relationships = []
        for relationship_item in prepared.relationships:
            edge = relationship_item.relationship
            persisted_relationship = apply_relationship(
                prepared.graph,
                relationship_item.validated,
                source,
                source_ref,
                config=config,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
                pending=relationship_item.pending,
                lifecycle=relationship_item.lifecycle,
            )
            stamped_relationship = prepared.graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
                edge_key=persisted_relationship.edge_key,
            )
            if stamped_relationship is not None:
                persisted_relationship = stamped_relationship
            touched_relationships.append(persisted_relationship)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload() for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = edge.metadata.evidence.rationale
                if relationship_item.pending:
                    evidence_detail["review_status"] = "pending"
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=relationship_item.validated.is_update,
                    detail=evidence_detail,
                    claim_id=persisted_relationship.claim_id,
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
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
) -> AddEntityResult:
    """Normalize entity write inputs, then add or update graph entities."""
    return service_add_entities(
        instance,
        [_entity_from_input(entity) for entity in entities],
        dry_run=dry_run,
        actor_context=actor_context,
        _create_receipt=_create_receipt,
    )


def service_add_entities(
    instance: InstanceProtocol,
    entities: Sequence[EntityInstance],
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
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
    current_graph = instance.load_graph()
    graph = EntityGraph.from_dict(deepcopy(current_graph.to_dict()))

    with mutation_receipt(
        instance,
        "add_entity",
        {"count": len(entities)},
        enabled=_create_receipt and not dry_run,
        actor_context=actor_context,
    ) as ctx:
        builder = ctx.builder
        if builder:
            proposal, subjects = build_proposal(
                operation="add_entity",
                entities=[entity_instance_member(entity) for entity in entities],
            )
            builder.record_proposal(proposal, subjects=subjects)
        errors: list[str] = []
        batch_seen: set[tuple[str, str]] = set()
        pending = []
        identity_warnings: list[EntityIdentityWarning] = []
        identity_warning_by_key: dict[tuple[str, str], EntityIdentityWarning] = {}

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
                    current_graph,
                    ent.entity_type,
                    ent.entity_id,
                    ent.properties,
                    metadata=ent.metadata,
                )
                identity_warning = apply_entity(
                    graph,
                    validated,
                    config=config,
                    source="add_entity",
                )
            except DataValidationError as exc:
                errors.append(f"Entity {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"entity": i, "error": str(exc)})
                continue

            batch_seen.add(key)
            pending.append(validated)
            validation_detail: dict[str, Any] = {
                "entity_type": ent.entity_type,
                "entity_id": ent.entity_id,
            }
            if identity_warning is not None:
                identity_warnings.append(identity_warning)
                identity_warning_by_key[key] = identity_warning
                validation_detail["similar_existing_entity"] = (
                    identity_warning.similar_existing_entity.to_payload()
                )
            if builder:
                builder.record_validation(
                    passed=True,
                    detail=validation_detail,
                )

        if errors:
            raise DataValidationError(
                f"Entity validation failed with {len(errors)} error(s)",
                errors=errors,
            )

        try:
            guard_evaluation = evaluate_mutation_guards(
                config,
                current_graph=current_graph,
                proposed_graph=graph,
                entities=pending,
                actor_context=actor_context,
                write_delta=build_guard_write_delta(pending),
                creation_actor_resolver=receipt_creation_actor_resolver(instance),
                resolution_contract_store=(
                    ctx.uow.resolution_contracts if ctx.uow is not None else None
                ),
            )
        except DataValidationError as exc:
            guard_evaluation = GuardEvaluation.from_messages([str(exc), *exc.errors])
        if builder:
            record_guard_evaluation(builder, guard_evaluation)
        guard_errors = guard_evaluation.messages
        if guard_errors:
            raise DataValidationError(
                f"Mutation guard validation failed with {len(guard_errors)} error(s)",
                errors=guard_errors,
            )

        if dry_run:
            return AddEntityResult(
                added=sum(1 for validated in pending if not validated.is_update),
                updated=sum(1 for validated in pending if validated.is_update),
                identity_warnings=identity_warnings,
            )

        if ctx.uow is not None and guard_evaluation.contract_activations:
            record_contract_activations(
                ctx.uow.resolution_contracts,
                guard_evaluation.contract_activations,
                acceptance_receipt_id=builder.receipt_id if builder else None,
            )

        added = 0
        updated = 0
        touched_entities = []
        for validated in pending:
            persisted = graph.get_entity(
                validated.entity.entity_type,
                validated.entity.entity_id,
            )
            if persisted is not None:
                touched_entities.append(persisted)
            if builder:
                detail = _entity_property_change_detail(
                    current_graph,
                    validated,
                    actor_context=actor_context,
                )
                identity_warning = identity_warning_by_key.get(
                    (validated.entity.entity_type, validated.entity.entity_id)
                )
                if identity_warning is not None:
                    detail["similar_existing_entity"] = (
                        identity_warning.similar_existing_entity.to_payload()
                    )
                builder.record_entity_write(
                    validated.entity.entity_type,
                    validated.entity.entity_id,
                    is_update=validated.is_update,
                    detail=detail,
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
        ctx.set_result(
            AddEntityResult(
                added=added,
                updated=updated,
                identity_warnings=identity_warnings,
            )
        )

    result = ctx.result
    assert isinstance(result, AddEntityResult)
    return result


def service_add_relationship_inputs(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipWriteInput],
    source: str,
    source_ref: str,
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
) -> AddRelationshipResult:
    """Normalize relationship write inputs, then add or update graph relationships."""
    inputs = list(relationships)
    normalized = [_relationship_from_input(instance, relationship) for relationship in inputs]
    return service_add_relationships(
        instance,
        [relationship for relationship, _pending in normalized],
        source=source,
        source_ref=source_ref,
        dry_run=dry_run,
        actor_context=actor_context,
        _create_receipt=_create_receipt,
        pending=[pending for _relationship, pending in normalized],
        lifecycle=[relationship.lifecycle for relationship in inputs],
    )


def service_add_relationships(
    instance: InstanceProtocol,
    relationships: Sequence[RelationshipInstance],
    source: str,
    source_ref: str,
    *,
    dry_run: bool = False,
    actor_context: GovernedActorContext | None = None,
    _create_receipt: bool = True,
    pending: bool | Sequence[bool] = False,
    lifecycle: RelationshipLifecycleState
    | None
    | Sequence[RelationshipLifecycleState | None] = None,
) -> AddRelationshipResult:
    """Add or update relationships in the graph (batch upsert).

    Refuses a caller-supplied ``source`` that names a governed write verb: the
    chokepoint exempts those names from the ``proposal_only`` refusal, and this
    is a bare direct write, not the proposal or workflow machinery.

    Validates all relationships first, then applies atomically.
    New edges get provenance stamped. Updated edges merge domain properties and
    preserve existing relationship metadata.
    Raises DataValidationError on duplicates within the batch or schema violations.

    ``lifecycle`` is the typed, review-SAFE lifecycle write channel (mirrors the
    batch direct-write path). When supplied per edge it is threaded to
    ``apply_relationship`` which sets ONLY ``assertion.lifecycle``; the review axis
    and group override are never touched from here.

    ``dry_run`` previews: refusals raised here (pending-edge, write-policy,
    validation) carry identical semantics to the applied path but are NOT
    receipted — a preview persists nothing, and receipts record what happened,
    not what was previewed.
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
        enabled=_create_receipt and not dry_run,
        actor_context=actor_context,
    ) as ctx:
        # INSIDE the receipt boundary: a spoof attempt is the single most
        # interesting negative-experience row this entry produces, so it is
        # receipted and the open write transaction rolls back, exactly like the
        # tier checks on the governed rails.
        refuse_governed_source_at_direct_write_entry(source, entry_point="add_relationship")
        builder = ctx.builder
        if builder:
            proposal, subjects = build_proposal(
                operation="add_relationship",
                relationships=[
                    relationship_instance_member(relationship) for relationship in relationships
                ],
                extra={
                    "source": source,
                    "source_ref": source_ref,
                    "pending": pending,
                    "lifecycle": lifecycle,
                },
            )
            builder.record_proposal(proposal, subjects=subjects)
        errors: list[str] = []
        batch_seen: set[tuple[str, str, str, str, str]] = set()
        prepared_relationships = []
        pending_flags = (
            list(pending)
            if isinstance(pending, Sequence) and not isinstance(pending, (str, bytes))
            else [bool(pending)] * len(relationships)
        )
        if len(pending_flags) != len(relationships):
            raise DataValidationError("pending flag count must match relationship count")
        lifecycle_states: list[RelationshipLifecycleState | None] = (
            list(lifecycle) if isinstance(lifecycle, Sequence) else [lifecycle] * len(relationships)
        )
        if len(lifecycle_states) != len(relationships):
            raise DataValidationError("lifecycle count must match relationship count")

        for i, edge in enumerate(relationships, start=1):
            pending_flag = pending_flags[i - 1]
            lifecycle_state = lifecycle_states[i - 1]
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
                    missing_endpoint_hint=ADD_RELATIONSHIP_MISSING_ENDPOINT_HINT,
                )
            except DataValidationError as exc:
                errors.append(f"Edge {i}: {exc}")
                if builder:
                    builder.record_validation(passed=False, detail={"edge": i, "error": str(exc)})
                continue

            validated.relationship.metadata = edge.metadata
            if pending_flag and validated.is_update:
                errors.append(f"Edge {i}: pending relationship writes can only create new edges")
                if builder:
                    builder.record_validation(
                        passed=False,
                        detail={"edge": i, "error": "pending update not allowed"},
                    )
                continue
            batch_seen.add(key)
            prepared_relationships.append((validated, edge, pending_flag, lifecycle_state))
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

        guard_evaluation = evaluate_relationship_mutation_guards(
            instance,
            config,
            current_graph=graph,
            relationships=[
                validated for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
            ],
        )
        if builder:
            record_guard_evaluation(builder, guard_evaluation)
        guard_errors = guard_evaluation.messages
        if guard_errors:
            raise DataValidationError(
                f"Mutation guard validation failed with {len(guard_errors)} error(s)",
                errors=guard_errors,
            )

        interactions = _empty_direct_write_group_interactions()

        # Run the refuse_direct_writes chokepoint here, in the prepare phase that
        # executes for BOTH dry-run and live, so a dry-run preview refuses a
        # proposal_only direct write identically to the live write (mirrors how
        # _prepare_batch_direct_write applies the edges through the chokepoint and
        # how the entity path refuses via apply_entity before its own dry-run
        # early return). Without this, the dry-run branch below would return
        # added=1 for a proposal_only direct add while the live write raises
        # DirectWriteRefusedError. A throwaway graph copy keeps this side-effect
        # free; the live block re-applies to the real graph below. pending=True
        # and governed sources are PERMITTED by the chokepoint, so this never
        # over-refuses.
        refusal_check_graph = EntityGraph.from_dict(deepcopy(graph.to_dict()))
        for validated, _edge, pending_flag, lifecycle_state in prepared_relationships:
            apply_relationship(
                refusal_check_graph,
                validated,
                source,
                source_ref,
                config=config,
                pending=pending_flag,
                lifecycle=lifecycle_state,
            )

        if dry_run:
            return AddRelationshipResult(
                added=sum(
                    1
                    for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
                    if not validated.is_update
                ),
                updated=sum(
                    1
                    for validated, _edge, _pending_flag, _lifecycle in prepared_relationships
                    if validated.is_update
                ),
                pending_conflicts=list(interactions.pending_conflicts),
                updated_group_backed_edges=list(interactions.updated_group_backed_edges),
            )

        added = 0
        updated = 0
        touched_relationships = []
        for validated, edge, pending_flag, lifecycle_state in prepared_relationships:
            persisted = apply_relationship(
                graph,
                validated,
                source,
                source_ref,
                config=config,
                receipt_id=builder.receipt_id if builder else None,
                actor_context=actor_context,
                pending=pending_flag,
                lifecycle=lifecycle_state,
            )
            stamped = graph.get_relationship(
                edge.from_type,
                edge.from_id,
                edge.to_type,
                edge.to_id,
                edge.relationship_type,
                edge_key=persisted.edge_key,
            )
            if stamped is not None:
                persisted = stamped
            touched_relationships.append(persisted)
            if builder:
                evidence_detail: dict[str, object] = {}
                if edge.metadata.evidence is not None:
                    evidence_detail = {
                        "evidence_refs": [
                            ref.to_payload() for ref in edge.metadata.evidence.evidence_refs
                        ],
                    }
                    if edge.metadata.evidence.rationale is not None:
                        evidence_detail["evidence_rationale"] = edge.metadata.evidence.rationale
                if pending_flag:
                    evidence_detail["review_status"] = "pending"
                builder.record_relationship_write(
                    edge.from_type,
                    edge.from_id,
                    edge.to_type,
                    edge.to_id,
                    edge.relationship_type,
                    is_update=validated.is_update,
                    detail=evidence_detail,
                    claim_id=persisted.claim_id,
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
        ctx.set_result(
            AddRelationshipResult(
                added=added,
                updated=updated,
                pending_conflicts=list(interactions.pending_conflicts),
                updated_group_backed_edges=list(interactions.updated_group_backed_edges),
            )
        )

    result = ctx.result
    assert isinstance(result, AddRelationshipResult)
    return result
