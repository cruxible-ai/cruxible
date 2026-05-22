"""Projection and deterministic ordering helpers for named-query rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cruxible_core.errors import QueryExecutionError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.predicate import PredicateCoercionError, coerce_predicate_value
from cruxible_core.query.predicates import is_missing_path, resolve_path
from cruxible_core.query.types import (
    BaseQueryRow,
    ProjectedQueryRow,
    QueryIncludeResult,
    QueryPathRow,
    QueryPathSegment,
    QueryRelationshipRow,
)

if TYPE_CHECKING:
    from cruxible_core.config.schema import CoreConfig, QueryOrderSpec


@dataclass(frozen=True)
class QueryRowContext:
    """Internal base row plus evidence needed for projection and ordering."""

    row: BaseQueryRow
    entry: EntityInstance
    result: EntityInstance
    entities: tuple[EntityInstance, ...]
    path: tuple[QueryPathSegment, ...]
    parent_id: str | None = None
    optional_path_aliases: frozenset[str] = frozenset()
    includes: dict[str, QueryIncludeResult] = field(default_factory=dict)


def project_query_row(
    select: dict[str, Any],
    context: QueryRowContext,
    params: dict[str, Any],
) -> ProjectedQueryRow:
    """Build a projected row while preserving the base source row."""
    return ProjectedQueryRow(
        values={
            key: _resolve_projection_value(value, context, params)
            for key, value in select.items()
        },
        source=context.row,
    )


def sort_query_row_contexts(
    contexts: Sequence[QueryRowContext],
    order_by: Sequence[QueryOrderSpec],
    params: dict[str, Any],
    *,
    config: CoreConfig | None = None,
) -> list[QueryRowContext]:
    """Sort query row contexts by explicit order specs plus stable identity."""
    if not order_by:
        return sorted(contexts, key=stable_row_identity)

    def compare(left: QueryRowContext, right: QueryRowContext) -> int:
        for order in order_by:
            left_value = _resolve_order_value(order, left, params, config=config)
            right_value = _resolve_order_value(order, right, params, config=config)
            if left_value is None and right_value is None:
                continue
            if left_value is None:
                return 1
            if right_value is None:
                return -1
            result = compare_order_values(left_value, right_value)
            if result != 0:
                return -result if order.direction == "desc" else result
        return compare_sort_keys(stable_row_identity(left), stable_row_identity(right))

    return sorted(contexts, key=cmp_to_key(compare))


def resolve_query_row_ref(
    ref: str,
    context: QueryRowContext,
    params: dict[str, Any],
) -> Any:
    """Resolve one query row reference against base row evidence."""
    scope, path = _split_query_ref(ref)
    if scope == "input":
        value = resolve_path(params, path)
        if is_missing_path(value):
            raise QueryExecutionError(f"Missing query input reference '{ref}'")
        return value
    if scope == "entry":
        return _optional_path(ref, context.entry, path)
    if scope == "result":
        return _optional_path(ref, context.result, path)
    if scope == "path":
        return _resolve_path_ref(ref, path, context)
    if scope == "include":
        return _resolve_include_ref(ref, path, context)
    if scope == "relationship":
        if not isinstance(context.row, QueryRelationshipRow):
            raise QueryExecutionError(
                f"Query reference '{ref}' is only available for relationship rows"
            )
        return _optional_path(ref, context.row, path)
    if scope == "from_entity":
        if not isinstance(context.row, QueryRelationshipRow):
            raise QueryExecutionError(
                f"Query reference '{ref}' is only available for relationship rows"
            )
        return _optional_path(ref, context.row.from_entity, path)
    if scope == "to_entity":
        if not isinstance(context.row, QueryRelationshipRow):
            raise QueryExecutionError(
                f"Query reference '{ref}' is only available for relationship rows"
            )
        return _optional_path(ref, context.row.to_entity, path)
    raise QueryExecutionError(f"Unsupported query reference '{ref}'")


def stable_row_identity(context: QueryRowContext) -> tuple[Any, ...]:
    """Return a deterministic identity key for a base query row."""
    row = context.row
    if isinstance(row, QueryPathRow):
        return (
            "path",
            row.entry.entity_type,
            row.entry.entity_id,
            _path_identity(tuple(row.path)),
            row.result.entity_type,
            row.result.entity_id,
        )
    if isinstance(row, QueryRelationshipRow):
        return (
            "relationship",
            row.relationship_type,
            row.from_type,
            row.from_id,
            row.to_type,
            row.to_id,
            row.edge_key is None,
            row.edge_key if row.edge_key is not None else -1,
        )
    return ("entity", row.entity_type, row.entity_id)


def _resolve_projection_value(
    value: Any,
    context: QueryRowContext,
    params: dict[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return resolve_query_row_ref(value, context, params)
    if isinstance(value, list):
        return [_resolve_projection_value(item, context, params) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_projection_value(item, context, params)
            for key, item in value.items()
        }
    return value


def coerce_query_order_value(
    value: Any,
    order: QueryOrderSpec,
    config: CoreConfig | None,
    *,
    label: str,
) -> Any:
    """Coerce one resolved order value according to an order spec."""
    if value is None:
        return None
    if order.enum_ref is not None:
        if config is None:
            raise QueryExecutionError(
                f"Cannot use enum_ref '{order.enum_ref}' for '{label}' without config"
            )
        enum_schema = config.enums.get(order.enum_ref)
        if enum_schema is None or enum_schema.ordered != "low_to_high":
            raise QueryExecutionError(
                f"Order reference '{label}' uses unordered or unknown enum_ref "
                f"'{order.enum_ref}'"
            )
        if value not in enum_schema.values:
            raise QueryExecutionError(
                f"Invalid ordered enum value for '{label}': {value!r} is not in "
                f"enum_ref '{order.enum_ref}'"
            )
        return enum_schema.values.index(value)
    if order.value_type is None:
        return value
    try:
        return coerce_predicate_value(value, order.value_type)
    except PredicateCoercionError as exc:
        raise QueryExecutionError(
            f"Invalid {exc.value_type} order_by value for '{label}': {exc.value!r}"
        ) from exc


def _resolve_order_value(
    order: QueryOrderSpec,
    context: QueryRowContext,
    params: dict[str, Any],
    *,
    config: CoreConfig | None,
) -> Any:
    value = resolve_query_row_ref(order.by, context, params)
    return coerce_query_order_value(value, order, config, label=order.by)


def compare_order_values(left: Any, right: Any) -> int:
    """Compare query ordering values with deterministic fallback behavior."""
    try:
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    except TypeError:
        return compare_sort_keys(fallback_sort_key(left), fallback_sort_key(right))


def compare_sort_keys(left: tuple[Any, ...], right: tuple[Any, ...]) -> int:
    """Compare stable tuple sort keys."""
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def fallback_sort_key(value: Any) -> tuple[str, str]:
    """Return a deterministic key for values that cannot be directly compared."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return (type(value).__name__, str(value))


def _resolve_path_ref(
    ref: str,
    path: list[str],
    context: QueryRowContext,
) -> Any:
    if len(path) < 2:
        raise QueryExecutionError(
            f"Path query reference '{ref}' must include alias and edge/source/target"
        )
    alias, section, *field_path = path
    segment = _path_segment_by_alias(
        ref,
        context.path,
        alias,
        optional_path_aliases=context.optional_path_aliases,
    )
    if segment is None:
        return None
    if section == "edge":
        return _optional_path(ref, segment, field_path)
    if section == "source":
        entity = _find_path_entity(context.entities, segment.from_type, segment.from_id)
        return _optional_path(ref, entity, field_path)
    if section == "target":
        entity = _find_path_entity(context.entities, segment.to_type, segment.to_id)
        return _optional_path(ref, entity, field_path)
    raise QueryExecutionError(
        f"Path query reference '{ref}' must use edge, source, or target after alias"
    )


def _resolve_include_ref(
    ref: str,
    path: list[str],
    context: QueryRowContext,
) -> Any:
    if not path:
        raise QueryExecutionError(f"Include query reference '{ref}' must include an alias")
    alias, *field_path = path
    include = context.includes.get(alias)
    if include is None:
        raise QueryExecutionError(f"Unknown include alias '{alias}' in query reference '{ref}'")
    if not field_path:
        return include.model_dump(mode="python")

    section, *rest = field_path
    if section in {"alias", "many", "exists", "count", "limit", "truncated", "items"}:
        return _optional_path(ref, include, field_path)
    if section not in {"edge", "source", "target"}:
        raise QueryExecutionError(
            f"Include query reference '{ref}' must use exists, count, truncated, "
            "items, edge, source, or target after alias"
        )
    if include.many:
        raise QueryExecutionError(
            f"Include query reference '{ref}' targets a many include; select "
            f"$include.{alias}.items, count, or existence instead"
        )
    if not include.items:
        return None
    item = include.items[0]
    return _optional_path(ref, getattr(item, section), rest)


def _path_segment_by_alias(
    ref: str,
    path: tuple[QueryPathSegment, ...],
    alias: str,
    *,
    optional_path_aliases: frozenset[str],
) -> QueryPathSegment | None:
    matches = [segment for segment in path if segment.alias == alias]
    if not matches:
        if alias in optional_path_aliases:
            return None
        raise QueryExecutionError(f"Unknown path alias '{alias}' in query reference '{ref}'")
    if len(matches) > 1:
        raise QueryExecutionError(f"Duplicate path alias '{alias}' in query result path")
    return matches[0]


def _find_path_entity(
    entities: tuple[EntityInstance, ...],
    entity_type: str,
    entity_id: str,
) -> EntityInstance | None:
    for entity in entities:
        if entity.entity_type == entity_type and entity.entity_id == entity_id:
            return entity
    return None


def _optional_path(ref: str, value: Any, path: list[str]) -> Any:
    if value is None:
        return None
    resolved = resolve_path(value, path)
    if is_missing_path(resolved):
        return None
    return resolved


def _split_query_ref(ref: str) -> tuple[str, list[str]]:
    if not ref.startswith("$"):
        raise QueryExecutionError(f"Query reference '{ref}' must start with '$'")
    scope, sep, path = ref[1:].partition(".")
    if not scope or not sep or not path:
        raise QueryExecutionError(f"Invalid query reference '{ref}'")
    return scope, path.split(".")


def _path_identity(path: tuple[QueryPathSegment, ...]) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            segment.relationship_type,
            segment.from_type,
            segment.from_id,
            segment.to_type,
            segment.to_id,
            segment.edge_key is None,
            segment.edge_key if segment.edge_key is not None else -1,
        )
        for segment in path
    )


__all__ = [
    "QueryRowContext",
    "coerce_query_order_value",
    "compare_order_values",
    "compare_sort_keys",
    "fallback_sort_key",
    "project_query_row",
    "resolve_query_row_ref",
    "sort_query_row_contexts",
    "stable_row_identity",
]
