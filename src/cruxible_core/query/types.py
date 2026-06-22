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


class QueryIncludeItem(BaseModel):
    """One included one-hop side-context relationship."""

    edge: QueryPathSegment
    source: EntityInstance
    target: EntityInstance


class QueryIncludeResult(BaseModel):
    """Side-context attached to a primary query row."""

    alias: str
    many: bool = False
    exists: bool = False
    count: int = 0
    limit: int | None = None
    truncated: bool = False
    items: list[QueryIncludeItem] = Field(default_factory=list)
    # Stable identities of every matched neighbor (pre-`limit` truncation), used
    # only to compute the include summary's distinct `total_matches`. Excluded
    # from serialization so it never reaches query output, projections, or
    # golden receipts.
    match_identities: tuple[tuple[Any, ...], ...] = Field(
        default_factory=tuple, exclude=True, repr=False
    )


class QueryPathRow(BaseModel):
    """Path-shaped query result with full entity payloads and relationship evidence."""

    entry: EntityInstance
    result: EntityInstance
    entities: list[EntityInstance]
    path: list[QueryPathSegment]
    includes: dict[str, QueryIncludeResult] = Field(default_factory=dict)


class QueryRelationshipRow(RelationshipInstance):
    """Relationship-shaped query result with entry and endpoint context."""

    entry: EntityInstance
    from_entity: EntityInstance | None = None
    to_entity: EntityInstance | None = None
    includes: dict[str, QueryIncludeResult] = Field(default_factory=dict)


BaseQueryRow = EntityInstance | QueryPathRow | QueryRelationshipRow


class ProjectedQueryRow(BaseModel):
    """Projected query row with preserved source evidence."""

    values: dict[str, Any]
    source: BaseQueryRow | None = None


QueryRow = BaseQueryRow | ProjectedQueryRow


def dump_query_row(
    row: QueryRow,
    *,
    include_source: bool = False,
    mode: str = "python",
) -> dict[str, Any]:
    """Serialize a query row with explicit projected-source handling."""
    if isinstance(row, ProjectedQueryRow):
        if include_source:
            return row.model_dump(mode=mode)
        return row.model_dump(mode=mode, exclude={"source"})
    return row.model_dump(mode=mode)


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
    limit: int | None = None
    truncated: bool = False
    limit_truncated: bool = False
    path_truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    total_path_count: int | None = None
    retained_path_count: int | None = None
    receipt: Receipt | None = None
    policy_summary: dict[str, int] = Field(default_factory=dict)

    def model_post_init(self, _context: Any) -> None:
        if self.total_results is None:
            self.total_results = len(self.results)


__all__ = [
    "BaseQueryRow",
    "ProjectedQueryRow",
    "QueryDedupe",
    "QueryIncludeItem",
    "QueryIncludeResult",
    "QueryPathRow",
    "QueryRelationshipState",
    "QueryPathSegment",
    "QueryRelationshipRow",
    "QueryResult",
    "QueryResultShape",
    "QueryRow",
    "dump_query_row",
]
