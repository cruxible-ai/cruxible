"""Shared property-schema validation for contracts and graph writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping

from cruxible_core.config.schema import CoreConfig, PropertySchema
from cruxible_core.graph.types import USER_STRIPPED_PROPERTIES, EntityInstance
from cruxible_core.primitives import canonical_json


@dataclass(frozen=True)
class PropertyValidationResult:
    """Normalized property payload plus per-field validation errors."""

    properties: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def enum_ref_values(enums: Mapping[str, Any], enum_ref: str) -> list[str]:
    """Resolve shared enum_ref values from a config enum mapping."""
    enum_schema = enums.get(enum_ref)
    if enum_schema is None:
        raise ValueError(f"enum_ref '{enum_ref}' is not defined")
    return list(enum_schema.values)


def enum_values(config: CoreConfig, schema: PropertySchema) -> list[Any] | None:
    """Resolve inline enum values or enum_ref values for a property."""
    if schema.enum is not None:
        return list(schema.enum)
    if schema.enum_ref is not None:
        return enum_ref_values(config.enums, schema.enum_ref)
    return None


def normalize_value(value: Any, schema: PropertySchema, config: CoreConfig) -> Any:
    """Normalize one value to the configured property type."""
    type_name = schema.type

    if value is None:
        if schema.optional:
            return None
        raise ValueError("value may not be null")

    allowed_values = enum_values(config, schema)
    if allowed_values is not None and value not in allowed_values:
        allowed = ", ".join(str(item) for item in allowed_values)
        raise ValueError(f"value must be one of: {allowed}")

    if type_name == "string":
        if not isinstance(value, str):
            raise ValueError("must be a string")
        return value

    if type_name in {"int", "integer"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("must be an int")
        return value

    if type_name in {"float", "number"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a float")
        return float(value)

    if type_name == "bool":
        if not isinstance(value, bool):
            raise ValueError("must be a bool")
        return value

    if type_name == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("must be an ISO date string (YYYY-MM-DD)") from exc
            return value
        raise ValueError("must be an ISO date string")

    if type_name == "json":
        try:
            canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be JSON-serializable") from exc
        return value

    raise ValueError(f"unsupported property type '{type_name}'")


def validate_property_payload(
    config: CoreConfig,
    property_schemas: Mapping[str, PropertySchema],
    payload: Mapping[str, Any],
    *,
    require_required: bool,
    primary_key_name: str | None = None,
    entity_id: str | None = None,
    strip_system_properties: bool = False,
) -> PropertyValidationResult:
    """Validate and normalize a property payload against schema definitions."""
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    source = dict(payload)
    if strip_system_properties:
        for key in USER_STRIPPED_PROPERTIES:
            source.pop(key, None)

    if primary_key_name is not None and primary_key_name in source:
        supplied = source.pop(primary_key_name)
        if entity_id is not None and str(supplied) != str(entity_id):
            errors.append(
                f"property '{primary_key_name}': must match entity_id '{entity_id}'"
            )

    extra = sorted(set(source) - set(property_schemas))
    for prop_name in extra:
        errors.append(f"unexpected property '{prop_name}'")

    for prop_name, prop_schema in property_schemas.items():
        if prop_name == primary_key_name:
            continue
        if prop_name not in source:
            if require_required and prop_schema.default is not None:
                try:
                    normalized[prop_name] = normalize_value(
                        prop_schema.default, prop_schema, config
                    )
                except ValueError as exc:
                    errors.append(f"property '{prop_name}' default: {exc}")
            elif require_required and not prop_schema.optional:
                errors.append(f"missing required property '{prop_name}'")
            continue
        try:
            normalized[prop_name] = normalize_value(source[prop_name], prop_schema, config)
        except ValueError as exc:
            errors.append(f"property '{prop_name}': {exc}")

    return PropertyValidationResult(properties=normalized, errors=errors)


def entity_properties_with_identity(
    config: CoreConfig,
    entity_type: str,
    entity_id: str,
    properties: Mapping[str, Any],
) -> dict[str, Any]:
    """Return entity properties plus the schema primary-key value derived from entity_id."""
    result = dict(properties)
    entity_schema = config.get_entity_type(entity_type)
    if entity_schema is not None:
        primary_key = entity_schema.get_primary_key()
        if primary_key is not None:
            result.setdefault(primary_key, entity_id)
    return result


def entity_with_identity_properties(config: CoreConfig, entity: EntityInstance) -> EntityInstance:
    """Return an entity view with the schema primary-key value derived from entity_id."""
    return entity.model_copy(
        update={
            "properties": entity_properties_with_identity(
                config,
                entity.entity_type,
                entity.entity_id,
                entity.properties,
            )
        }
    )
