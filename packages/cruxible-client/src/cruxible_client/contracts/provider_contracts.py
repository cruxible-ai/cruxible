"""Operation contracts carried in the exact bytes of a ProviderInterface.

Calls reuse Procedure Contract schemas. Acquisition has one shared result
contract; providers return material and Core constructs the Capture.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cruxible_client.contracts.procedures.contract_schema import ContractSchema

ACQUISITION_RESULT = "playbill-provider-result-to-external-capture-v1"


class ProviderOperationContractV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input: ContractSchema
    output: ContractSchema | Literal["playbill-provider-result-to-external-capture-v1"]
    material: ContractSchema | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _material_schema(self) -> "ProviderOperationContractV1":
        if self.material is not None and self.output != ACQUISITION_RESULT:
            raise ValueError("Only an acquisition interface declares captured material")
        return self


def read_provider_operation_contract(interface_bytes_hex: str) -> ProviderOperationContractV1:
    definition = json.loads(bytes.fromhex(interface_bytes_hex))
    if not isinstance(definition, dict) or "contracts" not in definition:
        raise ValueError("ProviderInterface must declare its operation contracts")
    return ProviderOperationContractV1.model_validate(definition["contracts"], extra="forbid")


def operation_schema_shape(schema: ContractSchema) -> dict[str, object]:
    """Compare validation rules, independent of field descriptions/index hints."""

    def fields_shape(fields: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name, field in fields.items():
            field = {
                key: value
                for key, value in field.items()
                if key not in {"description", "indexed", "primary_key"}
            }
            field["type"] = {"integer": "int", "number": "float"}.get(field["type"], field["type"])
            if "item_fields" in field:
                field["item_fields"] = fields_shape(field["item_fields"])
            result[name] = field
        return result

    return {
        "allow_extra": schema.allow_extra,
        "fields": fields_shape(schema.model_dump()["fields"]),
    }


def validate_provider_value(
    contract: ProviderOperationContractV1, payload: object, *, direction: Literal["input", "output"]
) -> object:
    # Lazy imports keep the interface/Procedure/Capture contract graph acyclic.
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.captures import ProviderResultToExternalCaptureV1
    from cruxible_client.contracts.procedures.contracts import validate_contract_schema

    schema = contract.input if direction == "input" else contract.output
    if isinstance(schema, str):
        parsed = ProviderResultToExternalCaptureV1.model_validate(payload)
        if canonical_bytes(parsed.model_dump(mode="json")) != canonical_bytes(payload):
            raise ValueError("Acquisition output must use the canonical Capture result encoding")
        if contract.material is not None:
            import base64

            from cruxible_client.contracts.records import Record

            content = base64.b64decode(parsed.content_base64, validate=True)
            material = json.loads(content)
            if canonical_bytes(material) != content:
                raise ValueError("Typed acquisition material must use canonical JSON")
            Record(contract.material, material)
    else:
        validate_contract_schema(schema, payload)
    # Interface checks cannot rewrite the exact request or returned bytes: those
    # are already bound by derived-request and invocation commitments. Defaults
    # remain the adapter's responsibility (or the caller's carried Contract).
    return payload
