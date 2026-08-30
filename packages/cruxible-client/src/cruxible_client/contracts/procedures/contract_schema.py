"""Playbill-owned schema for Contracts carried by Procedure-v2 owners.

This is intentionally the narrow, byte-compatible Contract subset, not an
import of the legacy config schema. Owner-carried Contracts are accepted
Playbill artifacts and the served Playbill closure may not initialize the
mutable-core config donor merely to validate their fields.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cruxible_client.contracts.primitives import canonical_json

PropertyType = Literal[
    "string",
    "int",
    "integer",
    "float",
    "number",
    "bool",
    "date",
    "datetime",
    "json",
    "list",
]


class PropertySchema(BaseModel):
    """One owner-carried Contract field, byte-compatible with the deferred family."""

    type: PropertyType = "string"
    primary_key: bool = False
    indexed: bool = False
    optional: bool = False
    required: bool | None = Field(default=None, exclude=True)
    default: Any | None = None
    enum: list[Any] | None = None
    enum_ref: str | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    item_fields: dict[str, PropertySchema] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_schema_usage(self) -> "PropertySchema":
        if self.required is not None:
            required_optional = not self.required
            if "optional" in self.model_fields_set and self.optional != required_optional:
                raise ValueError("required and optional are conflicting aliases")
            self.optional = required_optional
        if self.primary_key and self.optional:
            raise ValueError("primary_key properties may not be optional")
        if self.type == "list" and self.item_fields is None:
            raise ValueError("list properties require item_fields")
        if self.type != "list" and self.item_fields is not None:
            raise ValueError("item_fields is only allowed on properties with type 'list'")
        if self.type == "list" and self.primary_key:
            raise ValueError("list properties may not be primary_key")
        if self.enum is not None and self.enum_ref is not None:
            raise ValueError("enum and enum_ref are mutually exclusive")
        if self.enum_ref is not None and self.type != "string":
            raise ValueError("enum_ref is only allowed on properties with type 'string'")
        if self.enum is not None:
            if not self.enum:
                raise ValueError("enum values must not be empty")
            seen: set[str] = set()
            for index, value in enumerate(self.enum):
                try:
                    key = canonical_json(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"enum value at index {index} must be JSON-serializable"
                    ) from exc
                if key in seen:
                    raise ValueError("enum values must be unique")
                seen.add(key)
            if self.default is not None and self.default not in self.enum:
                allowed = ", ".join(str(value) for value in self.enum)
                raise ValueError(f"default must be one of enum values: {allowed}")
        if self.json_schema is None:
            return self
        if self.type != "json":
            raise ValueError("json_schema is only allowed on properties with type 'json'")
        try:
            canonical_json(self.json_schema)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"json_schema must be JSON-serializable: {exc}") from exc
        return self


class ContractSchema(BaseModel):
    """Typed input or output schema carried inside one Procedure-v2 envelope."""

    description: str | None = None
    fields: dict[str, PropertySchema]
    allow_extra: bool = False

    @model_validator(mode="after")
    def validate_explicit_field_types(self) -> "ContractSchema":
        for name, prop in self.fields.items():
            if "type" not in prop.model_fields_set:
                raise ValueError(f"Contract field '{name}' must define type explicitly")
        return self


__all__ = ["ContractSchema", "PropertySchema", "PropertyType"]
