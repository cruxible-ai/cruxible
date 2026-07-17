"""In-memory entity graph using networkx.

Uses networkx.MultiDiGraph for storage:
- Nodes store EntityInstance data
- Edges store RelationshipInstance data with unique integer keys
- Supports multiple edges of the same type between nodes

Node ID format: "{entity_type}:{entity_id}" (e.g., "Vehicle:V-2024-CIVIC-EX")
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import networkx as nx

from cruxible_core.graph.provenance import (
    CLONE_ORIGIN_UPSTREAM_SNAPSHOT,
    relabel_provenance_for_clone,
)
from cruxible_core.graph.types import (
    EntityInstance,
    EntityMetadata,
    RelationshipInstance,
    RelationshipMetadata,
    make_node_id,
    relationship_is_live,
    split_node_id,
)


def _metadata_dict(metadata: RelationshipMetadata) -> dict[str, Any]:
    return metadata.model_dump(mode="json", exclude_none=True)


def _entity_metadata_dict(metadata: EntityMetadata) -> dict[str, Any]:
    """Encode the typed entity metadata envelope to its flat stored dict."""
    return metadata.to_metadata_dict()


def _relationship_metadata(edge_data: dict[str, Any]) -> RelationshipMetadata:
    return RelationshipMetadata.model_validate(edge_data.get("metadata") or {})


# Canonical truncation-reason order for bounded neighborhood expansion. The
# result list is always a subsequence of this tuple, so callers (and pinned
# payloads) see a deterministic reason order regardless of trip order.
NEIGHBORHOOD_TRUNCATION_REASONS: tuple[str, ...] = ("node_budget", "edge_budget", "depth")


@dataclass
class NeighborhoodExpansion:
    """Bounded BFS expansion result around one root entity.

    ``nodes`` holds ``(entity, depth)`` pairs for every returned NON-root
    entity, sorted by ``(depth, entity_type, entity_id)``. ``edges`` holds
    serialized edge dicts (same shape as ``get_neighbor_relationships`` rows
    plus explicit endpoint refs), sorted by ``(relationship_type, from_type,
    from_id, to_type, to_id, edge_key)``. Budgets count RETURNED items, not
    visited ones: neighbors dropped by ``target_types`` never consume budget.

    ``hidden_edge_count`` counts distinct edges excluded solely by the
    ``edge_visible`` gate at nodes the BFS actually expanded: they passed the
    direction/relationship/target filters but the visibility predicate said
    no. Hidden edges consume no budget and are never walked, so the count
    covers the explored frontier only — never regions reachable exclusively
    through hidden edges.
    """

    nodes: list[tuple[EntityInstance, int]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    hidden_edge_count: int = 0


class EntityGraph:
    """In-memory graph of entity instances and relationships."""

    def __init__(self) -> None:
        self._graph: nx.MultiDiGraph[str] = nx.MultiDiGraph()
        self._entities_by_type: dict[str, set[str]] = defaultdict(set)
        self._edge_counter: count[int] = count()

    def clear(self) -> None:
        """Clear all entities and relationships from the graph."""
        self._graph.clear()
        self._entities_by_type.clear()
        self._edge_counter = count()

    # -------------------------------------------------------------------------
    # Entity Operations
    # -------------------------------------------------------------------------

    def add_entity(self, entity: EntityInstance) -> None:
        """Add an entity to the graph. Updates if entity with same ID exists."""
        node_id = entity.node_id()
        self._graph.add_node(
            node_id,
            entity_type=entity.entity_type,
            entity_id=entity.entity_id,
            properties=entity.properties,
            # The in-memory node stores the flat encoded metadata dict, mirroring how
            # edges store ``_metadata_dict(rel.metadata)``; the typed envelope is
            # rehydrated at the ``EntityInstance`` boundary on read.
            metadata=_entity_metadata_dict(entity.metadata),
        )
        self._entities_by_type[entity.entity_type].add(node_id)

    def get_entity(self, entity_type: str, entity_id: str) -> EntityInstance | None:
        """Get an entity by type and ID. Returns None if not found."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return None

        node_data = self._graph.nodes[node_id]
        return EntityInstance(
            entity_type=node_data["entity_type"],
            entity_id=node_data["entity_id"],
            properties=node_data.get("properties", {}),
            metadata=node_data.get("metadata", {}),
        )

    def has_entity(self, entity_type: str, entity_id: str) -> bool:
        """Check if an entity exists in the graph."""
        return make_node_id(entity_type, entity_id) in self._graph

    def list_entities(
        self,
        entity_type: str,
        property_filter: dict[str, Any] | None = None,
    ) -> list[EntityInstance]:
        """List all entities of a given type, optionally filtered by properties.

        When ``property_filter`` is provided, only entities whose properties
        match **all** filter key-value pairs (exact equality, AND semantics)
        are returned.
        """
        node_ids = self._entities_by_type.get(entity_type, set())
        entities = []
        for node_id in node_ids:
            if node_id in self._graph:
                node_data = self._graph.nodes[node_id]
                if property_filter:
                    props = node_data.get("properties", {})
                    if not all(props.get(k) == v for k, v in property_filter.items()):
                        continue
                entities.append(
                    EntityInstance(
                        entity_type=node_data["entity_type"],
                        entity_id=node_data["entity_id"],
                        properties=node_data.get("properties", {}),
                        metadata=node_data.get("metadata", {}),
                    )
                )
        return entities

    def remove_entity(self, entity_type: str, entity_id: str) -> None:
        """Remove an entity and all its relationships."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id in self._graph:
            self._graph.remove_node(node_id)
            self._entities_by_type[entity_type].discard(node_id)

    def iter_all_entities(self) -> Iterator[EntityInstance]:
        """Yield every EntityInstance in the graph, across all types."""
        for node_ids in self._entities_by_type.values():
            for node_id in node_ids:
                if node_id not in self._graph:
                    continue
                data = self._graph.nodes[node_id]
                yield EntityInstance(
                    entity_type=data["entity_type"],
                    entity_id=data["entity_id"],
                    properties=data.get("properties", {}),
                    metadata=data.get("metadata", {}),
                )

    def is_isolated(self, entity_type: str, entity_id: str) -> bool:
        """Check whether an entity has zero edges.

        Returns True if the entity does not exist in the graph.
        """
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return True
        return bool(self._graph.degree(node_id) == 0)

    def neighbor_ids(self, entity_type: str, entity_id: str) -> set[str]:
        """Get all neighbor node-ID strings (both directions, all relationship types).

        Returns an empty set if the entity does not exist in the graph.
        """
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return set()
        result: set[str] = set()
        for _, dest in self._graph.out_edges(node_id):
            result.add(dest)
        for src, _ in self._graph.in_edges(node_id):
            result.add(src)
        return result

    # -------------------------------------------------------------------------
    # Relationship Operations
    # -------------------------------------------------------------------------

    def _find_relationship_edge(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
        edge_key: int | None = None,
    ) -> tuple[Any, dict[str, Any]] | None:
        """Return the matching graph edge key and data dict, if present."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)
        edge_dict = self._graph.get_edge_data(from_node, to_node)
        if not edge_dict:
            return None

        for key, edge_data in edge_dict.items():
            if edge_key is not None and key != edge_key:
                continue
            if edge_data.get("relationship_type") == relationship_type:
                return key, edge_data
        return None

    def add_relationship(self, rel: RelationshipInstance) -> None:
        """Add a relationship to the graph. Creates stub entities if needed."""
        from_node = rel.from_node_id()
        to_node = rel.to_node_id()

        if from_node not in self._graph:
            self._graph.add_node(
                from_node,
                entity_type=rel.from_type,
                entity_id=rel.from_id,
                properties={},
                metadata={},
            )
            self._entities_by_type[rel.from_type].add(from_node)

        if to_node not in self._graph:
            self._graph.add_node(
                to_node,
                entity_type=rel.to_type,
                entity_id=rel.to_id,
                properties={},
                metadata={},
            )
            self._entities_by_type[rel.to_type].add(to_node)

        edge_key = next(self._edge_counter)
        self._graph.add_edge(
            from_node,
            to_node,
            key=edge_key,
            relationship_type=rel.relationship_type,
            properties=rel.properties,
            metadata=_metadata_dict(rel.metadata),
        )

    def get_relationship(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
        edge_key: int | None = None,
    ) -> RelationshipInstance | None:
        """Get a specific relationship between two entities. Returns first match."""
        found = self._find_relationship_edge(
            from_type,
            from_id,
            to_type,
            to_id,
            relationship_type,
            edge_key=edge_key,
        )
        if found is None:
            return None
        key, edge_data = found
        return RelationshipInstance(
            relationship_type=relationship_type,
            from_type=from_type,
            from_id=from_id,
            to_type=to_type,
            to_id=to_id,
            edge_key=key if isinstance(key, int) else None,
            properties=edge_data.get("properties", {}),
            metadata=_relationship_metadata(edge_data),
        )

    def has_relationship(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ) -> bool:
        """Check if a specific relationship exists between two entities."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)
        edge_dict = self._graph.get_edge_data(from_node, to_node)
        if not edge_dict:
            return False
        return any(e.get("relationship_type") == relationship_type for e in edge_dict.values())

    def has_live_relationship(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ) -> bool:
        """Check whether a live relationship exists."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)
        edge_dict = self._graph.get_edge_data(from_node, to_node)
        if not edge_dict:
            return False
        for edge_data in edge_dict.values():
            if edge_data.get("relationship_type") != relationship_type:
                continue
            if relationship_is_live(_relationship_metadata(edge_data)):
                return True
        return False

    def update_relationship_state(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
        *,
        property_updates: dict[str, Any] | None = None,
        metadata: RelationshipMetadata | None = None,
        edge_key: int | None = None,
    ) -> bool:
        """Merge domain property updates and/or replace metadata on a relationship."""
        found = self._find_relationship_edge(
            from_type,
            from_id,
            to_type,
            to_id,
            relationship_type,
            edge_key=edge_key,
        )
        if found is None:
            return False

        _key, edge_data = found
        if property_updates is not None:
            edge_data.setdefault("properties", {}).update(property_updates)
        if metadata is not None:
            edge_data["metadata"] = _metadata_dict(metadata)
        return True

    def replace_relationship_state(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
        *,
        properties: dict[str, Any],
        metadata: RelationshipMetadata,
        edge_key: int | None = None,
    ) -> bool:
        """Replace domain properties and metadata on a relationship."""
        found = self._find_relationship_edge(
            from_type,
            from_id,
            to_type,
            to_id,
            relationship_type,
            edge_key=edge_key,
        )
        if found is None:
            return False

        _key, edge_data = found
        edge_data["properties"] = dict(properties)
        edge_data["metadata"] = _metadata_dict(metadata)
        return True

    def relabel_clone_receipts(
        self,
        *,
        origin: str = CLONE_ORIGIN_UPSTREAM_SNAPSHOT,
    ) -> int:
        """Clear dangling receipt correlation on every edge, stamping clone origin.

        Call this when materializing a graph from a snapshot/clone/state-pull
        bundle (graph+config+lock, no receipts). Every edge whose provenance still
        carries a ``receipt_id``/``resolution_id`` points at an artifact that does
        not exist in this instance; this nulls those ids and records ``origin`` on
        the provenance so no edge is left with a phantom receipt pointer. Edges
        that are already clean (legacy/null or previously relabeled) are untouched.

        Returns the number of edges relabeled.
        """
        relabeled = 0
        for _u, _v, _key, edge_data in self._graph.edges(keys=True, data=True):
            metadata = _relationship_metadata(edge_data)
            relabeled_provenance = relabel_provenance_for_clone(
                metadata.provenance,
                origin=origin,
            )
            if relabeled_provenance is metadata.provenance:
                continue
            metadata.provenance = relabeled_provenance
            edge_data["metadata"] = _metadata_dict(metadata)
            relabeled += 1
        return relabeled

    def update_entity_properties(
        self,
        entity_type: str,
        entity_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """Merge updates into an entity's properties. Returns True if found."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return False

        self._graph.nodes[node_id].setdefault("properties", {}).update(updates)
        return True

    def update_entity_metadata(
        self,
        entity_type: str,
        entity_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """Merge updates into an entity's metadata. Returns True if found."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return False

        self._graph.nodes[node_id].setdefault("metadata", {}).update(updates)
        return True

    def remove_relationship(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
        edge_key: int | None = None,
    ) -> bool:
        """Remove a specific relationship. Returns True if found and removed."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)
        found = self._find_relationship_edge(
            from_type,
            from_id,
            to_type,
            to_id,
            relationship_type,
            edge_key=edge_key,
        )
        if found is None:
            return False

        key, _edge_data = found
        self._graph.remove_edge(from_node, to_node, key=key)
        return True

    def relationship_count_between(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        relationship_type: str,
    ) -> int:
        """Count matching relationships between two entities for a relationship type."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)
        edge_dict = self._graph.get_edge_data(from_node, to_node)
        if not edge_dict:
            return 0
        return sum(
            1
            for edge_data in edge_dict.values()
            if edge_data.get("relationship_type") == relationship_type
        )

    # -------------------------------------------------------------------------
    # Traversal Operations
    # -------------------------------------------------------------------------

    def get_descendants(
        self,
        entity_type: str,
        entity_id: str,
        relationship_type: str | None = None,
        max_depth: int | None = None,
        edge_filter: Callable[[dict[str, Any]], bool] | None = None,
        bidirectional: bool = False,
    ) -> list[tuple[EntityInstance, int]]:
        """Get all descendants (transitive closure) via BFS with depth.

        Args:
            entity_type: Source entity type
            entity_id: Source entity ID
            relationship_type: Filter by relationship type (None for all)
            max_depth: Maximum traversal depth (None for unlimited)
            edge_filter: Callable on edge properties dict, return True to traverse
            bidirectional: If True, traverse both outgoing and incoming edges
        """
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return []

        descendants: list[tuple[EntityInstance, int]] = []
        visited: set[str] = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])

        while queue:
            current_id, depth = queue.popleft()

            if max_depth is not None and depth >= max_depth:
                continue

            edges_to_check: list[tuple[str, str, dict[str, Any]]] = []  # noqa: UP006
            for _, target, key, data in self._graph.out_edges(current_id, keys=True, data=True):
                edges_to_check.append((target, key, data))
            if bidirectional:
                for source, _, key, data in self._graph.in_edges(current_id, keys=True, data=True):
                    edges_to_check.append((source, key, data))

            for neighbor, _key, data in edges_to_check:
                if neighbor in visited:
                    continue
                if (
                    relationship_type is not None
                    and data.get("relationship_type") != relationship_type
                ):
                    continue
                if edge_filter is not None and not edge_filter(data.get("properties", {})):
                    continue

                visited.add(neighbor)
                queue.append((neighbor, depth + 1))

                if neighbor in self._graph:
                    node_data = self._graph.nodes[neighbor]
                    descendants.append(
                        (
                            EntityInstance(
                                entity_type=node_data["entity_type"],
                                entity_id=node_data["entity_id"],
                                properties=node_data.get("properties", {}),
                                metadata=node_data.get("metadata", {}),
                            ),
                            depth + 1,
                        )
                    )

        return descendants

    def get_ancestors(
        self,
        entity_type: str,
        entity_id: str,
        relationship_type: str,
        max_depth: int | None = None,
    ) -> list[tuple[EntityInstance, int]]:
        """Get all ancestors by walking UP incoming edges of a relationship.

        Follows incoming edges (parent → child direction, walking child → parent).
        """
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return []

        ancestors: list[tuple[EntityInstance, int]] = []
        visited: set[str] = {node_id}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])

        while queue:
            current_id, depth = queue.popleft()

            if max_depth is not None and depth >= max_depth:
                continue

            for source, _, _key, data in self._graph.in_edges(current_id, keys=True, data=True):
                if source in visited:
                    continue
                if data.get("relationship_type") != relationship_type:
                    continue

                visited.add(source)
                queue.append((source, depth + 1))

                if source in self._graph:
                    node_data = self._graph.nodes[source]
                    ancestors.append(
                        (
                            EntityInstance(
                                entity_type=node_data["entity_type"],
                                entity_id=node_data["entity_id"],
                                properties=node_data.get("properties", {}),
                                metadata=node_data.get("metadata", {}),
                            ),
                            depth + 1,
                        )
                    )

        return ancestors

    def find_path(
        self,
        from_type: str,
        from_id: str,
        to_type: str,
        to_id: str,
        max_depth: int = 10,
    ) -> list[EntityInstance] | None:
        """Find shortest path between two entities. Returns None if no path."""
        from_node = make_node_id(from_type, from_id)
        to_node = make_node_id(to_type, to_id)

        if from_node not in self._graph or to_node not in self._graph:
            return None

        try:
            path = nx.shortest_path(self._graph, from_node, to_node)
            if len(path) > max_depth + 1:
                return None

            return [
                EntityInstance(
                    entity_type=self._graph.nodes[nid]["entity_type"],
                    entity_id=self._graph.nodes[nid]["entity_id"],
                    properties=self._graph.nodes[nid].get("properties", {}),
                    metadata=self._graph.nodes[nid].get("metadata", {}),
                )
                for nid in path
            ]
        except nx.NetworkXNoPath:
            return None

    # -------------------------------------------------------------------------
    # Efficient Edge Iteration
    # -------------------------------------------------------------------------

    def _iter_edges_raw(
        self,
        relationship_type: str | None = None,
    ) -> Iterator[tuple[str, str, str, str, str, Any, dict[str, Any], RelationshipMetadata]]:
        """Low-level iterator yielding 7-tuples.

        Yields (from_type, from_id, to_type, to_id, rel_type, edge_key, properties, metadata).
        """
        for u, v, key, data in self._graph.edges(keys=True, data=True):
            rel_type = data.get("relationship_type")
            if not isinstance(rel_type, str) or not rel_type:
                raise ValueError(
                    f"Graph edge {u!r} -> {v!r} (key={key!r}) is missing relationship_type"
                )
            if relationship_type is not None and rel_type != relationship_type:
                continue
            from_type, from_id = split_node_id(u)
            to_type, to_id = split_node_id(v)
            yield (
                from_type,
                from_id,
                to_type,
                to_id,
                rel_type,
                key,
                data.get("properties", {}),
                _relationship_metadata(data),
            )

    def iter_edges(
        self,
        relationship_type: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate edges as dicts including edge_key and relationship_type."""
        for (
            from_type,
            from_id,
            to_type,
            to_id,
            rel_type,
            key,
            props,
            metadata,
        ) in self._iter_edges_raw(relationship_type):
            yield {
                "from_type": from_type,
                "from_id": from_id,
                "to_type": to_type,
                "to_id": to_id,
                "relationship_type": rel_type,
                "edge_key": key,
                "properties": props,
                "metadata": _metadata_dict(metadata),
            }

    def iter_relationships(
        self,
        relationship_type: str | None = None,
    ) -> Iterator[RelationshipInstance]:
        """Iterate relationships as typed instances."""
        for (
            from_type,
            from_id,
            to_type,
            to_id,
            rel_type,
            key,
            props,
            metadata,
        ) in self._iter_edges_raw(relationship_type):
            yield RelationshipInstance(
                relationship_type=rel_type,
                from_type=from_type,
                from_id=from_id,
                to_type=to_type,
                to_id=to_id,
                edge_key=key if isinstance(key, int) else None,
                properties=dict(props),
                metadata=metadata,
            )

    def list_edges(
        self,
        relationship_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List edges as dicts. Materializes iter_edges()."""
        return list(self.iter_edges(relationship_type=relationship_type))

    def get_neighbors_with_relationship_refs(
        self,
        entity_type: str,
        entity_id: str,
        relationship_type: str | None = None,
        direction: str = "both",
    ) -> list[tuple[EntityInstance, dict[str, Any], RelationshipMetadata, int]]:
        """Get neighbors, edge properties, metadata, and edge key."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return []

        results: list[tuple[EntityInstance, dict[str, Any], RelationshipMetadata, int]] = []
        seen_edges: set[tuple[str, str, str]] = set()

        if direction in ("outgoing", "both"):
            for source, target, key, data in self._graph.out_edges(node_id, keys=True, data=True):
                if (
                    relationship_type is not None
                    and data.get("relationship_type") != relationship_type
                ):
                    continue
                edge_id = (source, target, str(key))
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                entity = self.get_entity(*split_node_id(target))
                if entity:
                    results.append(
                        (entity, data.get("properties", {}), _relationship_metadata(data), key)
                    )

        if direction in ("incoming", "both"):
            for source, target, key, data in self._graph.in_edges(node_id, keys=True, data=True):
                if (
                    relationship_type is not None
                    and data.get("relationship_type") != relationship_type
                ):
                    continue
                edge_id = (source, target, str(key))
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                entity = self.get_entity(*split_node_id(source))
                if entity:
                    results.append(
                        (entity, data.get("properties", {}), _relationship_metadata(data), key)
                    )

        return results

    def get_neighbor_relationships(
        self,
        entity_type: str,
        entity_id: str,
        relationship_type: str | None = None,
        direction: str = "both",
    ) -> list[dict[str, Any]]:
        """Get neighboring entities plus edge metadata for inspection surfaces."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return []

        results: list[dict[str, Any]] = []
        seen_edges: set[tuple[str, str, str]] = set()

        if direction in ("outgoing", "both"):
            for source, target, key, data in self._graph.out_edges(node_id, keys=True, data=True):
                rel_type = data.get("relationship_type")
                if relationship_type is not None and rel_type != relationship_type:
                    continue
                edge_id = (source, target, str(key))
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                entity = self.get_entity(*split_node_id(target))
                if entity is not None:
                    results.append(
                        {
                            "direction": "outgoing",
                            "relationship_type": rel_type,
                            "edge_key": key,
                            "properties": data.get("properties", {}),
                            "metadata": _metadata_dict(_relationship_metadata(data)),
                            "entity": entity,
                        }
                    )

        if direction in ("incoming", "both"):
            for source, target, key, data in self._graph.in_edges(node_id, keys=True, data=True):
                rel_type = data.get("relationship_type")
                if relationship_type is not None and rel_type != relationship_type:
                    continue
                edge_id = (source, target, str(key))
                if edge_id in seen_edges:
                    continue
                seen_edges.add(edge_id)
                entity = self.get_entity(*split_node_id(source))
                if entity is not None:
                    results.append(
                        {
                            "direction": "incoming",
                            "relationship_type": rel_type,
                            "edge_key": key,
                            "properties": data.get("properties", {}),
                            "metadata": _metadata_dict(_relationship_metadata(data)),
                            "entity": entity,
                        }
                    )

        return results

    def expand_neighborhood(
        self,
        entity_type: str,
        entity_id: str,
        *,
        depth: int = 1,
        direction: str = "both",
        relationship_types: list[str] | None = None,
        target_types: list[str] | None = None,
        edge_visible: Callable[[RelationshipMetadata], bool] | None = None,
        max_nodes: int = 100,
        max_edges: int = 200,
    ) -> NeighborhoodExpansion:
        """Deterministic budget-aware BFS expansion around one root entity.

        Semantics (the generic bounded read beneath named traversal queries):

        * Frontier is processed level by level, each level ordered by
          ``(entity_type, entity_id)``; candidate edges of a node are ordered
          by ``(relationship_type, to_type, to_id, from_type, from_id,
          edge_key)`` — so partial (budget-clipped) results are deterministic.
        * Cycles are visited once: an entity is returned at its minimum depth
          only; edges back into already-visited entities are still returned.
        * ``edge_visible`` gates edges (callers pass the query engine's
          ``relationship_matches_query_state`` bound to a state so visibility
          is bit-identical to traversal); hidden edges are never walked.
          Edges rejected solely by ``edge_visible`` while otherwise passing
          every filter are tallied in ``hidden_edge_count`` (deduplicated;
          counted only at nodes the BFS expanded — the explored frontier —
          never speculatively behind other hidden edges; no budget consumed).
        * ``target_types`` drops non-matching NEW neighbors before any budget
          is consumed (budgets count returned items, not visited); the root is
          exempt. Dropped neighbors are not expanded either.
        * Budgets: ``max_nodes`` counts returned non-root nodes, ``max_edges``
          counts returned edges. A new node needs node AND edge capacity (its
          discovery edge is returned with it); an edge between two returned
          nodes needs edge capacity only. When a budget trips, expansion stops
          but everything collected so far is returned with the tripped reason.
        * Edges among nodes at the final depth are not scanned (only nodes at
          depth < ``depth`` are expanded); a ``"depth"`` truncation reason is
          recorded when a final-depth node still has a visible, filter-passing
          edge that was not returned.
        """
        root_node_id = make_node_id(entity_type, entity_id)
        if root_node_id not in self._graph:
            return NeighborhoodExpansion()

        rel_filter = set(relationship_types) if relationship_types else None
        target_filter = set(target_types) if target_types else None

        def _record_hidden_edge(node_id: str, source: str, target: str, key: int) -> None:
            """Tally an edge excluded SOLELY by ``edge_visible`` at an expanded node.

            Mirrors the visible path's remaining filters so the count means
            "this edge would have been returned under a permissive gate":
            the far endpoint must exist, and ``target_types`` applies exactly
            as it does for visible edges (edges back into already-returned
            entities are exempt from the target filter).
            """
            edge_id = (source, target, key)
            if edge_id in hidden_edges:
                return
            other = target if source == node_id else source
            if other not in visited:
                other_entity = self.get_entity(*split_node_id(other))
                if other_entity is None:
                    return
                if target_filter is not None and other_entity.entity_type not in target_filter:
                    return
            hidden_edges.add(edge_id)

        def _candidate_edges(
            node_id: str, *, count_hidden: bool = False
        ) -> list[tuple[str, str, int, dict[str, Any]]]:
            """Visible, filter-passing edges of a node in deterministic order."""
            seen: set[tuple[str, str, int]] = set()
            candidates: list[tuple[str, str, int, dict[str, Any]]] = []
            edge_scans: list[Iterable[tuple[str, str, int, dict[str, Any]]]] = []
            if direction in ("outgoing", "both"):
                edge_scans.append(self._graph.out_edges(node_id, keys=True, data=True))
            if direction in ("incoming", "both"):
                edge_scans.append(self._graph.in_edges(node_id, keys=True, data=True))
            for scan in edge_scans:
                for source, target, key, data in scan:
                    if (source, target, key) in seen:
                        continue
                    seen.add((source, target, key))
                    rel_type = data.get("relationship_type")
                    if rel_filter is not None and rel_type not in rel_filter:
                        continue
                    if edge_visible is not None and not edge_visible(_relationship_metadata(data)):
                        if count_hidden:
                            _record_hidden_edge(node_id, source, target, key)
                        continue
                    candidates.append((source, target, key, data))
            candidates.sort(
                key=lambda edge: (
                    str(edge[3].get("relationship_type")),
                    *split_node_id(edge[1]),
                    *split_node_id(edge[0]),
                    edge[2],
                )
            )
            return candidates

        def _edge_payload(
            source: str, target: str, key: int, data: dict[str, Any]
        ) -> dict[str, Any]:
            from_type, from_id = split_node_id(source)
            to_type, to_id = split_node_id(target)
            return {
                "relationship_type": data.get("relationship_type"),
                "from_type": from_type,
                "from_id": from_id,
                "to_type": to_type,
                "to_id": to_id,
                "edge_key": key,
                "properties": dict(data.get("properties", {})),
                "metadata": _metadata_dict(_relationship_metadata(data)),
            }

        visited: dict[str, int] = {root_node_id: 0}
        returned_nodes: list[tuple[EntityInstance, int]] = []
        collected: dict[tuple[str, str, int], dict[str, Any]] = {}
        hidden_edges: set[tuple[str, str, int]] = set()
        reasons: set[str] = set()
        frontier = [root_node_id]
        edge_budget_exhausted = False

        for level in range(depth):
            if edge_budget_exhausted or not frontier:
                break
            frontier.sort(key=lambda node_id: split_node_id(node_id))
            next_frontier: list[str] = []
            for node_id in frontier:
                if edge_budget_exhausted:
                    break
                for source, target, key, data in _candidate_edges(node_id, count_hidden=True):
                    edge_id = (source, target, key)
                    if edge_id in collected:
                        continue
                    other = target if source == node_id else source
                    if other in visited:
                        # Cycle / cross edge between returned entities: edge
                        # capacity only, the entity is never re-returned.
                        if len(collected) >= max_edges:
                            reasons.add("edge_budget")
                            edge_budget_exhausted = True
                            break
                        collected[edge_id] = _edge_payload(source, target, key, data)
                        continue
                    other_entity = self.get_entity(*split_node_id(other))
                    if other_entity is None:
                        continue
                    if target_filter is not None and other_entity.entity_type not in target_filter:
                        # Filtered neighbors consume NO budget and are not
                        # expanded; their edge is dropped with them.
                        continue
                    if len(returned_nodes) >= max_nodes:
                        reasons.add("node_budget")
                        continue
                    if len(collected) >= max_edges:
                        reasons.add("edge_budget")
                        edge_budget_exhausted = True
                        break
                    visited[other] = level + 1
                    returned_nodes.append((other_entity, level + 1))
                    collected[edge_id] = _edge_payload(source, target, key, data)
                    next_frontier.append(other)
            frontier = next_frontier

        # Depth-horizon check: a node AT the depth limit with a visible,
        # filter-passing, un-returned edge means the horizon clipped the read.
        for node_id in sorted((n for n, d in visited.items() if d == depth), key=split_node_id):
            if "depth" in reasons:
                break
            for source, target, key, _data in _candidate_edges(node_id):
                if (source, target, key) in collected:
                    continue
                other = target if source == node_id else source
                if other in visited:
                    reasons.add("depth")
                    break
                other_entity = self.get_entity(*split_node_id(other))
                if other_entity is None:
                    continue
                if target_filter is not None and other_entity.entity_type not in target_filter:
                    continue
                reasons.add("depth")
                break

        returned_nodes.sort(key=lambda pair: (pair[1], pair[0].entity_type, pair[0].entity_id))
        edges = sorted(
            collected.values(),
            key=lambda edge: (
                str(edge["relationship_type"]),
                edge["from_type"],
                edge["from_id"],
                edge["to_type"],
                edge["to_id"],
                edge["edge_key"],
            ),
        )
        ordered_reasons = [r for r in NEIGHBORHOOD_TRUNCATION_REASONS if r in reasons]
        return NeighborhoodExpansion(
            nodes=returned_nodes,
            edges=edges,
            truncated=bool(ordered_reasons),
            truncation_reasons=ordered_reasons,
            hidden_edge_count=len(hidden_edges),
        )

    # -------------------------------------------------------------------------
    # Introspection
    # -------------------------------------------------------------------------

    def list_entity_types(self) -> list[str]:
        """Return entity types that have instances in the graph."""
        return [t for t, ids in self._entities_by_type.items() if ids]

    def list_relationship_types(self) -> list[str]:
        """Return relationship types that have edges in the graph."""
        types: set[str] = set()
        for _, _, _, data in self._graph.edges(keys=True, data=True):
            rt = data.get("relationship_type")
            if rt:
                types.add(rt)
        return sorted(types)

    # -------------------------------------------------------------------------
    # Counts
    # -------------------------------------------------------------------------

    def entity_count(self, entity_type: str | None = None) -> int:
        """Count entities, optionally filtered by type."""
        if entity_type is None:
            return int(self._graph.number_of_nodes())
        return len(self._entities_by_type.get(entity_type, set()))

    def count_edges(
        self,
        entity_type: str,
        entity_id: str,
        relationship_type: str | None = None,
        direction: str = "both",
    ) -> int:
        """Count edges by type/direction without materializing neighbors."""
        node_id = make_node_id(entity_type, entity_id)
        if node_id not in self._graph:
            return 0
        n = 0
        if direction in ("incoming", "both"):
            for _, _, data in self._graph.in_edges(node_id, data=True):
                if relationship_type is None or data.get("relationship_type") == relationship_type:
                    n += 1
        if direction in ("outgoing", "both"):
            for _, _, data in self._graph.out_edges(node_id, data=True):
                if relationship_type is None or data.get("relationship_type") == relationship_type:
                    n += 1
        return n

    def edge_count(self, relationship_type: str | None = None) -> int:
        """Count edges, optionally filtered by type."""
        if relationship_type is None:
            return int(self._graph.number_of_edges())
        return sum(
            1
            for _, _, _, data in self._graph.edges(keys=True, data=True)
            if data.get("relationship_type") == relationship_type
        )

    def extract_owned_subgraph(
        self,
        *,
        entity_types: list[str],
        relationship_types: list[str],
    ) -> EntityGraph:
        """Extract a subgraph containing selected entity and relationship types."""
        entity_type_set = set(entity_types)
        relationship_type_set = set(relationship_types)
        subgraph = EntityGraph()

        for entity in self.iter_all_entities():
            if entity.entity_type in entity_type_set:
                subgraph.add_entity(entity)

        for edge in self.iter_edges():
            if edge["relationship_type"] not in relationship_type_set:
                continue
            if edge["from_type"] in entity_type_set and not subgraph.has_entity(
                edge["from_type"], edge["from_id"]
            ):
                source = self.get_entity(edge["from_type"], edge["from_id"])
                if source is not None:
                    subgraph.add_entity(source)
            if edge["to_type"] in entity_type_set and not subgraph.has_entity(
                edge["to_type"], edge["to_id"]
            ):
                target = self.get_entity(edge["to_type"], edge["to_id"])
                if target is not None:
                    subgraph.add_entity(target)
            subgraph.add_relationship(
                RelationshipInstance(
                    relationship_type=edge["relationship_type"],
                    from_type=edge["from_type"],
                    from_id=edge["from_id"],
                    to_type=edge["to_type"],
                    to_id=edge["to_id"],
                    edge_key=edge["edge_key"],
                    properties=dict(edge["properties"]),
                    metadata=RelationshipMetadata.model_validate(edge.get("metadata") or {}),
                )
            )

        return subgraph

    @classmethod
    def merge_graphs(cls, base: EntityGraph, overlay: EntityGraph) -> EntityGraph:
        """Merge two graphs by upserting overlay entities and appending overlay edges.

        Overlay entities with empty properties that already exist in base are
        treated as cross-boundary stubs (created by ``extract_owned_subgraph``)
        and skipped, so they do not clobber populated base properties. This is a
        defensive guard pending a post-0.2 redesign of stub-creation in extract.
        """
        merged = cls.from_dict(base.to_dict())

        for entity in overlay.iter_all_entities():
            # Defensive: extract_owned_subgraph emits empty-property stubs for
            # cross-boundary endpoints. Skip them when base already has the
            # entity so the stub does not clobber populated upstream data.
            # Revisit when the extract-stub behavior is redesigned post-0.2.
            if merged.has_entity(entity.entity_type, entity.entity_id) and not entity.properties:
                stub_metadata = _entity_metadata_dict(entity.metadata)
                if stub_metadata:
                    merged.update_entity_metadata(
                        entity.entity_type,
                        entity.entity_id,
                        stub_metadata,
                    )
                continue
            merged.add_entity(entity)

        for edge in overlay.iter_edges():
            if not merged.has_entity(edge["from_type"], edge["from_id"]):
                raise ValueError(
                    "Overlay relationship references missing source entity "
                    f"{edge['from_type']}:{edge['from_id']}"
                )
            if not merged.has_entity(edge["to_type"], edge["to_id"]):
                raise ValueError(
                    "Overlay relationship references missing target entity "
                    f"{edge['to_type']}:{edge['to_id']}"
                )
            merged.add_relationship(
                RelationshipInstance(
                    relationship_type=edge["relationship_type"],
                    from_type=edge["from_type"],
                    from_id=edge["from_id"],
                    to_type=edge["to_type"],
                    to_id=edge["to_id"],
                    edge_key=edge["edge_key"],
                    properties=dict(edge["properties"]),
                    metadata=RelationshipMetadata.model_validate(edge.get("metadata") or {}),
                )
            )

        return merged

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize the graph to a dict (networkx node-link format)."""
        return dict(nx.node_link_data(self._graph, edges="edges"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityGraph:
        """Deserialize a graph from a dict (networkx node-link format).

        Rebuilds internal indexes (_entities_by_type, _edge_counter).
        """
        graph = cls()
        nx_graph = nx.node_link_graph(data, directed=True, multigraph=True, edges="edges")
        if not nx_graph.is_directed() or not nx_graph.is_multigraph():
            raise ValueError("Graph data must represent a directed multigraph")
        graph._graph = nx_graph
        # rebuild _entities_by_type index
        for node_id, node_data in graph._graph.nodes(data=True):
            entity_type = node_data.get("entity_type")
            if entity_type:
                graph._entities_by_type[entity_type].add(node_id)
            node_data.setdefault("metadata", {})
        # rebuild _edge_counter
        max_key = -1
        for _, _, key, edge_data in graph._graph.edges(keys=True, data=True):
            if isinstance(key, int) and key > max_key:
                max_key = key
            edge_data.setdefault("metadata", _metadata_dict(RelationshipMetadata()))
        graph._edge_counter = count(max_key + 1)
        return graph
