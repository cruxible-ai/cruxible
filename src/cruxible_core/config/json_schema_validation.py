"""Small Cruxible-owned validator for contract JSON schema subsets."""

from __future__ import annotations

import json
from typing import Any, Mapping

from cruxible_core.config.property_validation import enum_ref_values

SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "items",
        "properties",
        "required",
        "enum",
        "enum_ref",
    }
)
SUPPORTED_TYPES = frozenset(
    {
        "object",
        "array",
        "string",
        "number",
        "integer",
        "boolean",
        "null",
    }
)


def validate_json_schema_shape(
    schema_node: Mapping[str, Any],
    enums: Mapping[str, Any],
    path: str,
) -> None:
    """Validate the supported nested JSON schema subset at config-load time."""
    _validate_schema_node(schema_node, enums, path)


def validate_value_against_json_schema(
    value: Any,
    schema_node: Mapping[str, Any],
    enums: Mapping[str, Any],
    path: str,
) -> None:
    """Validate a runtime value against the supported nested JSON schema subset."""
    _validate_runtime_node(value, schema_node, enums, path)


def _validate_schema_node(
    schema_node: Mapping[str, Any],
    enums: Mapping[str, Any],
    path: str,
) -> None:
    if not isinstance(schema_node, dict):
        raise ValueError(f"{path}: schema node must be an object")

    extra = sorted(set(schema_node) - SUPPORTED_KEYWORDS)
    if extra:
        allowed = ", ".join(sorted(SUPPORTED_KEYWORDS))
        found = ", ".join(extra)
        raise ValueError(f"{path}: unsupported json_schema keyword(s): {found}; allowed: {allowed}")

    type_name = schema_node.get("type")
    if type_name is not None:
        if not isinstance(type_name, str) or type_name not in SUPPORTED_TYPES:
            allowed = ", ".join(sorted(SUPPORTED_TYPES))
            raise ValueError(f"{path}.type: expected one of: {allowed}")

    if "enum" in schema_node and "enum_ref" in schema_node:
        raise ValueError(f"{path}: enum and enum_ref are mutually exclusive")

    if "enum" in schema_node:
        _validate_inline_enum(schema_node["enum"], path)

    enum_ref = schema_node.get("enum_ref")
    if enum_ref is not None:
        if not isinstance(enum_ref, str) or not enum_ref.strip():
            raise ValueError(f"{path}.enum_ref: must be a non-empty string")
        try:
            enum_ref_values(enums, enum_ref)
        except ValueError as exc:
            raise ValueError(f"{path}.enum_ref: {exc}") from exc
        if type_name not in (None, "string"):
            raise ValueError(f"{path}.enum_ref: enum_ref is only supported for string values")

    properties = schema_node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties: must be an object")
        if type_name not in (None, "object"):
            raise ValueError(f"{path}.properties: properties is only supported for object schemas")
        for prop_name, child_schema in properties.items():
            if not isinstance(prop_name, str) or not prop_name.strip():
                raise ValueError(f"{path}.properties: property names must be non-empty strings")
            if not isinstance(child_schema, dict):
                raise ValueError(f"{path}.properties.{prop_name}: schema node must be an object")
            _validate_schema_node(child_schema, enums, f"{path}.properties.{prop_name}")

    required = schema_node.get("required")
    if required is not None:
        if type_name not in (None, "object"):
            raise ValueError(f"{path}.required: required is only supported for object schemas")
        if not isinstance(required, list):
            raise ValueError(f"{path}.required: must be a list of property names")
        if any(not isinstance(item, str) or not item.strip() for item in required):
            raise ValueError(f"{path}.required: entries must be non-empty strings")
        if len(set(required)) != len(required):
            raise ValueError(f"{path}.required: entries must be unique")

    items = schema_node.get("items")
    if items is not None:
        if type_name not in (None, "array"):
            raise ValueError(f"{path}.items: items is only supported for array schemas")
        if not isinstance(items, dict):
            raise ValueError(f"{path}.items: must be an object")
        _validate_schema_node(items, enums, f"{path}.items")


def _validate_inline_enum(values: Any, path: str) -> None:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{path}.enum: must be a non-empty list")
    seen: set[str] = set()
    for index, value in enumerate(values):
        try:
            key = json.dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}.enum[{index}]: must be JSON-serializable") from exc
        if key in seen:
            raise ValueError(f"{path}.enum: values must be unique")
        seen.add(key)


def _validate_runtime_node(
    value: Any,
    schema_node: Mapping[str, Any],
    enums: Mapping[str, Any],
    path: str,
) -> None:
    type_name = schema_node.get("type")
    if isinstance(type_name, str):
        _validate_runtime_type(value, type_name, path)

    if "enum" in schema_node and value not in schema_node["enum"]:
        allowed = ", ".join(str(item) for item in schema_node["enum"])
        raise ValueError(f"{path}: value must be one of: {allowed}")

    enum_ref = schema_node.get("enum_ref")
    if isinstance(enum_ref, str):
        allowed_values = enum_ref_values(enums, enum_ref)
        if value not in allowed_values:
            allowed = ", ".join(allowed_values)
            raise ValueError(f"{path}: value must be one of enum_ref '{enum_ref}': {allowed}")

    if value is None:
        return

    if isinstance(value, dict):
        required = schema_node.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        if isinstance(required, list):
            for prop_name in required:
                if prop_name not in value:
                    raise ValueError(f"{path}: missing required property '{prop_name}'")

        properties = schema_node.get("properties")
        if isinstance(properties, dict):
            for prop_name, child_schema in properties.items():
                if prop_name in value:
                    if value[prop_name] is None and prop_name not in required_names:
                        continue
                    _validate_runtime_node(
                        value[prop_name],
                        child_schema,
                        enums,
                        f"{path}.{prop_name}",
                    )

    if isinstance(value, list):
        items = schema_node.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_runtime_node(item, items, enums, f"{path}[{index}]")


def _validate_runtime_type(value: Any, type_name: str, path: str) -> None:
    if type_name == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: must be an object")
        return
    if type_name == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path}: must be an array")
        return
    if type_name == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path}: must be a string")
        return
    if type_name == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: must be a number")
        return
    if type_name == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: must be an integer")
        return
    if type_name == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: must be a boolean")
        return
    if type_name == "null" and value is not None:
        raise ValueError(f"{path}: must be null")
