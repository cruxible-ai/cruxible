"""Contract validation helpers for workflow/provider payloads."""

from __future__ import annotations

from typing import Any, Callable

from cruxible_core.config.json_schema_validation import validate_value_against_json_schema
from cruxible_core.config.property_validation import normalize_value
from cruxible_core.config.schema import (
    BUILTIN_CONTRACTS,
    ContractReference,
    ContractSchema,
    CoreConfig,
    PropertySchema,
)
from cruxible_core.errors import ConfigError, QueryExecutionError
from cruxible_core.workflow.step_helpers import SOURCE_METADATA_KEY

MAX_DECLARED_FIELDS_IN_ERROR = 40
"""Cap on field names echoed by one contract rejection.

Mirrors the registered-provider cap in ``service.procedures``: a wide contract
must still teach its shape without turning one rejection into an unbounded
dump.
"""


def declared_fields(contract: ContractSchema) -> str:
    """Render a contract's declared fields as ``name (type, required|optional)``."""
    entries = [
        f"{field_name} ({field_schema.type}, {'optional' if field_schema.optional else 'required'})"
        for field_name, field_schema in sorted(contract.fields.items())
    ]
    if not entries:
        return (
            "no fields (any object accepted)" if contract.allow_extra else "no fields (empty input)"
        )
    suffix = " plus extra fields" if contract.allow_extra else ""
    if len(entries) > MAX_DECLARED_FIELDS_IN_ERROR:
        shown = ", ".join(entries[:MAX_DECLARED_FIELDS_IN_ERROR])
        return (
            f"{shown}, ... ({len(entries)} total; "
            f"first {MAX_DECLARED_FIELDS_IN_ERROR} shown){suffix}"
        )
    return f"{', '.join(entries)}{suffix}"


def validate_contract_payload(
    config: CoreConfig,
    contract_ref: ContractReference,
    payload: dict[str, Any],
    *,
    subject: str,
    error_factory: Callable[[str], Exception],
    empty_payload_hint: str | None = None,
    strip_reserved_source_metadata: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a payload against a named contract."""
    contract_name = contract_reference_label(contract_ref)
    contract = resolve_contract(config, contract_ref)
    if contract is None:
        raise ConfigError(f"Contract '{contract_name}' not found for {subject}")

    validation_payload = payload
    if (
        strip_reserved_source_metadata
        and SOURCE_METADATA_KEY in payload
        and SOURCE_METADATA_KEY not in contract.fields
    ):
        validation_payload = dict(payload)
        validation_payload.pop(SOURCE_METADATA_KEY, None)

    required_missing: list[str] = []
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    shape_errors = False

    for field_name, field_schema in contract.fields.items():
        if field_name not in validation_payload:
            if field_schema.default is not None:
                try:
                    normalized[field_name] = _normalize_contract_field(
                        config,
                        field_name,
                        field_schema.default,
                        field_schema,
                    )
                except ValueError as exc:
                    errors.append(f"field '{field_name}' default: {exc}")
                continue
            if field_schema.optional:
                continue
            required_missing.append(field_name)
            continue
        try:
            normalized[field_name] = _normalize_contract_field(
                config,
                field_name,
                validation_payload[field_name],
                field_schema,
            )
        except ValueError as exc:
            errors.append(f"field '{field_name}': {exc}")

    extra = sorted(set(validation_payload.keys()) - set(contract.fields.keys()))
    if contract.allow_extra:
        json_schema = PropertySchema(type="json", optional=True)
        for field_name in extra:
            try:
                normalized[field_name] = _normalize_contract_field(
                    config,
                    field_name,
                    validation_payload[field_name],
                    json_schema,
                )
            except ValueError as exc:
                errors.append(f"field '{field_name}': {exc}")
    else:
        for field_name in extra:
            errors.append(f"unexpected field '{field_name}'")
            shape_errors = True

    if not validation_payload and required_missing:
        missing = ", ".join(f"'{field_name}'" for field_name in required_missing)
        message = f"{subject} failed contract '{contract_name}': empty input payload provided"
        message = f"{message}; required fields: {missing}"
        message = f"{message}; contract '{contract_name}' declares: {declared_fields(contract)}"
        if empty_payload_hint:
            message = f"{message}. {empty_payload_hint}"
        raise error_factory(message)

    for field_name in required_missing:
        errors.append(f"missing required field '{field_name}'")
        shape_errors = True

    if errors:
        message = f"{subject} failed contract '{contract_name}': {'; '.join(errors)}"
        if shape_errors:
            # A field-shape rejection ("unexpected field 'x'" / "missing
            # required field 'y'") is the caller guessing at the contract. Echo
            # what the contract actually declares so the next call is informed
            # rather than another guess.
            message = f"{message}; contract '{contract_name}' declares: {declared_fields(contract)}"
        raise error_factory(message)

    return normalized


def resolve_contract(
    config: CoreConfig,
    contract_ref: ContractReference,
) -> ContractSchema | None:
    """Resolve inline, config-defined, or built-in contract references."""
    if isinstance(contract_ref, ContractSchema):
        return contract_ref
    return config.contracts.get(contract_ref) or BUILTIN_CONTRACTS.get(contract_ref)


def contract_reference_label(contract_ref: ContractReference) -> str:
    """Return a stable label for human-facing contract diagnostics."""
    if isinstance(contract_ref, ContractSchema):
        return "<inline>"
    return contract_ref


def _normalize_contract_field(
    config: CoreConfig,
    field_name: str,
    value: Any,
    field_schema: PropertySchema,
) -> Any:
    normalized = normalize_value(value, field_schema, config)
    if (
        field_schema.type == "json"
        and field_schema.json_schema is not None
        and normalized is not None
    ):
        validate_value_against_json_schema(
            normalized,
            field_schema.json_schema,
            config.enums,
            field_name,
        )
    return normalized


def query_execution_error(message: str) -> QueryExecutionError:
    """Factory used by runtime validation helpers."""
    return QueryExecutionError(message)
