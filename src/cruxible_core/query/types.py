"""Shared query result contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.query.enums import QueryDedupe, QueryRelationshipState, QueryResultShape
from cruxible_core.receipt.types import Receipt


class QueryPathSegment(RelationshipInstance):
    """One relationship segment in a query evidence path."""

    alias: str | None = None


class QueryPathRow(BaseModel):
    """Path-shaped query result with full entity payloads and relationship evidence."""

    entry: EntityInstance
    result: EntityInstance
    entities: list[EntityInstance]
    path: list[QueryPathSegment]


class QueryRelationshipRow(RelationshipInstance):
    """Relationship-shaped query result with entry and endpoint context."""

    entry: EntityInstance
    from_entity: EntityInstance | None = None
    to_entity: EntityInstance | None = None


QueryRow = EntityInstance | QueryPathRow | QueryRelationshipRow


class QueryResult(BaseModel):
    """Result of executing a named query."""

    query_name: str
    parameters: dict[str, Any]
    results: list[QueryRow]
    result_shape: QueryResultShape = "path"
    dedupe: QueryDedupe = "path"
    relationship_state: QueryRelationshipState = "live"
    steps_executed: int
    total_results: int | None = None
    receipt: Receipt | None = None
    policy_summary: dict[str, int] = Field(default_factory=dict)

    def model_post_init(self, _context: Any) -> None:
        if self.total_results is None:
            self.total_results = len(self.results)


__all__ = [
    "QueryDedupe",
    "QueryPathRow",
    "QueryRelationshipState",
    "QueryPathSegment",
    "QueryRelationshipRow",
    "QueryResult",
    "QueryResultShape",
    "QueryRow",
]
