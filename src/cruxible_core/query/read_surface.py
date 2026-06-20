"""Shared internal read operations for graph-backed service and workflow reads."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    entity_with_identity_properties,
)
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import (
    ConfigError,
    EntityTypeNotFoundError,
    RelationshipAmbiguityError,
    RelationshipNotFoundError,
)
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.query.engine import execute_query
from cruxible_core.query.enums import QueryRelationshipState
from cruxible_core.query.types import QueryResult


@dataclass
class ReadInspectNeighbor:
    direction: str
    relationship_type: str
    edge_key: int | None
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    entity: EntityInstance | None = None


@dataclass
class ReadInspectEntity:
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    neighbors: list[ReadInspectNeighbor] = field(default_factory=list)
    total_neighbors: int = 0


@dataclass
class ReadStatsResult:
    entity_count: int
    edge_count: int
    entity_counts: dict[str, int] = field(default_factory=dict)
    relationship_counts: dict[str, int] = field(default_factory=dict)
    head_snapshot_id: str | None = None


def run_query(
    config: CoreConfig,
    graph: EntityGraph,
    query_name: str,
    params: dict[str, Any],
    *,
    relationship_state: QueryRelationshipState | None = None,
) -> QueryResult:
    """Execute a named query against graph state without persistence side effects."""
    return execute_query(
        config,
        graph,
        query_name,
        params,
        relationship_state=relationship_state,
    )


def _paginate(items: list[Any], *, limit: int | None, offset: int) -> list[Any]:
    end = None if limit is None else offset + limit
    return items[offset:end]


def relationship_sort_key(edge: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    return (
        str(edge.get("relationship_type")),
        str(edge.get("from_type")),
        str(edge.get("from_id")),
        str(edge.get("to_type")),
        str(edge.get("to_id")),
        str(edge.get("edge_key")),
        json.dumps(edge.get("properties") or {}, sort_keys=True, default=str),
    )


def _known_entity_types(config: CoreConfig) -> list[str]:
    return sorted(config.entity_types)


def _require_entity_type(config: CoreConfig, entity_type: str) -> None:
    if entity_type not in config.entity_types:
        raise EntityTypeNotFoundError(
            entity_type,
            known_entity_types=_known_entity_types(config),
        )


def _require_relationship_type(config: CoreConfig, relationship_type: str) -> None:
    if config.get_relationship(relationship_type) is None:
        raise RelationshipNotFoundError(relationship_type)


def get_entity(
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    *,
    config: CoreConfig | None = None,
) -> EntityInstance | None:
    """Look up a specific entity by type and ID."""
    if config is not None:
        _require_entity_type(config, entity_type)
    entity = graph.get_entity(entity_type, entity_id)
    if entity is None or config is None:
        return entity
    return entity_with_identity_properties(config, entity)


def get_relationship(
    graph: EntityGraph,
    *,
    config: CoreConfig | None = None,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> RelationshipInstance | None:
    """Look up a specific relationship by endpoints and type."""
    if config is not None:
        _require_entity_type(config, from_type)
        _require_entity_type(config, to_type)
        _require_relationship_type(config, relationship_type)
    if edge_key is None:
        count = graph.relationship_count_between(
            from_type, from_id, to_type, to_id, relationship_type
        )
        if count > 1:
            raise RelationshipAmbiguityError(
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                relationship_type=relationship_type,
            )

    return graph.get_relationship(
        from_type,
        from_id,
        to_type,
        to_id,
        relationship_type,
        edge_key=edge_key,
    )


def inspect_entity(
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    *,
    config: CoreConfig | None = None,
    direction: Literal["incoming", "outgoing", "both"] = "both",
    relationship_type: str | None = None,
    limit: int | None = None,
) -> ReadInspectEntity:
    """Look up an entity and its immediate neighbors."""
    if config is not None:
        _require_entity_type(config, entity_type)
        if relationship_type is not None:
            _require_relationship_type(config, relationship_type)
    entity = graph.get_entity(entity_type, entity_id)
    if entity is None:
        return ReadInspectEntity(found=False, entity_type=entity_type, entity_id=entity_id)

    neighbor_rows = graph.get_neighbor_relationships(
        entity_type,
        entity_id,
        relationship_type=relationship_type,
        direction=direction,
    )
    total_neighbors = len(neighbor_rows)
    if limit is not None:
        neighbor_rows = neighbor_rows[:limit]
    neighbors = [
        ReadInspectNeighbor(
            direction=row["direction"],
            relationship_type=str(row["relationship_type"]),
            edge_key=row.get("edge_key"),
            properties=dict(row.get("properties", {})),
            metadata=dict(row.get("metadata", {})),
            entity=(
                entity_with_identity_properties(config, row["entity"])
                if config is not None
                else row["entity"]
            ),
        )
        for row in neighbor_rows
    ]
    entity_props = (
        entity_properties_with_identity(
            config,
            entity.entity_type,
            entity.entity_id,
            entity.properties,
        )
        if config is not None
        else dict(entity.properties)
    )
    return ReadInspectEntity(
        found=True,
        entity_type=entity.entity_type,
        entity_id=entity.entity_id,
        properties=entity_props,
        metadata=dict(entity.metadata),
        neighbors=neighbors,
        total_neighbors=total_neighbors,
    )


def sample_entities(
    graph: EntityGraph,
    entity_type: str,
    *,
    config: CoreConfig | None = None,
    fields: list[str] | None = None,
    limit: int = 5,
) -> list[EntityInstance]:
    """Sample entities of a given type."""
    if config is not None:
        _require_entity_type(config, entity_type)
        validate_entity_projection_fields(config, entity_type, fields)
        entities = [
            entity_with_identity_properties(config, entity)
            for entity in graph.list_entities(entity_type)
        ]
    else:
        entities = graph.list_entities(entity_type)
    entities = sorted(entities, key=lambda entity: (entity.entity_type, entity.entity_id))
    items = cast(list[EntityInstance], _paginate(entities, limit=limit, offset=0))
    if config is not None and fields is not None:
        items = [project_entity_fields(config, entity, fields) for entity in items]
    return items


_IDENTITY_PROJECTION_FIELDS = {"id", "entity_id", "type", "entity_type"}


def validate_entity_projection_fields(
    config: CoreConfig,
    entity_type: str,
    fields: list[str] | None,
) -> None:
    if fields is None:
        return
    entity_schema = config.get_entity_type(entity_type)
    if entity_schema is None:
        _require_entity_type(config, entity_type)
        return
    known_fields = set(entity_schema.properties) | _IDENTITY_PROJECTION_FIELDS
    unknown_fields = sorted(set(fields) - known_fields)
    if unknown_fields:
        field_list = ", ".join(unknown_fields)
        known = ", ".join(sorted(known_fields))
        raise ConfigError(
            f"Unknown field(s) for entity type '{entity_type}': {field_list}. "
            f"Known fields: {known}"
        )


def project_entity_fields(
    config: CoreConfig,
    entity: EntityInstance,
    fields: list[str],
) -> EntityInstance:
    entity_schema = config.get_entity_type(entity.entity_type)
    if entity_schema is None:
        return entity
    property_fields = [field for field in fields if field not in _IDENTITY_PROJECTION_FIELDS]
    projected = {
        field: entity.properties[field]
        for field in property_fields
        if field in entity.properties
    }
    return entity.model_copy(update={"properties": projected})


def graph_stats(
    graph: EntityGraph,
    *,
    head_snapshot_id: str | None = None,
) -> ReadStatsResult:
    """Return graph counts grouped by entity and relationship type."""
    entity_counts = {
        entity_type: graph.entity_count(entity_type) for entity_type in graph.list_entity_types()
    }
    relationship_counts = {
        relationship_type: graph.edge_count(relationship_type)
        for relationship_type in graph.list_relationship_types()
    }
    return ReadStatsResult(
        entity_count=graph.entity_count(),
        edge_count=graph.edge_count(),
        entity_counts=entity_counts,
        relationship_counts=relationship_counts,
        head_snapshot_id=head_snapshot_id,
    )


__all__ = [
    "graph_stats",
    "get_entity",
    "get_relationship",
    "inspect_entity",
    "project_entity_fields",
    "ReadInspectEntity",
    "ReadInspectNeighbor",
    "ReadStatsResult",
    "relationship_sort_key",
    "run_query",
    "sample_entities",
    "validate_entity_projection_fields",
]
