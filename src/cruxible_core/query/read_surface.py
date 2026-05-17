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
from cruxible_core.errors import RelationshipAmbiguityError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.query.engine import QueryResult, execute_query


@dataclass
class ReadListResult:
    items: list[Any] = field(default_factory=list)
    total: int = 0


@dataclass
class ReadInspectNeighbor:
    direction: str
    relationship_type: str
    edge_key: int | None
    properties: dict[str, Any] = field(default_factory=dict)
    entity: EntityInstance | None = None


@dataclass
class ReadInspectEntity:
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)
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
) -> QueryResult:
    """Execute a named query against graph state without persistence side effects."""
    return execute_query(config, graph, query_name, params)


def list_entities(
    graph: EntityGraph,
    entity_type: str,
    *,
    config: CoreConfig | None = None,
    property_filter: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ReadListResult:
    """List entities of a type with shared limit/filter semantics."""
    if config is None:
        entities = graph.list_entities(entity_type, property_filter=property_filter)
    else:
        entities = [
            entity_with_identity_properties(config, entity)
            for entity in graph.list_entities(entity_type)
        ]
        if property_filter:
            entities = [
                entity
                for entity in entities
                if all(
                    entity.properties.get(key) == value
                    for key, value in property_filter.items()
                )
            ]
    entities = sorted(entities, key=lambda entity: (entity.entity_type, entity.entity_id))
    items = entities[:limit] if limit is not None else entities
    return ReadListResult(items=items, total=len(entities))


def list_relationships(
    graph: EntityGraph,
    *,
    relationship_type: str | None = None,
    property_filter: dict[str, Any] | None = None,
    limit: int | None = None,
) -> ReadListResult:
    """List relationships with shared limit/filter semantics."""
    relationships = graph.list_edges(relationship_type=relationship_type)
    if property_filter:
        relationships = [
            edge
            for edge in relationships
            if all(edge["properties"].get(key) == value for key, value in property_filter.items())
        ]
    relationships = sorted(relationships, key=_relationship_sort_key)
    items = relationships[:limit] if limit is not None else relationships
    return ReadListResult(items=items, total=len(relationships))


def _relationship_sort_key(edge: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    return (
        str(edge.get("relationship_type")),
        str(edge.get("from_type")),
        str(edge.get("from_id")),
        str(edge.get("to_type")),
        str(edge.get("to_id")),
        str(edge.get("edge_key")),
        json.dumps(edge.get("properties") or {}, sort_keys=True, default=str),
    )


def get_entity(
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    *,
    config: CoreConfig | None = None,
) -> EntityInstance | None:
    """Look up a specific entity by type and ID."""
    entity = graph.get_entity(entity_type, entity_id)
    if entity is None or config is None:
        return entity
    return entity_with_identity_properties(config, entity)


def get_relationship(
    graph: EntityGraph,
    *,
    from_type: str,
    from_id: str,
    relationship_type: str,
    to_type: str,
    to_id: str,
    edge_key: int | None = None,
) -> RelationshipInstance | None:
    """Look up a specific relationship by endpoints and type."""
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
        neighbors=neighbors,
        total_neighbors=total_neighbors,
    )


def sample_entities(
    graph: EntityGraph,
    entity_type: str,
    *,
    config: CoreConfig | None = None,
    limit: int = 5,
) -> list[EntityInstance]:
    """Sample entities of a given type."""
    return cast(
        list[EntityInstance],
        list_entities(graph, entity_type, config=config, limit=limit).items,
    )


def graph_stats(
    graph: EntityGraph,
    *,
    head_snapshot_id: str | None = None,
) -> ReadStatsResult:
    """Return graph counts grouped by entity and relationship type."""
    entity_counts = {
        entity_type: graph.entity_count(entity_type)
        for entity_type in graph.list_entity_types()
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
    "list_entities",
    "list_relationships",
    "ReadInspectEntity",
    "ReadInspectNeighbor",
    "ReadListResult",
    "ReadStatsResult",
    "run_query",
    "sample_entities",
]
