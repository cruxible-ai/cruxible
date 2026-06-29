"""Query engine, traversal, and constraints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cruxible_core.query.types import QueryResult


def execute_query(*args: Any, **kwargs: Any) -> "QueryResult":
    """Execute a named query.

    Imported lazily so config schema validation can use query result contracts
    without importing the query engine and its config dependencies.
    """
    from cruxible_core.query.engine import execute_query as _execute_query

    return _execute_query(*args, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "QueryResult":
        from cruxible_core.query.types import QueryResult

        return QueryResult
    if name == "execute_query":
        return execute_query
    raise AttributeError(name)


__all__ = [
    "QueryResult",
    "execute_query",
]
