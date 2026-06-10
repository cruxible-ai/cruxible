"""Config-defined mutation guard evaluation for direct graph writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from cruxible_core.config.property_validation import (
    entity_properties_with_identity,
    normalize_value,
)
from cruxible_core.config.schema import CoreConfig, MutationGuardSchema
from cruxible_core.errors import DataValidationError
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.operations import ValidatedEntity
from cruxible_core.graph.types import EntityInstance
from cruxible_core.query.engine import execute_query

_MISSING = object()


@dataclass(frozen=True)
class _GuardEntityContext:
    current: EntityInstance | None
    proposed: EntityInstance
    old_value: Any
    new_value: Any


def mutation_guard_errors(
    config: CoreConfig,
    *,
    current_graph: EntityGraph,
    proposed_graph: EntityGraph,
    entities: Sequence[ValidatedEntity],
) -> list[str]:
    """Return mutation guard errors for proposed entity writes (creates and updates)."""
    if not config.mutation_guards:
        return []

    errors: list[str] = []
    for entity in entities:
        current = current_graph.get_entity(
            entity.entity.entity_type,
            entity.entity.entity_id,
        )
        proposed = proposed_graph.get_entity(
            entity.entity.entity_type,
            entity.entity.entity_id,
        )
        if proposed is None:
            continue
        for guard in config.mutation_guards:
            context = _matching_guard_context(config, guard, entity, current, proposed)
            if context is None:
                continue
            if not _guard_condition_passes(config, guard, proposed_graph, context):
                errors.append(_guard_error_message(guard, entity.entity, context))
    return errors


def validate_mutation_guards(
    config: CoreConfig,
    *,
    current_graph: EntityGraph,
    proposed_graph: EntityGraph,
    entities: Sequence[ValidatedEntity],
) -> None:
    """Raise DataValidationError when any proposed entity write violates a guard."""
    errors = mutation_guard_errors(
        config,
        current_graph=current_graph,
        proposed_graph=proposed_graph,
        entities=entities,
    )
    if errors:
        raise DataValidationError(
            f"Mutation guard validation failed with {len(errors)} error(s)",
            errors=errors,
        )


def _matching_guard_context(
    config: CoreConfig,
    guard: MutationGuardSchema,
    validated: ValidatedEntity,
    current: EntityInstance | None,
    proposed: EntityInstance,
) -> _GuardEntityContext | None:
    entity = validated.entity
    if guard.entity_type != entity.entity_type:
        return None
    if guard.property not in entity.properties:
        return None

    property_schema = config.entity_types[guard.entity_type].properties[guard.property]
    guarded_value = normalize_value(guard.new_value, property_schema, config)
    old_value = current.properties.get(guard.property, _MISSING) if current else _MISSING
    new_value = proposed.properties.get(guard.property, _MISSING)
    if new_value != guarded_value:
        return None
    if old_value == new_value:
        return None
    return _GuardEntityContext(
        current=current,
        proposed=proposed,
        old_value=old_value,
        new_value=new_value,
    )


def _guard_condition_passes(
    config: CoreConfig,
    guard: MutationGuardSchema,
    graph: EntityGraph,
    context: _GuardEntityContext,
) -> bool:
    condition = guard.condition
    params = _resolve_guard_params(config, condition.params, context)
    result = execute_query(config, graph, condition.query_name, params)
    count = result.total_results if result.total_results is not None else len(result.results)
    if condition.min_count is not None and count < condition.min_count:
        return False
    if condition.max_count is not None and count > condition.max_count:
        return False
    return True


def _resolve_guard_params(
    config: CoreConfig,
    params: Mapping[str, Any],
    context: _GuardEntityContext,
) -> dict[str, Any]:
    scopes = {
        "entity": _entity_view(config, context.proposed),
        # On creates there is no current entity; $current.* refs then fail
        # closed via the missing-reference error.
        "current": None if context.current is None else _entity_view(config, context.current),
        "new_value": context.new_value,
        "old_value": None if context.old_value is _MISSING else context.old_value,
    }
    return {key: _resolve_guard_param_value(value, scopes) for key, value in params.items()}


def _resolve_guard_param_value(
    value: Any,
    scopes: Mapping[str, Any],
) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return _resolve_guard_ref(value, scopes)
    if isinstance(value, list):
        return [_resolve_guard_param_value(item, scopes) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_guard_param_value(item, scopes) for key, item in value.items()}
    return value


def _resolve_guard_ref(
    ref: str,
    scopes: Mapping[str, Any],
) -> Any:
    raw = ref[1:]
    if raw in {"new_value", "old_value"}:
        return scopes[raw]
    scope, sep, path = raw.partition(".")
    if not sep or not path or scope not in {"entity", "current"}:
        raise DataValidationError(f"Unsupported mutation guard param reference '{ref}'")
    value = _resolve_path(scopes[scope], path.split("."))
    if value is _MISSING:
        raise DataValidationError(f"Missing mutation guard param reference '{ref}'")
    return value


def _resolve_path(value: Any, parts: Sequence[str]) -> Any:
    current = value
    for part in parts:
        if current is _MISSING:
            return _MISSING
        if isinstance(current, BaseModel):
            if not hasattr(current, part):
                return _MISSING
            current = getattr(current, part)
            continue
        if isinstance(current, Mapping):
            if part not in current:
                return _MISSING
            current = current[part]
            continue
        return _MISSING
    return current


def _entity_view(config: CoreConfig, entity: EntityInstance) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "entity_id": entity.entity_id,
        "properties": entity_properties_with_identity(
            config,
            entity.entity_type,
            entity.entity_id,
            entity.properties,
        ),
    }


def _guard_error_message(
    guard: MutationGuardSchema,
    entity: EntityInstance,
    context: _GuardEntityContext,
) -> str:
    message = guard.message or "mutation guard condition failed"
    return (
        f"Mutation guard '{guard.name}' rejected write "
        f"{entity.entity_type}:{entity.entity_id} "
        f"{guard.property}={context.new_value!r}: {message}"
    )


__all__ = [
    "mutation_guard_errors",
    "validate_mutation_guards",
]
