"""Structured predicate evaluation for named-query traversal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.graph.types import EntityInstance
from cruxible_core.predicate import (
    PredicateCoercionError,
    PredicateValueType,
    evaluate_typed_comparison,
    infer_predicate_value_type,
)
from cruxible_core.query.enums import QueryVisibilityState
from cruxible_core.query.relationship_state import relationship_matches_query_state
from cruxible_core.query.types import QueryPathSegment

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from cruxible_core.config.schema import (
        CoreConfig,
        NamedQuerySchema,
        QueryPredicateSpec,
        RelatedPredicateSpec,
    )
    from cruxible_core.graph.entity_graph import EntityGraph

_MISSING = object()
RELATED_PREDICATE_SCOPES = (
    "edge",
    "source",
    "target",
    "current",
    "candidate",
    "entry",
)
QUERY_PREDICATE_SCOPES = (*RELATED_PREDICATE_SCOPES, "result")


def relationship_property_names(
    config: CoreConfig,
    relationship_type: str | None,
) -> set[str]:
    """Return configured property names for a relationship type.

    When ``relationship_type`` is ``None`` the union of every configured
    relationship's properties is returned (used by the ``list edges`` surface
    when no relationship type is supplied). A reverse-alias name resolves to its
    canonical relationship so both surfaces agree on the configured schema.
    """
    if relationship_type is None:
        fields: set[str] = set()
        for relationship in config.relationships:
            fields.update(relationship.properties)
        return fields
    resolved = config.resolve_relationship_reference(relationship_type)
    if resolved is None:
        return set()
    schema, _is_reverse = resolved
    return set(schema.properties)


def iter_edge_where_property_fields(where: QueryPredicateSpec) -> Iterator[str]:
    """Yield the ``<X>`` of every ``edge.properties.<X>`` path in a where spec."""
    for path in where.root:
        parts = path.split(".")
        if len(parts) >= 3 and parts[0] == "edge" and parts[1] == "properties":
            yield parts[2]


def validate_edge_where_property_fields(
    config: CoreConfig,
    relationship_type: str | None,
    property_names: Iterable[str],
    *,
    subject: str,
) -> None:
    """Reject edge ``where``/property-filter fields not in the configured schema.

    Shared by the ``service_list("edges")`` surface and the inline
    relationship-collection query so an unconfigured ``edge.properties.<X>``
    raises the same :class:`ConfigError` on both. Keeps the two surfaces' read
    semantics fail-closed and identical for property-name validation.
    """
    known_fields = relationship_property_names(config, relationship_type)
    for field in property_names:
        if field not in known_fields:
            known = ", ".join(sorted(known_fields)) or "(none)"
            raise ConfigError(
                f"Unknown where field for {subject}: {field}. Known fields: {known}"
            )


@dataclass(frozen=True)
class PredicateContext:
    """Predicate evaluation context for one traversed query segment."""

    edge: QueryPathSegment
    source: EntityInstance
    target: EntityInstance
    current: EntityInstance
    candidate: EntityInstance
    entry: EntityInstance
    result: Any = None
    path: tuple[QueryPathSegment, ...] = ()
    entities: tuple[EntityInstance, ...] = ()
    optional_path_aliases: frozenset[str] = frozenset()


def iter_step_relationships(
    graph: EntityGraph,
    entity: EntityInstance,
    *,
    relationship_type: str,
    direction: str,
    alias: str | None,
) -> list[tuple[EntityInstance, QueryPathSegment, str]]:
    """Return traversable neighbor relationships with concrete edge identity."""
    rows = graph.get_neighbor_relationships(
        entity.entity_type,
        entity.entity_id,
        relationship_type=relationship_type,
        direction=direction,
    )
    relationships: list[tuple[EntityInstance, QueryPathSegment, str]] = []
    for row in rows:
        neighbor = row.get("entity")
        if not isinstance(neighbor, EntityInstance):
            continue
        relative_direction = str(row.get("direction"))
        if relative_direction == "outgoing":
            from_entity = entity
            to_entity = neighbor
        else:
            from_entity = neighbor
            to_entity = entity
        segment = QueryPathSegment(
            alias=alias,
            relationship_type=str(row["relationship_type"]),
            from_type=from_entity.entity_type,
            from_id=from_entity.entity_id,
            to_type=to_entity.entity_type,
            to_id=to_entity.entity_id,
            edge_key=row.get("edge_key"),
            properties=dict(row.get("properties", {})),
            metadata=row.get("metadata", {}),
        )
        relationships.append((neighbor, segment, relative_direction))
    return relationships


def build_predicate_context(
    *,
    entry: EntityInstance,
    current: EntityInstance,
    candidate: EntityInstance,
    segment: QueryPathSegment,
    path: tuple[QueryPathSegment, ...] = (),
    entities: tuple[EntityInstance, ...] = (),
    optional_path_aliases: frozenset[str] = frozenset(),
) -> PredicateContext:
    """Build a predicate context for one traversal candidate."""
    source, target = segment_endpoint_entities(current, candidate, segment)
    return PredicateContext(
        edge=segment,
        source=source,
        target=target,
        current=current,
        candidate=candidate,
        entry=entry,
        result=candidate,
        path=path,
        entities=entities,
        optional_path_aliases=optional_path_aliases,
    )


def segment_endpoint_entities(
    current: EntityInstance,
    candidate: EntityInstance,
    segment: QueryPathSegment,
) -> tuple[EntityInstance, EntityInstance]:
    """Return canonical source/target entities for a segment.

    The segment must connect the current and candidate entities. A mismatch
    means traversal state has become internally inconsistent and should not be
    treated as a valid predicate context.
    """
    if (
        segment.from_type == current.entity_type
        and segment.from_id == current.entity_id
        and segment.to_type == candidate.entity_type
        and segment.to_id == candidate.entity_id
    ):
        return current, candidate
    if (
        segment.from_type == candidate.entity_type
        and segment.from_id == candidate.entity_id
        and segment.to_type == current.entity_type
        and segment.to_id == current.entity_id
    ):
        return candidate, current
    raise QueryExecutionError(
        "Query path segment endpoints do not match traversal entities: "
        f"segment={segment.relationship_type} "
        f"{segment.from_type}:{segment.from_id}->{segment.to_type}:{segment.to_id}, "
        f"current={current.entity_type}:{current.entity_id}, "
        f"candidate={candidate.entity_type}:{candidate.entity_id}"
    )


def evaluate_query_predicates(
    config: CoreConfig,
    predicates: QueryPredicateSpec,
    context: PredicateContext,
    params: dict[str, Any],
    *,
    base_scope: str | None = None,
) -> bool:
    """Evaluate structured named-query predicates against one traversal context."""
    for path, operators in predicates.root.items():
        scope, field_path = _split_predicate_path(path, base_scope=base_scope)
        left = resolve_path(_scope_value(context, scope), field_path)
        for operator, raw_expected in operators.items():
            expected = _resolve_predicate_value(raw_expected, context, params)
            if not _evaluate_predicate_operator(
                config,
                context,
                path,
                scope,
                field_path,
                left,
                operator,
                expected,
            ):
                return False
    return True


def evaluate_related_predicate(
    graph: EntityGraph,
    related: RelatedPredicateSpec,
    context: PredicateContext,
    params: dict[str, Any],
    *,
    config: CoreConfig,
    relationship_state: QueryVisibilityState,
) -> bool:
    """Return whether a related edge exists and matches the related predicates."""
    anchor = context.candidate
    for related_neighbor, related_segment, _relative_direction in iter_step_relationships(
        graph,
        anchor,
        relationship_type=related.relationship,
        direction=related.direction,
        alias=None,
    ):
        if not relationship_matches_query_state(
            related_segment.metadata,
            relationship_state,
        ):
            continue
        related_context = _build_related_context(
            context,
            anchor=anchor,
            related_neighbor=related_neighbor,
            related_segment=related_segment,
        )
        if _related_predicates_match(config, related, related_context, params):
            return True
    return False


def related_edge_exists(
    graph: EntityGraph,
    *,
    current_entity: EntityInstance,
    candidate_entity: EntityInstance,
    relationship_type: str,
    direction: str,
    relationship_state: QueryVisibilityState,
) -> bool:
    """Check whether a related edge exists under the effective query state."""
    for neighbor, segment, _relative_direction in iter_step_relationships(
        graph,
        current_entity,
        relationship_type=relationship_type,
        direction=direction,
        alias=None,
    ):
        if neighbor.node_id() != candidate_entity.node_id():
            continue
        if relationship_matches_query_state(segment.metadata, relationship_state):
            return True
    return False


def query_filter_summary(query_schema: NamedQuerySchema) -> list[dict[str, Any]]:
    """Return config-level predicate summaries for receipt root metadata."""
    summaries: list[dict[str, Any]] = []
    if query_schema.where is not None:
        summaries.append({"scope": "query", "where": query_schema.where.model_dump(mode="python")})
    for index, step in enumerate(query_schema.traversal):
        summary: dict[str, Any] = {
            "step": index,
            "relationship": step.relationship,
            "direction": step.direction,
            "required": step.required,
        }
        if step.where is not None:
            summary["where"] = step.where.model_dump(mode="python")
        if step.where_related:
            summary["where_related"] = [
                related.model_dump(mode="python", exclude_none=True)
                for related in step.where_related
            ]
        if step.where_not_related:
            summary["where_not_related"] = [
                related.model_dump(mode="python", exclude_none=True)
                for related in step.where_not_related
            ]
        if any(key in summary for key in ("where", "where_related", "where_not_related")):
            summaries.append(summary)
    return summaries


def _build_related_context(
    original_context: PredicateContext,
    *,
    anchor: EntityInstance,
    related_neighbor: EntityInstance,
    related_segment: QueryPathSegment,
) -> PredicateContext:
    source, target = segment_endpoint_entities(anchor, related_neighbor, related_segment)
    return PredicateContext(
        edge=related_segment,
        source=source,
        target=target,
        current=original_context.current,
        candidate=original_context.candidate,
        entry=original_context.entry,
        result=original_context.result,
        path=original_context.path,
        entities=original_context.entities,
        optional_path_aliases=original_context.optional_path_aliases,
    )


def _related_predicates_match(
    config: CoreConfig,
    related: RelatedPredicateSpec,
    context: PredicateContext,
    params: dict[str, Any],
) -> bool:
    for scope, predicates in _iter_related_predicate_scopes(related):
        if not evaluate_query_predicates(
            config,
            predicates,
            context,
            params,
            base_scope=scope,
        ):
            return False
    return True


def _iter_related_predicate_scopes(
    related: RelatedPredicateSpec,
) -> Iterator[tuple[str, QueryPredicateSpec]]:
    for scope in RELATED_PREDICATE_SCOPES:
        predicates = getattr(related, scope)
        if predicates is not None:
            yield scope, predicates


def _split_predicate_path(path: str, *, base_scope: str | None) -> tuple[str, list[str]]:
    parts = path.split(".")
    if not parts:
        raise QueryExecutionError("predicate path must not be empty")
    first = parts[0]
    if first == "result":
        return first, parts[1:]
    if first in QUERY_PREDICATE_SCOPES:
        return first, parts[1:]
    if base_scope is not None:
        return base_scope, parts
    allowed = ", ".join(QUERY_PREDICATE_SCOPES)
    raise QueryExecutionError(
        f"Predicate path '{path}' must start with one of: {allowed}"
    )


def _scope_value(context: PredicateContext, scope: str) -> Any:
    return {
        "edge": context.edge,
        "source": context.source,
        "target": context.target,
        "current": context.current,
        "candidate": context.candidate,
        "entry": context.entry,
        "result": context.result,
    }[scope]


def _resolve_predicate_value(
    value: Any,
    context: PredicateContext,
    params: dict[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_predicate_ref(value, context, params)
    if isinstance(value, list):
        return [_resolve_predicate_value(item, context, params) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_predicate_value(item, context, params)
            for key, item in value.items()
        }
    return value


def _resolve_predicate_ref(
    ref: str,
    context: PredicateContext,
    params: dict[str, Any],
) -> Any:
    prefix, sep, path = ref[1:].partition(".")
    if not sep or not path:
        raise QueryExecutionError(f"Invalid predicate reference '{ref}'")
    if prefix == "input":
        value = resolve_path(params, path.split("."))
        if value is _MISSING:
            raise QueryExecutionError(f"Missing query input reference '{ref}'")
        return value
    if prefix in QUERY_PREDICATE_SCOPES:
        value = resolve_path(_scope_value(context, prefix), path.split("."))
        if value is _MISSING:
            raise QueryExecutionError(f"Missing predicate reference '{ref}'")
        return value
    if prefix == "path":
        return _resolve_path_predicate_ref(ref, context, path)
    raise QueryExecutionError(f"Unsupported predicate reference '{ref}'")


def _resolve_path_predicate_ref(
    ref: str,
    context: PredicateContext,
    raw_path: str,
) -> Any:
    parts = raw_path.split(".")
    if len(parts) < 2 or parts[1] not in {"edge", "source", "target"}:
        raise QueryExecutionError(
            f"Predicate reference '{ref}' must use $path.<alias>.edge|source|target"
        )
    alias, scope = parts[0], parts[1]
    segment = _predicate_path_segment(context, alias, ref)
    if segment is None:
        return _MISSING
    if scope == "edge":
        base: Any = segment
    elif scope == "source":
        base = _predicate_path_entity(context, segment.from_type, segment.from_id, ref)
    else:
        base = _predicate_path_entity(context, segment.to_type, segment.to_id, ref)
    value = resolve_path(base, parts[2:])
    if value is _MISSING:
        raise QueryExecutionError(f"Missing predicate reference '{ref}'")
    return value


def _predicate_path_segment(
    context: PredicateContext,
    alias: str,
    ref: str,
) -> QueryPathSegment | None:
    matches = [segment for segment in context.path if segment.alias == alias]
    if not matches:
        if alias in context.optional_path_aliases:
            return None
        raise QueryExecutionError(f"Unknown path alias '{alias}' in predicate reference '{ref}'")
    if len(matches) > 1:
        raise QueryExecutionError(f"Duplicate path alias '{alias}' in predicate reference '{ref}'")
    return matches[0]


def _predicate_path_entity(
    context: PredicateContext,
    entity_type: str,
    entity_id: str,
    ref: str,
) -> EntityInstance:
    for entity in context.entities:
        if entity.entity_type == entity_type and entity.entity_id == entity_id:
            return entity
    raise QueryExecutionError(
        f"Predicate reference '{ref}' points to missing path entity "
        f"{entity_type}:{entity_id}"
    )


def resolve_path(value: Any, parts: list[str]) -> Any:
    """Resolve a dotted path through Pydantic models and dict values."""
    current = value
    for part in parts:
        if current is _MISSING:
            return _MISSING
        if isinstance(current, BaseModel):
            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)
            continue
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        return _MISSING
    return current


def is_missing_path(value: Any) -> bool:
    """Return whether a resolved path value represents a missing path."""
    return value is _MISSING


def resolve_query_predicate_value_type(
    config: CoreConfig,
    context: PredicateContext,
    scope: str,
    field_path: list[str],
    left: Any,
    expected: Any,
) -> PredicateValueType | None:
    """Resolve the typed comparison mode for a query predicate."""
    declared_type = _declared_predicate_property_type(config, context, scope, field_path)
    if declared_type in {"date", "datetime"}:
        return declared_type
    return infer_predicate_value_type(left, expected)


def _declared_predicate_property_type(
    config: CoreConfig,
    context: PredicateContext,
    scope: str,
    field_path: list[str],
) -> PredicateValueType | None:
    if len(field_path) < 2 or field_path[0] != "properties":
        return None

    property_name = field_path[1]
    if scope == "edge":
        return _relationship_property_type(
            config,
            context.edge.relationship_type,
            property_name,
        )

    value = _scope_value(context, scope)
    if isinstance(value, EntityInstance):
        return _entity_property_type(config, value.entity_type, property_name)
    return None


def _relationship_property_type(
    config: CoreConfig,
    relationship_type: str,
    property_name: str,
) -> PredicateValueType | None:
    for relationship in config.relationships:
        if relationship.name == relationship_type or relationship.reverse_name == relationship_type:
            prop = relationship.properties.get(property_name)
            return _temporal_property_value_type(prop.type) if prop is not None else None
    return None


def _entity_property_type(
    config: CoreConfig,
    entity_type: str,
    property_name: str,
) -> PredicateValueType | None:
    entity_schema = config.entity_types.get(entity_type)
    if entity_schema is None:
        return None
    prop = entity_schema.properties.get(property_name)
    return _temporal_property_value_type(prop.type) if prop is not None else None


def _temporal_property_value_type(property_type: str) -> PredicateValueType | None:
    if property_type == "date":
        return "date"
    if property_type == "datetime":
        return "datetime"
    return None


def _evaluate_predicate_operator(
    config: CoreConfig,
    context: PredicateContext,
    path: str,
    scope: str,
    field_path: list[str],
    left: Any,
    operator: str,
    expected: Any,
) -> bool:
    if operator == "exists":
        exists = left is not _MISSING
        return exists is expected
    if left is _MISSING:
        return False
    if operator in {"contains", "icontains"}:
        if not isinstance(left, str) or not isinstance(expected, str):
            raise QueryExecutionError(
                f"Predicate operator '{operator}' for '{path}' requires string values"
            )
        if operator == "contains":
            return expected in left
        return expected.casefold() in left.casefold()
    if operator in {"in", "not_in"}:
        if not isinstance(expected, list | tuple | set | frozenset):
            raise QueryExecutionError(
                f"Predicate operator '{operator}' for '{path}' requires a list value"
            )
        matched = any(
            _evaluate_comparison(
                config,
                context,
                path,
                scope,
                field_path,
                left,
                "eq",
                item,
            )
            for item in expected
        )
        return matched if operator == "in" else not matched
    return _evaluate_comparison(
        config,
        context,
        path,
        scope,
        field_path,
        left,
        operator,
        expected,
    )


def _evaluate_comparison(
    config: CoreConfig,
    context: PredicateContext,
    path: str,
    scope: str,
    field_path: list[str],
    left: Any,
    operator: str,
    expected: Any,
) -> bool:
    value_type = resolve_query_predicate_value_type(
        config,
        context,
        scope,
        field_path,
        left,
        expected,
    )
    try:
        return evaluate_typed_comparison(
            left,
            operator,
            expected,
            value_type=value_type,
            invalid="raise",
        )
    except PredicateCoercionError as exc:
        raise QueryExecutionError(
            f"Invalid {exc.value_type} predicate value for '{path}': {exc.value!r}"
        ) from exc


__all__ = [
    "QUERY_PREDICATE_SCOPES",
    "PredicateContext",
    "build_predicate_context",
    "evaluate_query_predicates",
    "evaluate_related_predicate",
    "iter_step_relationships",
    "is_missing_path",
    "query_filter_summary",
    "related_edge_exists",
    "resolve_path",
    "resolve_query_predicate_value_type",
    "segment_endpoint_entities",
]
