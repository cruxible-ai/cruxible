"""cruxible-client public contract snapshot and compatibility helpers."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from cruxible_client import contracts
from cruxible_core.primitives import canonical_json

CONTRACT_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class CompatibilityReport:
    """Compatibility comparison result for two contract manifests."""

    breaking: tuple[str, ...]
    compatible: tuple[str, ...]

    @property
    def is_compatible(self) -> bool:
        return not self.breaking


def generate_contract_manifest() -> dict[str, Any]:
    """Generate a deterministic manifest for public cruxible-client contracts."""
    return {
        "manifest_version": CONTRACT_MANIFEST_VERSION,
        "module": contracts.__name__,
        "literal_aliases": _literal_aliases(),
        "models": _public_models(),
    }


def load_contract_snapshot(path: Path) -> dict[str, Any]:
    """Load a checked-in contract snapshot."""
    decoded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"Contract snapshot must contain a JSON object: {path}")
    if not all(isinstance(key, str) for key in decoded):
        raise ValueError(f"Contract snapshot keys must be strings: {path}")
    return cast(dict[str, Any], decoded)


def write_contract_snapshot(path: Path) -> None:
    """Write the current contract manifest with stable formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_stable_json(generate_contract_manifest()) + "\n", encoding="utf-8")


def compare_contract_manifests(
    old: dict[str, Any],
    new: dict[str, Any],
) -> CompatibilityReport:
    """Compare two contract manifests and report pragmatic breaking changes."""
    breaking: list[str] = []
    compatible: list[str] = []
    old_models = old.get("models", {})
    new_models = new.get("models", {})

    for model_name in sorted(set(old_models) - set(new_models)):
        breaking.append(f"Removed model {model_name}")
    for model_name in sorted(set(new_models) - set(old_models)):
        compatible.append(f"Added model {model_name}")
    for model_name in sorted(set(old_models) & set(new_models)):
        _compare_model(
            model_name,
            old_models[model_name],
            new_models[model_name],
            breaking,
            compatible,
        )

    old_aliases = old.get("literal_aliases", {})
    new_aliases = new.get("literal_aliases", {})
    for alias_name in sorted(set(old_aliases) - set(new_aliases)):
        breaking.append(f"Removed Literal alias {alias_name}")
    for alias_name in sorted(set(old_aliases) & set(new_aliases)):
        old_values = set(old_aliases[alias_name].get("values", ()))
        new_values = set(new_aliases[alias_name].get("values", ()))
        removed = sorted(old_values - new_values)
        added = sorted(new_values - old_values)
        if removed:
            breaking.append(f"Removed Literal value(s) from {alias_name}: {removed}")
        if added:
            compatible.append(f"Added Literal value(s) to {alias_name}: {added}")

    return CompatibilityReport(tuple(breaking), tuple(compatible))


def _public_models() -> dict[str, Any]:
    models: dict[str, Any] = {}
    for name, value in sorted(vars(contracts).items()):
        if name.startswith("_"):
            continue
        if not inspect.isclass(value):
            continue
        if not issubclass(value, BaseModel) or value.__module__ != contracts.__name__:
            continue
        value.model_rebuild()
        schema = _json_roundtrip(value.model_json_schema(ref_template="#/$defs/{model}"))
        required_fields = [
            field_name
            for field_name, field in sorted(value.model_fields.items())
            if field.is_required()
        ]
        models[name] = {
            "fields": _model_fields(value, schema),
            "json_schema": schema,
            "required_fields": required_fields,
        }
    return models


def _model_fields(model: type[BaseModel], schema: dict[str, Any]) -> dict[str, Any]:
    properties = schema.get("properties", {})
    fields: dict[str, Any] = {}
    for field_name, field in sorted(model.model_fields.items()):
        json_name = field.alias or field_name
        default_payload: dict[str, Any] = {
            "has_default": False,
            "has_default_factory": field.default_factory is not None,
        }
        if not field.is_required() and field.default_factory is None:
            default_payload["has_default"] = field.default is not PydanticUndefined
            if field.default is not PydanticUndefined:
                try:
                    default_payload["default"] = _json_roundtrip(field.default)
                    default_payload["default_json_serializable"] = True
                except (TypeError, ValueError):
                    default_payload["default_json_serializable"] = False
        fields[field_name] = {
            **default_payload,
            "json_name": json_name,
            "required": field.is_required(),
            "schema": _json_roundtrip(properties.get(json_name, {})),
        }
    return fields


def _literal_aliases() -> dict[str, Any]:
    aliases: dict[str, Any] = {}
    for name, value in sorted(vars(contracts).items()):
        if name.startswith("_") or get_origin(value) is not Literal:
            continue
        aliases[name] = {"values": [_json_roundtrip(item) for item in get_args(value)]}
    return aliases


def _compare_model(
    model_name: str,
    old_model: dict[str, Any],
    new_model: dict[str, Any],
    breaking: list[str],
    compatible: list[str],
) -> None:
    old_fields = old_model.get("fields", {})
    new_fields = new_model.get("fields", {})
    for field_name in sorted(set(old_fields) - set(new_fields)):
        breaking.append(f"Removed field {model_name}.{field_name}")
    for field_name in sorted(set(new_fields) - set(old_fields)):
        if new_fields[field_name].get("required", False):
            breaking.append(f"Added required field {model_name}.{field_name}")
        else:
            compatible.append(f"Added optional field {model_name}.{field_name}")
    for field_name in sorted(set(old_fields) & set(new_fields)):
        _compare_field(
            model_name,
            field_name,
            old_fields[field_name],
            new_fields[field_name],
            breaking,
            compatible,
        )


def _compare_field(
    model_name: str,
    field_name: str,
    old_field: dict[str, Any],
    new_field: dict[str, Any],
    breaking: list[str],
    compatible: list[str],
) -> None:
    label = f"{model_name}.{field_name}"
    if not old_field.get("required", False) and new_field.get("required", False):
        breaking.append(f"Field became required {label}")

    old_types = _schema_types(old_field.get("schema", {}))
    new_types = _schema_types(new_field.get("schema", {}))
    if old_types and new_types:
        removed_types = {
            old_type
            for old_type in old_types
            if not _type_is_still_accepted(old_type, new_types)
        }
        added_types = {
            new_type
            for new_type in new_types
            if not _type_was_already_accepted(new_type, old_types)
        }
        if removed_types:
            breaking.append(f"Narrowed field type {label}: removed {sorted(removed_types)}")
        if added_types:
            compatible.append(f"Widened field type {label}: added {sorted(added_types)}")

    old_values = _schema_value_tokens(old_field.get("schema", {}))
    new_values = _schema_value_tokens(new_field.get("schema", {}))
    if old_values and new_values:
        removed_values = _json_token_values(old_values - new_values)
        added_values = _json_token_values(new_values - old_values)
        if removed_values:
            breaking.append(f"Removed enum value(s) from {label}: {removed_values}")
        if added_values:
            compatible.append(f"Added enum value(s) to {label}: {added_values}")


def _schema_types(schema: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types.add(raw_type)
    elif isinstance(raw_type, list):
        types.update(item for item in raw_type if isinstance(item, str))
    if "enum" in schema:
        types.update(_json_value_type(value) for value in schema["enum"])
    if "const" in schema:
        types.add(_json_value_type(schema["const"]))
    for key in ("anyOf", "oneOf", "allOf"):
        children = schema.get(key, ())
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    types.update(_schema_types(child))
    return types


def _schema_value_tokens(schema: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    if isinstance(schema.get("enum"), list):
        values.update(canonical_json(value) for value in schema["enum"])
    if "const" in schema:
        values.add(canonical_json(schema["const"]))
    for key in ("anyOf", "oneOf", "allOf"):
        children = schema.get(key, ())
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    values.update(_schema_value_tokens(child))
    return values


def _json_token_values(tokens: set[str]) -> list[Any]:
    return [json.loads(token) for token in sorted(tokens)]


def _type_is_still_accepted(old_type: str, new_types: set[str]) -> bool:
    return old_type in new_types or (old_type == "integer" and "number" in new_types)


def _type_was_already_accepted(new_type: str, old_types: set[str]) -> bool:
    return new_type in old_types or (new_type == "number" and "integer" in old_types)


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _json_roundtrip(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _stable_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
