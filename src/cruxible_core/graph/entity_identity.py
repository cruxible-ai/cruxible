"""Deterministic config-declared entity identity checks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from cruxible_core.config.property_validation import entity_properties_with_identity
from cruxible_core.config.schema import CoreConfig, EntityTypeSchema
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance


@dataclass(frozen=True)
class SimilarExistingEntity:
    """The existing same-type entity matched by an advisory identity hint."""

    entity_id: str
    matched_properties: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "matched_properties": list(self.matched_properties),
        }


@dataclass(frozen=True)
class EntityIdentityWarning:
    """Structured advisory warning returned by direct entity-write surfaces."""

    entity_type: str
    entity_id: str
    similar_existing_entity: SimilarExistingEntity

    def to_payload(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "similar_existing_entity": self.similar_existing_entity.to_payload(),
        }


def normalize_identity_value(value: str) -> str:
    """Return the shared normalized form used by declared identity keys.

    Matching is case-insensitive, ignores leading/trailing and repeated
    whitespace, and removes every Unicode punctuation code point. Symbols and
    letters remain significant.
    """
    if not isinstance(value, str):
        raise TypeError("identity values must be strings")
    normalized = unicodedata.normalize("NFC", value)
    without_punctuation = "".join(
        character
        for character in normalized.casefold().strip()
        if not unicodedata.category(character).startswith("P")
    )
    return " ".join(without_punctuation.split())


def _identity_key(
    properties: Mapping[str, Any],
    property_names: Sequence[str],
) -> tuple[str, ...] | None:
    values: list[str] = []
    for property_name in property_names:
        value = properties.get(property_name)
        if not isinstance(value, str):
            return None
        values.append(normalize_identity_value(value))
    if not any(values):
        return None
    return tuple(values)


def _candidate_properties(
    config: CoreConfig,
    graph: EntityGraph,
    entity: EntityInstance,
    *,
    is_update: bool,
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if is_update:
        existing = graph.get_entity(entity.entity_type, entity.entity_id)
        if existing is not None:
            properties.update(existing.properties)
    properties.update(entity.properties)
    return entity_properties_with_identity(
        config,
        entity.entity_type,
        entity.entity_id,
        properties,
    )


def _update_changes_unique_key(
    config: CoreConfig,
    graph: EntityGraph,
    entity: EntityInstance,
    property_names: Sequence[str],
) -> bool:
    existing = graph.get_entity(entity.entity_type, entity.entity_id)
    if existing is None:
        return True
    existing_properties = entity_properties_with_identity(
        config,
        entity.entity_type,
        entity.entity_id,
        existing.properties,
    )
    candidate_properties = _candidate_properties(config, graph, entity, is_update=True)
    return _identity_key(existing_properties, property_names) != _identity_key(
        candidate_properties,
        property_names,
    )


def _matching_existing_entity(
    config: CoreConfig,
    graph: EntityGraph,
    entity: EntityInstance,
    property_names: Sequence[str],
    *,
    is_update: bool,
) -> EntityInstance | None:
    candidate_key = _identity_key(
        _candidate_properties(config, graph, entity, is_update=is_update),
        property_names,
    )
    if candidate_key is None:
        return None

    # Deliberately O(n) per write over existing entities of this type. Identity
    # declarations add no storage or indexes in 0.3.1; sorting makes the signal
    # deterministic even if a pre-feature graph already contains duplicates.
    existing_entities = sorted(
        graph.list_entities(entity.entity_type),
        key=lambda existing: existing.entity_id,
    )
    for existing in existing_entities:
        if existing.entity_id == entity.entity_id:
            continue
        existing_properties = entity_properties_with_identity(
            config,
            existing.entity_type,
            existing.entity_id,
            existing.properties,
        )
        if _identity_key(existing_properties, property_names) == candidate_key:
            return existing
    return None


def check_declared_entity_identity(
    config: CoreConfig,
    graph: EntityGraph,
    entity: EntityInstance,
    *,
    is_update: bool,
) -> EntityIdentityWarning | None:
    """Enforce hard identity declarations and return a create-only advisory."""
    schema: EntityTypeSchema = config.entity_types[entity.entity_type]

    if schema.id_pattern is not None and re.fullmatch(schema.id_pattern, entity.entity_id) is None:
        raise DataValidationError(
            f"Entity id '{entity.entity_id}' for type '{entity.entity_type}' does not match "
            f"id_pattern {schema.id_pattern!r}"
        )

    if schema.unique_by and (
        not is_update
        or _update_changes_unique_key(
            config,
            graph,
            entity,
            schema.unique_by,
        )
    ):
        existing = _matching_existing_entity(
            config,
            graph,
            entity,
            schema.unique_by,
            is_update=is_update,
        )
        if existing is not None:
            matched = ", ".join(schema.unique_by)
            raise DataValidationError(
                f"Entity '{entity.entity_type}:{entity.entity_id}' violates unique_by "
                f"[{matched}]: normalized identity matches existing entity_id "
                f"'{existing.entity_id}'; reuse that entity_id or change the declared "
                "identity properties"
            )

    # Advisory hints intentionally run only for creates. Updates keep the hard
    # unique_by guarantee above, without adding warning scans to routine edits.
    if is_update or not schema.identity_hint:
        return None
    existing = _matching_existing_entity(
        config,
        graph,
        entity,
        schema.identity_hint,
        is_update=False,
    )
    if existing is None:
        return None
    return EntityIdentityWarning(
        entity_type=entity.entity_type,
        entity_id=entity.entity_id,
        similar_existing_entity=SimilarExistingEntity(
            entity_id=existing.entity_id,
            matched_properties=list(schema.identity_hint),
        ),
    )
