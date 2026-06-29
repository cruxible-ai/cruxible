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


def validate_contract_payload(
    config: CoreConfig,
    contract_ref: ContractReference,
    payload: dict[str, Any],
    *,
    subject: str,
    error_factory: Callable[[str], Exception],
    empty_payload_hint: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize a payload against a named contract."""
    contract_name = contract_reference_label(contract_ref)
    contract = resolve_contract(config, contract_ref)
    if contract is None:
        raise ConfigError(f"Contract '{contract_name}' not found for {subject}")

    required_missing: list[str] = []
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    for field_name, field_schema in contract.fields.items():
        if field_name not in payload:
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
                payload[field_name],
                field_schema,
            )
        except ValueError as exc:
            errors.append(f"field '{field_name}': {exc}")

    extra = sorted(set(payload.keys()) - set(contract.fields.keys()))
    if contract.allow_extra:
        json_schema = PropertySchema(type="json", optional=True)
        for field_name in extra:
            try:
                normalized[field_name] = _normalize_contract_field(
                    config,
                    field_name,
                    payload[field_name],
                    json_schema,
                )
            except ValueError as exc:
                errors.append(f"field '{field_name}': {exc}")
    else:
        for field_name in extra:
            errors.append(f"unexpected field '{field_name}'")

    if not payload and required_missing:
        missing = ", ".join(f"'{field_name}'" for field_name in required_missing)
        message = f"{subject} failed contract '{contract_name}': empty input payload provided"
        message = f"{message}; required fields: {missing}"
        if empty_payload_hint:
            message = f"{message}. {empty_payload_hint}"
        raise error_factory(message)

    for field_name in required_missing:
        errors.append(f"missing required field '{field_name}'")

    if errors:
        raise error_factory(f"{subject} failed contract '{contract_name}': {'; '.join(errors)}")

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
