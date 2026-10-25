"""Typed schema views of existing query and acquisition records.

These describe data returned by the existing readers. They do not evaluate
queries or invent fields from sample values.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cruxible_client.contracts.claim_type_structure import ClaimTypeStructure
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema


def expanded_model_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema()
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

    return expand(raw)


def query_view_schema() -> ContractSchema:
    from cruxible_client.contracts.projection import AcceptedProjectionCoordinate
    from cruxible_client.contracts.query.results import ClaimQueryResult, QueryExecutionReceipt

    return ContractSchema(
        fields={
            "coordinate": PropertySchema(
                type="json", json_schema=expanded_model_schema(AcceptedProjectionCoordinate)
            ),
            "definition_path": PropertySchema(type="string"),
            "definition_digest": PropertySchema(type="string"),
            "receipt": PropertySchema(
                type="json", json_schema=expanded_model_schema(QueryExecutionReceipt)
            ),
            "completed": PropertySchema(type="bool"),
            "truncated": PropertySchema(type="bool"),
            "has_conflicts": PropertySchema(type="bool"),
            "result": PropertySchema(
                type="json", json_schema=expanded_model_schema(ClaimQueryResult)
            ),
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


def schema_json_type(schema: ContractSchema | PropertySchema) -> dict[str, Any]:
    if isinstance(schema, ContractSchema):
        return {
            "type": "object",
            "properties": {k: schema_json_type(v) for k, v in schema.fields.items()},
            "required": [k for k, v in schema.fields.items() if not v.optional],
            "additionalProperties": schema.allow_extra,
        }
    if schema.json_schema is not None:
        return schema.json_schema
    if schema.type == "json":
        return {}
    if schema.type == "list":
        return {
            "type": "array",
            "items": schema_json_type(ContractSchema(fields=schema.item_fields or {})),
        }
    result: dict[str, Any] = {
        "type": {"int": "integer", "bool": "boolean", "datetime": "string", "date": "string"}.get(
            schema.type, schema.type
        )
    }
    if schema.enum is not None:
        result["enum"] = schema.enum
    return result


def claim_view_schema(shape: ClaimTypeStructure) -> ContractSchema:
    from cruxible_client.contracts.claims import ExactContentClaimObject, SubjectClaimObject
    from cruxible_client.contracts.semantic import SemanticAddress

    value_schema = (
        shape.literal_schema
        if shape.object_kind == "literal"
        else expanded_model_schema(
            SubjectClaimObject if shape.object_kind == "subject" else ExactContentClaimObject
        )
    )
    schema = ContractSchema(
        fields={
            "value": PropertySchema(type="json", json_schema=value_schema),
            "claim_id": PropertySchema(type="string"),
            "revision": PropertySchema(type="string"),
            "subject": PropertySchema(
                type="json", json_schema=expanded_model_schema(SemanticAddress)
            ),
            "predicate": PropertySchema(type="string"),
            "qualifier": PropertySchema(type="json", json_schema={"type": ["string", "null"]}),
            "role": PropertySchema(type="string"),
            "object_kind": PropertySchema(type="string"),
            "lifecycle_state": PropertySchema(type="string"),
            "captures": PropertySchema(
                type="json", json_schema={"type": "array", "items": {"type": "string"}}
            ),
            "verdict": PropertySchema(type="string"),
            "currency": PropertySchema(type="string"),
            "identity": PropertySchema(type="string"),
            "artifact_digest": PropertySchema(type="string"),
            "statement_digest": PropertySchema(type="string"),
            "path": PropertySchema(type="string"),
            "claim": PropertySchema(type="json"),
        }
    )
    return schema
