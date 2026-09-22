"""Typed schema views of existing query and acquisition records.

These describe data returned by the existing readers. They do not evaluate
queries or invent fields from sample values.
"""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema


def object_field(fields: dict[str, Any]) -> PropertySchema:
    return PropertySchema(
        type="json",
        json_schema={
            "type": "object",
            "properties": fields,
            "required": list(fields),
            "additionalProperties": False,
        },
    )


def query_view_schema() -> ContractSchema:
    from cruxible_client.contracts.query.results import ClaimQueryResultV1

    raw = ClaimQueryResultV1.model_json_schema()
    definitions = raw.get("$defs", {})

    def expand(schema: dict[str, Any], stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if "$ref" in schema:
            key = schema["$ref"].rsplit("/", 1)[-1]
            # Recursive graph definitions inside artifact rows remain opaque. Their
            # own typed artifact reader is the interface for inspecting them.
            if key in stack:
                return {}
            return expand(definitions[key], (*stack, key))
        return {
            key: (
                {name: expand(child, stack) for name, child in value.items()}
                if key == "properties"
                else expand(value, stack)
                if key == "items" and isinstance(value, dict)
                else [expand(child, stack) for child in value]
                if key in {"anyOf", "oneOf", "allOf"}
                else value
            )
            for key, value in schema.items()
            if key not in {"$defs", "title", "description", "default"}
        }

    return ContractSchema(
        fields={
            "completed": PropertySchema(type="bool"),
            "truncated": PropertySchema(type="bool"),
            "has_conflicts": PropertySchema(type="bool"),
            "result": PropertySchema(type="json", json_schema=expand(raw)),
        }
    )


def read_object_schema(schema: dict[str, Any]) -> ContractSchema:
    """Preserve each declared field's full schema without inferring from data."""
    return ContractSchema(
        fields={
            name: PropertySchema(
                type="json", json_schema=field, optional=name not in schema.get("required", [])
            )
            for name, field in schema["properties"].items()
        },
        allow_extra=schema.get("additionalProperties", True) is not False,
    )
