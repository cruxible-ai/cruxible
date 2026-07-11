"""Config-defined mutation guard evaluation for direct graph writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    normalize_value,
)
from cruxible_core.config.schema import (
    ActorIdentityGuardCondition,
    CoreConfig,
    CoWriteGuardCondition,
    EvidenceRequirementGuardCondition,
    MutationGuardSchema,
    NamedQueryResultCountGuardCondition,
)
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.evidence import RelationshipEvidence
from cruxible_core.graph.operations import ValidatedEntity, ValidatedRelationship
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.query.engine import execute_query
from cruxible_core.query.predicates import (
    entity_matches_predicates,
    entity_matches_related_predicates,
)
from cruxible_core.receipt.types import Receipt
from cruxible_core.source_artifacts.store import SourceArtifactStoreProtocol

_MISSING = object()


@dataclass(frozen=True)
class _GuardEntityContext:
    current: EntityInstance | None
    proposed: EntityInstance
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class GuardWriteDelta:
    """Entities and edges newly created in the current unit-of-work write.

    Only the ``co_write`` condition reads this; ``actor``/``query``/``evidence``
    conditions ignore it. "Created in THIS write" means present here, not merely
    reachable in the proposed graph — a stale pre-existing linked entity does not
    satisfy a co-write requirement. Created entities are keyed by identity so a
    ``kind`` property filter can read the co-written entity's properties.
    """

    created_entities: Mapping[tuple[str, str], EntityInstance]
    created_edges: frozenset[tuple[str, str, str, str, str]]

    @classmethod
    def empty(cls) -> GuardWriteDelta:
        return cls(created_entities={}, created_edges=frozenset())


@dataclass(frozen=True)
class CreationActorResolution:
    """Outcome of resolving an entity's creation actor from provenance.

    ``found`` carries the actor id recorded on the entity's committed creation
    receipt. Every other status (``no_actor`` for pre-auth creation receipts,
    ``not_found`` for entities with no committed creation receipt — e.g.
    clone/import-materialized records, ``error`` for lookup failures) is a
    refusal for ``distinct_from_creation_actor`` conditions: separation passes
    only on positive proof.
    """

    status: Literal["found", "no_actor", "not_found", "error"]
    actor_id: str | None = None


CreationActorResolver = Callable[[str, str], CreationActorResolution]
"""Resolve ``(entity_type, entity_id)`` to the entity's creation actor."""


def receipt_creation_actor_resolver(instance: InstanceProtocol) -> CreationActorResolver:
    """Build a creation-actor resolver backed by the instance receipt store.

    The creation anchor is the newest COMMITTED receipt containing an
    ``entity_write`` node for the target with ``is_update`` false — the write
    that created the record as it exists now (after a hypothetical hard
    delete + recreate, the newest creation is the honest creator of the current
    record). The receipt's top-level ``actor_context`` is server-derived from
    the authenticated credential, never from the request body, which is why it
    can anchor separation while writable properties and the last-writer
    ``EntityMetadata.actor_context`` cannot.
    """

    def resolve(entity_type: str, entity_id: str) -> CreationActorResolution:
        try:
            store = instance.get_receipt_store()
            try:
                # get_receipts_for_entity orders newest-first by created_at.
                for receipt_id in store.get_receipts_for_entity(entity_type, entity_id):
                    receipt = store.get_receipt(receipt_id)
                    if receipt is None or not receipt.committed:
                        continue
                    if not _receipt_creates_entity(receipt, entity_type, entity_id):
                        continue
                    if receipt.actor_context is None:
                        return CreationActorResolution(status="no_actor")
                    return CreationActorResolution(
                        status="found",
                        actor_id=receipt.actor_context.actor_id,
                    )
            finally:
                store.close()
        except Exception:
            return CreationActorResolution(status="error")
        return CreationActorResolution(status="not_found")

    return resolve


def _receipt_creates_entity(receipt: Receipt, entity_type: str, entity_id: str) -> bool:
    return any(
        node.node_type == "entity_write"
        and node.entity_type == entity_type
        and node.entity_id == entity_id
        and node.detail.get("is_update") is False
        for node in receipt.nodes
    )


def build_guard_write_delta(
    entities: Sequence[ValidatedEntity],
    relationships: Sequence[ValidatedRelationship] = (),
) -> GuardWriteDelta:
    """Build the guard write delta from this UOW's validated creates.

    Only newly-created entities/edges (``is_update`` is False) are included;
    updates re-assert existing state and are not part of the create delta.
    """
    created_entities = {
        (item.entity.entity_type, item.entity.entity_id): item.entity
        for item in entities
        if not item.is_update
    }
    created_edges = frozenset(
        item.relationship.identity_tuple() for item in relationships if not item.is_update
    )
    return GuardWriteDelta(created_entities=created_entities, created_edges=created_edges)


def mutation_guard_errors(
    config: CoreConfig,
    *,
    current_graph: EntityGraph,
    proposed_graph: EntityGraph,
    entities: Sequence[ValidatedEntity],
    actor_context: GovernedActorContext | None = None,
    write_delta: GuardWriteDelta | None = None,
    creation_actor_resolver: CreationActorResolver | None = None,
) -> list[str]:
    """Return mutation guard errors for proposed entity writes (creates and updates).

    ``creation_actor_resolver`` supplies creation-provenance lookups for
    ``distinct_from_creation_actor`` conditions; when it is None those
    conditions fail closed.
    """
    if not config.mutation_guards:
        return []

    delta = write_delta if write_delta is not None else GuardWriteDelta.empty()
    errors: list[str] = []
    for entity in entities:
        current = current_graph.get_entity(
            entity.entity.entity_type,
            entity.entity.entity_id,
        )
        proposed = proposed_graph.get_entity(
            entity.entity.entity_type,
            entity.entity.entity_id,
        )
        if proposed is None:
            continue
        for guard in config.mutation_guards:
            context = _matching_guard_context(
                config, guard, entity, current, proposed, proposed_graph
            )
            if context is None:
                continue
            if not _guard_condition_passes(
                config,
                guard,
                proposed_graph,
                context,
                actor_context=actor_context,
                write_delta=delta,
                creation_actor_resolver=creation_actor_resolver,
            ):
                errors.append(_guard_error_message(guard, entity.entity, context))
    return errors


def relationship_mutation_guard_errors(
    instance: InstanceProtocol,
    config: CoreConfig,
    *,
    current_graph: EntityGraph,
    relationships: Sequence[ValidatedRelationship],
) -> list[str]:
    """Return mutation guard errors for proposed relationship writes."""
    if not config.mutation_guards:
        return []

    evidence_guards = [
        guard
        for guard in config.mutation_guards
        if isinstance(guard.condition, EvidenceRequirementGuardCondition)
    ]
    if not evidence_guards:
        return []

    errors: list[str] = []
    store = instance.get_source_artifact_store()
    try:
        for relationship in relationships:
            for guard in evidence_guards:
                condition = guard.condition
                if not isinstance(condition, EvidenceRequirementGuardCondition):
                    continue
                if guard.relationship_type != relationship.relationship.relationship_type:
                    continue
                evidence = _resulting_relationship_evidence(current_graph, relationship)
                count = _dereferenceable_source_evidence_count(store, evidence)
                if count < condition.min_count:
                    errors.append(
                        _relationship_evidence_guard_error_message(
                            guard,
                            relationship.relationship,
                            required_count=condition.min_count,
                            actual_count=count,
                        )
                    )
    finally:
        store.close()
    return errors


def validate_mutation_guards(
    config: CoreConfig,
    *,
    current_graph: EntityGraph,
    proposed_graph: EntityGraph,
    entities: Sequence[ValidatedEntity],
    actor_context: GovernedActorContext | None = None,
    write_delta: GuardWriteDelta | None = None,
    creation_actor_resolver: CreationActorResolver | None = None,
) -> None:
    """Raise DataValidationError when any proposed entity write violates a guard."""
    errors = mutation_guard_errors(
        config,
        current_graph=current_graph,
        proposed_graph=proposed_graph,
        entities=entities,
        actor_context=actor_context,
        write_delta=write_delta,
        creation_actor_resolver=creation_actor_resolver,
    )
    if errors:
        raise DataValidationError(
            f"Mutation guard validation failed with {len(errors)} error(s)",
            errors=errors,
        )


def _matching_guard_context(
    config: CoreConfig,
    guard: MutationGuardSchema,
    validated: ValidatedEntity,
    current: EntityInstance | None,
    proposed: EntityInstance,
    proposed_graph: EntityGraph,
) -> _GuardEntityContext | None:
    entity = validated.entity
    if guard.entity_type != entity.entity_type:
        return None
    if guard.property not in entity.properties:
        return None

    assert guard.entity_type is not None
    assert guard.property is not None
    property_schema = config.entity_types[guard.entity_type].properties[guard.property]
    guarded_values = [
        normalize_value(value, property_schema, config)
        for value in _guarded_value_list(guard.new_value)
    ]
    old_value = current.properties.get(guard.property, _MISSING) if current else _MISSING
    new_value = proposed.properties.get(guard.property, _MISSING)
    if new_value not in guarded_values:
        return None
    if old_value == new_value:
        return None
    if guard.where is not None and not entity_matches_predicates(config, guard.where, proposed):
        return None
    # Related-edge trigger scoping: the guard fires only when the proposed
    # entity's edges satisfy the related predicates. Edges are evaluated at the
    # canonical visible ("live") relationship state -- the chosen default; there
    # is intentionally no per-guard visibility knob.
    if (guard.where_related or guard.where_not_related) and not entity_matches_related_predicates(
        config,
        proposed_graph,
        proposed,
        guard.where_related,
        guard.where_not_related,
        relationship_state="live",
    ):
        return None
    return _GuardEntityContext(
        current=current,
        proposed=proposed,
        old_value=old_value,
        new_value=new_value,
    )


def _guarded_value_list(new_value: Any) -> list[Any]:
    """Return the guarded values as a list; a scalar ``new_value`` yields one."""
    if isinstance(new_value, list):
        return list(new_value)
    return [new_value]


def _guard_condition_passes(
    config: CoreConfig,
    guard: MutationGuardSchema,
    graph: EntityGraph,
    context: _GuardEntityContext,
    actor_context: GovernedActorContext | None = None,
    write_delta: GuardWriteDelta | None = None,
    creation_actor_resolver: CreationActorResolver | None = None,
) -> bool:
    condition = guard.condition
    if isinstance(condition, NamedQueryResultCountGuardCondition):
        params = _resolve_guard_params(config, condition.params, context)
        result = execute_query(config, graph, condition.query_name, params)
        count = result.total_results if result.total_results is not None else len(result.results)
        if condition.min_count is not None and count < condition.min_count:
            return False
        if condition.max_count is not None and count > condition.max_count:
            return False
        return True
    if isinstance(condition, ActorIdentityGuardCondition):
        if actor_context is None or actor_context.actor_id not in condition.allowed_actor_ids:
            return False
        if not condition.distinct_from_creation_actor:
            return True
        return _distinct_from_creation_actor_passes(
            context,
            actor_context,
            creation_actor_resolver,
        )
    if isinstance(condition, CoWriteGuardCondition):
        delta = write_delta if write_delta is not None else GuardWriteDelta.empty()
        return _co_write_condition_passes(condition, context, delta, config)
    return False


def _distinct_from_creation_actor_passes(
    context: _GuardEntityContext,
    actor_context: GovernedActorContext,
    resolver: CreationActorResolver | None,
) -> bool:
    """Pass only on positive proof the acting actor differs from the creation actor.

    Fail-closed by construction. Refused: creating the entity in THIS write
    (creator == actor trivially, which also covers create-with-guarded-value),
    no resolver wired, resolver raising, no committed creation receipt, creation
    provenance with no recorded actor (pre-auth records), and creation actor ==
    acting actor. Actors compare by actor id (the credential LABEL): rotation /
    re-mint of the same label keeps the identity stable, so an actor cannot shed
    creator identity by re-minting, and minting a NEW label is an admin-tier
    operation.
    """
    if context.current is None:
        return False
    if resolver is None:
        return False
    try:
        resolution = resolver(context.proposed.entity_type, context.proposed.entity_id)
    except Exception:
        return False
    return resolution.status == "found" and resolution.actor_id != actor_context.actor_id


def _co_write_condition_passes(
    condition: CoWriteGuardCondition,
    context: _GuardEntityContext,
    write_delta: GuardWriteDelta,
    config: CoreConfig,
) -> bool:
    """Pass only when THIS write co-creates the required linked entity.

    The required entity (``requires.entity_type``, optionally filtered by its
    ``kind`` property) AND the linking edge (``requires.via_relationship``
    between it and the guarded ``$entity``) must both be present in the write
    delta. A stale pre-existing linked entity or edge does not satisfy the
    requirement: only the delta — never the proposed graph — is consulted here.
    """
    requires = condition.requires
    guarded_key = (context.proposed.entity_type, context.proposed.entity_id)

    for from_type, from_id, to_type, to_id, relationship_type in write_delta.created_edges:
        if relationship_type != requires.via_relationship:
            continue
        # The linking edge must touch the guarded $entity on one endpoint and a
        # co-written required entity on the other.
        if guarded_key == (from_type, from_id):
            other_key = (to_type, to_id)
        elif guarded_key == (to_type, to_id):
            other_key = (from_type, from_id)
        else:
            continue
        if other_key[0] != requires.entity_type:
            continue
        created = write_delta.created_entities.get(other_key)
        if created is None:
            continue
        if requires.kind is not None and not _co_write_kind_matches(config, requires.kind, created):
            continue
        return True
    return False


def _co_write_kind_matches(
    config: CoreConfig,
    required_kind: str,
    created: EntityInstance,
) -> bool:
    """Return whether the co-written entity's ``kind`` property matches the filter."""
    actual = created.properties.get("kind", _MISSING)
    if actual is _MISSING:
        return False
    property_schema = (
        config.entity_types[created.entity_type].properties.get("kind")
        if created.entity_type in config.entity_types
        else None
    )
    expected: Any = required_kind
    if property_schema is not None:
        try:
            expected = normalize_value(required_kind, property_schema, config)
            actual = normalize_value(actual, property_schema, config)
        except ValueError:
            return False
    return bool(actual == expected)


def _resolve_guard_params(
    config: CoreConfig,
    params: Mapping[str, Any],
    context: _GuardEntityContext,
) -> dict[str, Any]:
    scopes = {
        "entity": _entity_view(config, context.proposed),
        # On creates there is no current entity; $current.* refs then fail
        # closed via the missing-reference error.
        "current": None if context.current is None else _entity_view(config, context.current),
        "new_value": context.new_value,
        "old_value": None if context.old_value is _MISSING else context.old_value,
    }
    return {key: _resolve_guard_param_value(value, scopes) for key, value in params.items()}


def _resolve_guard_param_value(
    value: Any,
    scopes: Mapping[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_guard_ref(value, scopes)
    if isinstance(value, list):
        return [_resolve_guard_param_value(item, scopes) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_guard_param_value(item, scopes) for key, item in value.items()}
    return value


def _resolve_guard_ref(
    ref: str,
    scopes: Mapping[str, Any],
) -> Any:
    raw = ref[1:]
    if raw in {"new_value", "old_value"}:
        return scopes[raw]
    scope, sep, path = raw.partition(".")
    if not sep or not path or scope not in {"entity", "current"}:
        raise DataValidationError(f"Unsupported mutation guard param reference '{ref}'")
    value = _resolve_path(scopes[scope], path.split("."))
    if value is _MISSING:
        raise DataValidationError(f"Missing mutation guard param reference '{ref}'")
    return value


def _resolve_path(value: Any, parts: Sequence[str]) -> Any:
    current = value
    for part in parts:
        if current is _MISSING:
            return _MISSING
        if isinstance(current, BaseModel):
            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)
            continue
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        return _MISSING
    return current


def _entity_view(config: CoreConfig, entity: EntityInstance) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "properties": entity_properties_with_identity(
            config,
            entity.entity_type,
            entity.entity_id,
            entity.properties,
        ),
    }


def _guard_error_message(
    guard: MutationGuardSchema,
    entity: EntityInstance,
    context: _GuardEntityContext,
) -> str:
    message = guard.message or "mutation guard condition failed"
    return (
        f"Mutation guard '{guard.name}' rejected write "
        f"{entity.entity_type}:{entity.entity_id} "
        f"{guard.property}={context.new_value!r}: {message}"
    )


def _resulting_relationship_evidence(
    current_graph: EntityGraph,
    validated: ValidatedRelationship,
) -> RelationshipEvidence | None:
    incoming_evidence = validated.relationship.metadata.evidence
    if incoming_evidence is not None:
        return incoming_evidence
    existing = current_graph.get_relationship(
        validated.relationship.from_type,
        validated.relationship.from_id,
        validated.relationship.to_type,
        validated.relationship.to_id,
        validated.relationship.relationship_type,
    )
    if existing is None:
        return None
    return existing.metadata.evidence


def _dereferenceable_source_evidence_count(
    store: SourceArtifactStoreProtocol,
    evidence: RelationshipEvidence | None,
) -> int:
    if evidence is None:
        return 0
    count = 0
    for ref in evidence.evidence_refs:
        if _source_artifact_ref_round_trips(store, ref):
            count += 1
    return count


def _source_artifact_ref_round_trips(
    store: SourceArtifactStoreProtocol,
    ref: Any,
) -> bool:
    if ref.source != "source_artifact" or not ref.artifact_id or not ref.source_record_id:
        return False

    artifact = store.get_artifact(ref.artifact_id)
    if artifact is None:
        return False

    metadata_chunk_id = ref.metadata.get("chunk_id")
    if metadata_chunk_id is not None and metadata_chunk_id != ref.source_record_id:
        return False

    content_hash = ref.metadata.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash.strip():
        return False

    chunk = store.get_chunk(ref.artifact_id, ref.source_record_id)
    if chunk is None or chunk.content_hash != content_hash:
        return False

    artifact_content_hash = ref.metadata.get("artifact_content_hash")
    if artifact_content_hash is not None and artifact_content_hash != artifact.content_hash:
        return False

    return True


def _relationship_evidence_guard_error_message(
    guard: MutationGuardSchema,
    relationship: RelationshipInstance,
    *,
    required_count: int,
    actual_count: int,
) -> str:
    message = guard.message or "relationship evidence requirement failed"
    return (
        f"Mutation guard '{guard.name}' rejected relationship write "
        f"{relationship.relationship_label()}: requires at least "
        f"{required_count} source_evidence ref(s), found {actual_count}: {message}"
    )


__all__ = [
    "CreationActorResolution",
    "CreationActorResolver",
    "GuardWriteDelta",
    "build_guard_write_delta",
    "mutation_guard_errors",
    "receipt_creation_actor_resolver",
    "relationship_mutation_guard_errors",
    "validate_mutation_guards",
]
