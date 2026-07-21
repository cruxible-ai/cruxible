"""Entity graph module."""

from __future__ import annotations

from typing import Any

__all__ = [
    "EntityGraph",
    "EntityInstance",
    "RelationshipInstance",
    "make_node_id",
    "split_node_id",
]


def __getattr__(name: str) -> Any:
    """Avoid importing NetworkX until the graph implementation is requested."""
    if name == "EntityGraph":
        from cruxible_core.graph.entity_graph import EntityGraph

        return EntityGraph
    if name in {"EntityInstance", "RelationshipInstance", "make_node_id", "split_node_id"}:
        from cruxible_core.graph import types

        return getattr(types, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
