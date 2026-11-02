"""Immutable records validated by governed Contracts and the native field grammar."""

from __future__ import annotations

import keyword
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.provider_operations import validate_operation_value
from cruxible_client.contracts.shapes import (
    Contract,
    ContractResolver,
    ListType,
    NestedType,
    contract_digest,
)


class RecordSchemaError(ValueError):
    """The named Contract needed by a record is unavailable."""


def record_field_names(schema: Contract) -> dict[str, str]:
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
    _schema: Contract
    _data: dict[str, CanonicalValue]
    _resolve: ContractResolver

    def __init__(
        self, schema: Contract, value: Mapping[str, object], resolve: ContractResolver | None = None
    ) -> None:
        resolver = resolve or (lambda _ref: None)
        raw = normalize_canonical(value)
        validate_operation_value(raw, schema, resolve=resolver)
        assert isinstance(raw, dict)
        object.__setattr__(self, "_schema", schema.model_copy(deep=True))
        object.__setattr__(self, "_data", deepcopy(raw))
        object.__setattr__(self, "_resolve", resolver)

    @property
    def schema_digest(self) -> str:
        return contract_digest(self._schema).tagged

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, name: str) -> CanonicalValue:
        return deepcopy(self._data[name])

    def __getattr__(self, name: str) -> Any:
        schema = object.__getattribute__(self, "_schema")
        wire = record_field_names(schema).get(name)
        if wire is None or wire not in self._data:
            raise AttributeError(f"undeclared or absent record field {name!r}")
        value = self._data[wire]
        field_type = self._schema.fields[wire].type
        if isinstance(field_type, NestedType) and isinstance(value, dict):
            nested = self._resolve(field_type.contract)
            if nested is not None:
                return Record(nested, value, self._resolve)
        if (
            isinstance(field_type, ListType)
            and isinstance(field_type.item, NestedType)
            and isinstance(value, list)
        ):
            nested = self._resolve(field_type.item.contract)
            if nested is not None:
                return tuple(
                    Record(nested, item, self._resolve) for item in value if isinstance(item, dict)
                )
        return deepcopy(value)

    def model_dump(self, *, mode: str = "python") -> dict[str, CanonicalValue]:
        if mode not in {"python", "json"}:
            raise ValueError(f"unknown serialization mode {mode!r}")
        return deepcopy(self._data)


@dataclass(frozen=True)
class RecordConstructor:
    _schema: Contract
    _omittable_fields: frozenset[str] = frozenset()
    _resolve: ContractResolver | None = None

    @property
    def schema(self) -> Contract:
        return self._schema.model_copy(deep=True)

    def __call__(self, **fields: object) -> Record:
        aliases = record_field_names(self._schema)
        data: dict[str, object] = {}
        for name, value in fields.items():
            wire = aliases.get(name, name)
            if wire in data:
                raise ValueError(f"two arguments supply field {wire!r}")
            data[wire] = value
        return self.from_wire(data)

    def from_wire(self, value: object) -> Record:
        if not isinstance(value, Mapping):
            raise TypeError("record value must be an object")
        schema = (
            self._schema.model_copy(
                update={
                    "fields": {
                        name: field.model_copy(update={"optional": True})
                        if name in self._omittable_fields and name not in value
                        else field
                        for name, field in self._schema.fields.items()
                    }
                }
            )
            if self._omittable_fields
            else self._schema
        )
        return Record(schema, value, self._resolve)

    def __getattr__(self, name: str) -> RecordConstructor:
        wire = record_field_names(self._schema).get(name)
        if wire is None:
            raise AttributeError(f"undeclared constructor field {name!r}")
        field_type = self._schema.fields[wire].type
        if isinstance(field_type, ListType):
            field_type = field_type.item
        if isinstance(field_type, NestedType) and self._resolve is not None:
            nested = self._resolve(field_type.contract)
            if nested is not None:
                return RecordConstructor(nested, _resolve=self._resolve)
        raise AttributeError(f"{wire!r} has no declared nested Contract")
