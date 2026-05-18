"""Two-phase shared helpers for entity and relationship validation/application.

Phase 1 (validate): Pure functions that check inputs against config/graph,
returning a validated result or raising DataValidationError. No graph mutation.

Phase 2 (apply): Functions that mutate the graph using a validated result.

MCP handlers use validate in batch loops (collect errors, then apply all if
no errors — preserving batch atomicity). CLI validates and applies one at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cruxible_core.config.property_validation import validate_property_payload
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.assertion_state import (
    ASSERTION_PROPERTY,
    LEGACY_REVIEW_STATUS_PROPERTY,
    RelationshipAssertionState,
    RelationshipReviewState,
    dump_assertion_state,
    load_assertion_state,
    review_state_to_legacy_review_status,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import (
    dump_provenance,
    load_provenance,
    make_provenance,
    stamp_provenance_modified,
)
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
)


@dataclass
class ValidatedEntity:
    """Result of validate_entity — ready to apply."""

    entity: EntityInstance
    is_update: bool


@dataclass
class ValidatedRelationship:
    """Result of validate_relationship — ready to apply."""

    relationship: RelationshipInstance
    is_update: bool


def validate_entity(
    config: CoreConfig,
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    properties: dict[str, Any] | None = None,
) -> ValidatedEntity:
    """Validate an entity against config and graph state.

    Raises DataValidationError on failure.
    """
    if entity_type not in config.entity_types:
        raise DataValidationError(f"type '{entity_type}' not found in config")
    if not entity_id.strip():
        raise DataValidationError("entity_id must not be empty")

    is_update = graph.has_entity(entity_type, entity_id)
    entity_schema = config.entity_types[entity_type]
    validation = validate_property_payload(
        config,
        entity_schema.properties,
        properties or {},
        require_required=not is_update,
        primary_key_name=entity_schema.get_primary_key(),
        entity_id=entity_id,
        strip_system_properties=True,
    )
    if validation.errors:
        raise DataValidationError(
            f"Entity '{entity_type}:{entity_id}' property validation failed",
            errors=validation.errors,
        )
    entity = EntityInstance(
        entity_type=entity_type,
        entity_id=entity_id,
        properties=validation.properties,
    )
    return ValidatedEntity(entity=entity, is_update=is_update)


def validate_relationship(
    config: CoreConfig,
    graph: EntityGraph,
    from_type: str,
    from_id: str,
    relationship: str,
    to_type: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
) -> ValidatedRelationship:
    """Validate a relationship against config and graph state.

    Handles property schema checks, system metadata stripping, direction
    checks, and endpoint existence checks.

    Raises DataValidationError on failure.
    """
    props = dict(properties) if properties else {}

    # Validate relationship type exists in config
    rel_schema = config.get_relationship(relationship)
    if rel_schema is None:
        raise DataValidationError(f"relationship '{relationship}' not found in config")

    # Validate endpoint types match config direction
    if from_type != rel_schema.from_entity:
        raise DataValidationError(
            f"from_type '{from_type}' does not match "
            f"relationship '{relationship}' "
            f"which expects '{rel_schema.from_entity}'"
        )
    if to_type != rel_schema.to_entity:
        raise DataValidationError(
            f"to_type '{to_type}' does not match "
            f"relationship '{relationship}' "
            f"which expects '{rel_schema.to_entity}'"
        )

    # Validate source entity exists
    if graph.get_entity(from_type, from_id) is None:
        raise DataValidationError(f"entity {from_type}:{from_id} not found")

    # Validate target entity exists
    if graph.get_entity(to_type, to_id) is None:
        raise DataValidationError(f"entity {to_type}:{to_id} not found")

    existing_rel = graph.get_relationship(from_type, from_id, to_type, to_id, relationship)
    is_update = existing_rel is not None
    validation_source = dict(existing_rel.properties) if existing_rel is not None else {}
    validation_source.update(props)
    validation = validate_property_payload(
        config,
        rel_schema.properties,
        validation_source,
        require_required=True,
        strip_system_properties=True,
    )
    if validation.errors:
        raise DataValidationError(
            f"Relationship '{relationship}' property validation failed",
            errors=validation.errors,
        )

    rel = RelationshipInstance(
        relationship_type=relationship,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        properties=validation.properties,
    )
    return ValidatedRelationship(relationship=rel, is_update=is_update)


def apply_entity(graph: EntityGraph, validated: ValidatedEntity) -> None:
    """Apply a validated entity to the graph (add or update)."""
    if validated.is_update:
        graph.update_entity_properties(
            validated.entity.entity_type,
            validated.entity.entity_id,
            dict(validated.entity.properties),
        )
    else:
        graph.add_entity(validated.entity)


def _initial_assertion_state(source: str) -> RelationshipAssertionState:
    if source == "group_resolve":
        return RelationshipAssertionState(
            review=RelationshipReviewState(status="approved", source="group")
        )
    return RelationshipAssertionState()


def _set_assertion_properties(
    properties: dict[str, Any],
    state: RelationshipAssertionState,
) -> None:
    properties[ASSERTION_PROPERTY] = dump_assertion_state(state)
    legacy_review_status = review_state_to_legacy_review_status(state.review)
    if legacy_review_status is not None:
        properties[LEGACY_REVIEW_STATUS_PROPERTY] = legacy_review_status


def apply_relationship(
    graph: EntityGraph,
    validated: ValidatedRelationship,
    source: str,
    source_ref: str,
) -> None:
    """Apply a validated relationship to the graph (add or update).

    New edges get provenance stamped via make_provenance(source, source_ref)
    and a default assertion state. Updated edges preserve existing assertion
    state and provenance with last_modified_at/last_modified_by.
    """
    rel = validated.relationship
    if validated.is_update:
        existing_rel = graph.get_relationship(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            rel.relationship_type,
        )
        replace_props = dict(rel.properties)
        if existing_rel:
            old_provenance = existing_rel.properties.get("_provenance")
            provenance = load_provenance(old_provenance)
            if provenance is not None:
                replace_props["_provenance"] = dump_provenance(
                    stamp_provenance_modified(provenance, source)
                )

            assertion = load_assertion_state(existing_rel.properties)
            _set_assertion_properties(replace_props, assertion)
        graph.replace_edge_properties(
            rel.from_type,
            rel.from_id,
            rel.to_type,
            rel.to_id,
            rel.relationship_type,
            replace_props,
        )
    else:
        rel.properties["_provenance"] = dump_provenance(make_provenance(source, source_ref))
        _set_assertion_properties(rel.properties, _initial_assertion_state(source))
        graph.add_relationship(rel)
