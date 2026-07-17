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
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.query.types import QueryResult

# Bounded-neighborhood budgets: defaults keep an unadorned expanded read
# agent-sized; hard caps bound the worst case a caller can request.
NEIGHBORHOOD_MAX_DEPTH = 4
NEIGHBORHOOD_DEFAULT_MAX_NODES = 100
NEIGHBORHOOD_MAX_NODES = 500
NEIGHBORHOOD_DEFAULT_MAX_EDGES = 200
NEIGHBORHOOD_MAX_EDGES = 1000

_NEIGHBORHOOD_STATES: tuple[QueryVisibilityState, ...] = (
    "live",
    "accepted",
    "all",
    "not-live",
    "pending",
    "reviewable",
)


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
class ReadNeighborhoodNode:
    entity: EntityInstance
    depth: int


@dataclass
class ReadNeighborhoodEdge:
    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReadInspectNeighborhood:
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    depth: int = 1
    state: QueryVisibilityState = "live"
    nodes: list[ReadNeighborhoodNode] = field(default_factory=list)
    edges: list[ReadNeighborhoodEdge] = field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    nodes_returned: int = 0
    edges_returned: int = 0


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
    relationship_state: QueryVisibilityState | None = None,
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
        metadata=entity.metadata.to_metadata_dict(),
        neighbors=neighbors,
        total_neighbors=total_neighbors,
    )


def neighborhood_requested(
    *,
    depth: int | None = None,
    relationship_types: list[str] | None = None,
    target_types: list[str] | None = None,
    state: str | None = None,
    projection: list[str] | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> bool:
    """Whether an inspect call opted into the expanded neighborhood shape.

    Any explicitly provided neighborhood parameter opts in — including an
    explicit ``depth=1`` (the documented way to get a single-hop read with
    visible truncation). Calls providing none of them keep the legacy
    single-hop ``neighbors`` shape bit-for-bit.
    """
    return (
        depth is not None
        or state is not None
        or max_nodes is not None
        or max_edges is not None
        or bool(relationship_types)
        or bool(target_types)
        or bool(projection)
    )


def validate_neighborhood_projection(
    config: CoreConfig | None,
    projection: list[str] | None,
) -> None:
    """Reject projection names unknown to EVERY configured entity type.

    Neighborhood nodes span entity types, so a name only needs to exist on
    one type (nodes of other types simply omit it); a name known to no type
    is a typo and fails loudly.
    """
    if config is None or not projection:
        return
    known: set[str] = set(_IDENTITY_PROJECTION_FIELDS)
    for entity_type in config.entity_types:
        entity_schema = config.get_entity_type(entity_type)
        if entity_schema is not None:
            known.update(entity_schema.properties)
    unknown = sorted(set(projection) - known)
    if unknown:
        raise ConfigError(
            "Unknown projection propert{} {}: not defined on any entity type".format(
                "ies" if len(unknown) > 1 else "y",
                ", ".join(unknown),
            )
        )


def inspect_neighborhood(
    graph: EntityGraph,
    entity_type: str,
    entity_id: str,
    *,
    config: CoreConfig | None = None,
    depth: int | None = None,
    direction: Literal["incoming", "outgoing", "both"] = "both",
    relationship_type: str | None = None,
    relationship_types: list[str] | None = None,
    target_types: list[str] | None = None,
    state: QueryVisibilityState | None = None,
    limit: int | None = None,
    max_nodes: int | None = None,
    max_edges: int | None = None,
) -> ReadInspectNeighborhood:
    """Bounded, deterministic neighborhood read around one root entity.

    Parameter semantics:

    * ``depth`` defaults to 1, hard max ``NEIGHBORHOOD_MAX_DEPTH``.
    * ``relationship_type`` (legacy single filter) and ``relationship_types``
      compose as a UNION when both are given.
    * ``state`` gates edges through the query engine's
      ``relationship_matches_query_state`` — visibility is bit-identical to
      named-query traversal. Default ``live`` (unlike the legacy single-hop
      read, which shows every stored edge).
    * ``limit`` (the legacy single-hop cap) maps to ``max_nodes`` when
      ``max_nodes`` is not given, so the previously silent cap now reports
      ``truncated`` with reason ``node_budget``.
    """
    resolved_depth = 1 if depth is None else depth
    if not 1 <= resolved_depth <= NEIGHBORHOOD_MAX_DEPTH:
        raise ConfigError(
            f"depth must be between 1 and {NEIGHBORHOOD_MAX_DEPTH}, got {resolved_depth}"
        )
    resolved_max_nodes = max_nodes if max_nodes is not None else limit
    if resolved_max_nodes is None:
        resolved_max_nodes = NEIGHBORHOOD_DEFAULT_MAX_NODES
    if not 1 <= resolved_max_nodes <= NEIGHBORHOOD_MAX_NODES:
        raise ConfigError(
            f"max_nodes must be between 1 and {NEIGHBORHOOD_MAX_NODES}, got {resolved_max_nodes}"
        )
    resolved_max_edges = max_edges if max_edges is not None else NEIGHBORHOOD_DEFAULT_MAX_EDGES
    if not 1 <= resolved_max_edges <= NEIGHBORHOOD_MAX_EDGES:
        raise ConfigError(
            f"max_edges must be between 1 and {NEIGHBORHOOD_MAX_EDGES}, got {resolved_max_edges}"
        )
    resolved_state: QueryVisibilityState = state if state is not None else "live"
    if resolved_state not in _NEIGHBORHOOD_STATES:
        raise ConfigError(
            f"state must be one of {', '.join(_NEIGHBORHOOD_STATES)}; got '{resolved_state}'"
        )

    # Legacy single filter + repeatable filter compose as a union.
    rel_filter = sorted(
        set(relationship_types or []) | ({relationship_type} if relationship_type else set())
    )
    if config is not None:
        _require_entity_type(config, entity_type)
        for name in rel_filter:
            _require_relationship_type(config, name)
        for name in target_types or []:
            _require_entity_type(config, name)

    entity = graph.get_entity(entity_type, entity_id)
    if entity is None:
        return ReadInspectNeighborhood(
            found=False,
            entity_type=entity_type,
            entity_id=entity_id,
            depth=resolved_depth,
            state=resolved_state,
        )

    expansion = graph.expand_neighborhood(
        entity_type,
        entity_id,
        depth=resolved_depth,
        direction=direction,
        relationship_types=rel_filter or None,
        target_types=sorted(set(target_types)) if target_types else None,
        edge_visible=lambda metadata: relationship_matches_query_state(metadata, resolved_state),
        max_nodes=resolved_max_nodes,
        max_edges=resolved_max_edges,
    )
    nodes = [
        ReadNeighborhoodNode(
            entity=(
                entity_with_identity_properties(config, node_entity)
                if config is not None
                else node_entity
            ),
            depth=node_depth,
        )
        for node_entity, node_depth in expansion.nodes
    ]
    edges = [
        ReadNeighborhoodEdge(
            relationship_type=str(edge["relationship_type"]),
            from_type=edge["from_type"],
            from_id=edge["from_id"],
            to_type=edge["to_type"],
            to_id=edge["to_id"],
            edge_key=edge.get("edge_key"),
            properties=dict(edge.get("properties", {})),
            metadata=dict(edge.get("metadata", {})),
        )
        for edge in expansion.edges
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
    return ReadInspectNeighborhood(
        found=True,
        entity_type=entity.entity_type,
        entity_id=entity.entity_id,
        properties=entity_props,
        metadata=entity.metadata.to_metadata_dict(),
        depth=resolved_depth,
        state=resolved_state,
        nodes=nodes,
        edges=edges,
        truncated=expansion.truncated,
        truncation_reasons=list(expansion.truncation_reasons),
        nodes_returned=len(nodes),
        edges_returned=len(edges),
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
            f"Unknown field(s) for entity type '{entity_type}': {field_list}. Known fields: {known}"
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
        field: entity.properties[field] for field in property_fields if field in entity.properties
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
    "NEIGHBORHOOD_DEFAULT_MAX_EDGES",
    "NEIGHBORHOOD_DEFAULT_MAX_NODES",
    "NEIGHBORHOOD_MAX_DEPTH",
    "NEIGHBORHOOD_MAX_EDGES",
    "NEIGHBORHOOD_MAX_NODES",
    "graph_stats",
    "get_entity",
    "get_relationship",
    "inspect_entity",
    "inspect_neighborhood",
    "neighborhood_requested",
    "project_entity_fields",
    "ReadInspectEntity",
    "ReadInspectNeighbor",
    "ReadInspectNeighborhood",
    "ReadNeighborhoodEdge",
    "ReadNeighborhoodNode",
    "ReadStatsResult",
    "relationship_sort_key",
    "run_query",
    "sample_entities",
    "validate_entity_projection_fields",
    "validate_neighborhood_projection",
]
