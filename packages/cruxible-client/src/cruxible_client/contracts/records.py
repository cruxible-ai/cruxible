"""Contract-derived authoring values; canonical wire data stays unchanged.

These objects are conveniences over retained schemas, never a second schema
registry. Frozen historical Contract validation stays in its original module.
The source frontend additionally refuses schemas it cannot represent faithfully.
"""

from __future__ import annotations

import keyword
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from cruxible_client.contracts.canonical import CanonicalValue, canonical_bytes, normalize_canonical
from cruxible_client.contracts.procedures.contract_schema import (
    ContractSchema,
    PropertySchema,
    PropertyType,
)


class RecordSchemaError(ValueError):
    """A schema cannot be represented by this source-language version."""


def _check_json_schema(schema: Mapping[str, object], path: str) -> None:
    supported = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "description",
        "title",
    }
    unknown = set(schema) - supported
    if unknown:
        raise RecordSchemaError(f"{path}: unsupported schema keywords: {sorted(unknown)}")
    if schema.get("type") not in {None, "string", "integer", "boolean", "null", "object", "array"}:
        raise RecordSchemaError(f"{path}: unsupported canonical schema type {schema.get('type')!r}")
    extra = schema.get("additionalProperties", True)
    if not isinstance(extra, bool):
        raise RecordSchemaError(f"{path}: schema-valued additionalProperties is unsupported")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise RecordSchemaError(f"{path}: properties must be an object")
    for name, child in properties.items():
        if not isinstance(name, str) or not isinstance(child, Mapping):
            raise RecordSchemaError(f"{path}: invalid property schema")
        _check_json_schema(child, f"{path}.{name}")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise RecordSchemaError(f"{path}: tuple-style items are unsupported")
        _check_json_schema(items, f"{path}[]")


def _json_record_schema(schema: Mapping[str, object], path: str) -> ContractSchema:
    _check_json_schema(schema, path)
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
        raise RecordSchemaError(f"{path}: no declared object fields")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise RecordSchemaError(f"{path}: required must be a list of field names")
    properties = cast(Mapping[str, Mapping[str, Any]], schema["properties"])
    if not set(required).issubset(properties):
        raise RecordSchemaError(f"{path}: required names an undeclared property")
    fields: dict[str, PropertySchema] = {}
    kinds: dict[str, PropertyType] = {"string": "string", "integer": "int", "boolean": "bool"}
    for name, child in properties.items():
        kind = child.get("type")
        # Retain the full JSON schema for nested/constraint-rich values. Runtime
        # validation below uses the same frozen literal-schema evaluator.
        simple = set(child) <= {"type", "enum", "description", "title"}
        if kind in kinds and simple:
            fields[name] = PropertySchema(
                type=kinds[kind], optional=name not in required, enum=child.get("enum")
            )
        else:
            fields[name] = PropertySchema(
                type="json", optional=name not in required, json_schema=dict(child)
            )
    return ContractSchema(fields=fields, allow_extra=bool(schema.get("additionalProperties", True)))


def _check_schema(schema: ContractSchema) -> None:
    for name, field in schema.fields.items():
        if field.enum_ref is not None or field.type in {"float", "number"}:
            raise RecordSchemaError(f"{name}: field has no supported canonical source type")
        if field.item_fields is not None:
            _check_schema(ContractSchema(fields=field.item_fields))
        if field.json_schema is not None:
            _check_json_schema(field.json_schema, name)


def _validate_nested(schema: ContractSchema, value: Mapping[str, object], prefix: str = "") -> None:
    from cruxible_client.contracts.claims import _validate_literal_schema
    from cruxible_client.contracts.procedures.contracts import ProcedureContractValidationError

    for name, field in schema.fields.items():
        if name not in value:
            continue
        item = value[name]
        path = f"{prefix}.{name}" if prefix else name
        if field.json_schema is not None and not _validate_literal_schema(item, field.json_schema):
            raise ProcedureContractValidationError(
                f"{path}: value does not satisfy its declared JSON schema", field_path=path
            )
        if field.enum is not None and canonical_bytes(item) not in {
            canonical_bytes(member) for member in field.enum
        }:
            raise ProcedureContractValidationError(
                f"{path}: value is outside the declared enum", field_path=path
            )
        if field.item_fields is not None and isinstance(item, list):
            nested = ContractSchema(fields=field.item_fields)
            for index, member in enumerate(item):
                _validate_nested(nested, cast(Mapping[str, object], member), f"{path}[{index}]")


def record_field_names(schema: ContractSchema) -> dict[str, str]:
    """Python alias -> exact wire name, shared by constructors, values and stubs."""
    reserved = set(dir(Record)) | set(dir(RecordConstructor))
    occupied = set(schema.fields)
    aliases: dict[str, str] = {}
    for name in sorted(schema.fields):
        alias = name
        if (
            not name.isidentifier()
            or keyword.iskeyword(name)
            or name.startswith("_")
            or name in reserved
        ):
            alias = "field_" + name.encode("utf-8").hex()
            while alias in occupied or alias in reserved:
                alias += "_"
        occupied.add(alias)
        aliases[alias] = name
    return aliases


@dataclass(frozen=True, init=False)
class Record(Mapping[str, CanonicalValue]):
    """Immutable validated record with declared field access and exact wire keys."""

    _schema: ContractSchema
    _data: dict[str, CanonicalValue]

    def __init__(self, schema: ContractSchema, value: Mapping[str, object]) -> None:
        from cruxible_client.contracts.procedures.contracts import validate_contract_schema

        sealed_schema = schema.model_copy(deep=True)
        _check_schema(sealed_schema)
        # Normalize nested Records through the existing canonical Mapping path.
        raw = normalize_canonical(value)
        parsed = validate_contract_schema(sealed_schema, raw)
        assert isinstance(parsed, dict)
        _validate_nested(sealed_schema, parsed)
        object.__setattr__(self, "_schema", sealed_schema)
        object.__setattr__(self, "_data", deepcopy(parsed))

    @property
    def schema_digest(self) -> str:
        return "sha256:" + sha256(canonical_bytes(self._schema.model_dump(mode="json"))).hexdigest()

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, name: str) -> CanonicalValue:
        # An exposed list/open JSON value cannot mutate the retained record.
        return deepcopy(self._data[name])

    def __getattr__(self, name: str) -> Any:
        wire = record_field_names(self._schema).get(name)
        if wire is None:
            raise AttributeError(f"undeclared record field {name!r}")
        if wire not in self._data:
            raise AttributeError(f"optional record field {wire!r} is absent")
        value = self._data[wire]
        field = self._schema.fields[wire]
        if isinstance(value, dict) and field.json_schema is not None:
            if field.json_schema.get("type") == "object" and "properties" in field.json_schema:
                return Record(_json_record_schema(field.json_schema, wire), value)
        if isinstance(value, list) and field.item_fields is not None:
            schema = ContractSchema(fields=field.item_fields)
            return tuple(Record(schema, cast(Mapping[str, object], item)) for item in value)
        return deepcopy(value)

    def model_dump(self, *, mode: str = "python") -> dict[str, CanonicalValue]:
        if mode not in {"python", "json"}:
            raise ValueError(f"unknown serialization mode {mode!r}")
        return deepcopy(self._data)


@dataclass(frozen=True)
class RecordConstructor:
    """A schema handle usable as .value(...), .input(...), or .parameters(...)."""

    _schema: ContractSchema

    def __post_init__(self) -> None:
        object.__setattr__(self, "_schema", self._schema.model_copy(deep=True))
        _check_schema(self._schema)

    @property
    def schema(self) -> ContractSchema:
        return self._schema.model_copy(deep=True)

    def __call__(self, **fields: object) -> Record:
        aliases = record_field_names(self._schema)
        data: dict[str, object] = {}
        for name, value in fields.items():
            wire = aliases.get(name, name)
            if wire in data:
                raise ValueError(f"two arguments supply field {wire!r}")
            data[wire] = value
        return Record(self._schema, data)

    def from_wire(self, value: object) -> Record:
        """Validate a service/provider result without changing its field names."""
        if not isinstance(value, Mapping):
            raise TypeError("record value must be an object")
        return Record(self._schema, value)

    def __getattr__(self, name: str) -> RecordConstructor:
        wire = record_field_names(self._schema).get(name)
        if wire is None:
            raise AttributeError(f"undeclared constructor field {name!r}")
        field = self._schema.fields[wire]
        if field.item_fields is not None:
            return RecordConstructor(ContractSchema(fields=field.item_fields))
        if field.json_schema is not None:
            return RecordConstructor(_json_record_schema(field.json_schema, wire))
        raise AttributeError(f"{wire!r} has no declared record schema")
