"""Contract validation helpers for workflow/provider payloads."""

from __future__ import annotations

from typing import Any, Callable

from cruxible_core.config.property_validation import normalize_value
from cruxible_core.config.schema import CoreConfig
from cruxible_core.errors import ConfigError, QueryExecutionError


def validate_contract_payload(
    config: CoreConfig,
    contract_name: str,
    payload: dict[str, Any],
    *,
    subject: str,
    error_factory: Callable[[str], Exception],
    empty_payload_hint: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize a payload against a named contract."""
    contract = config.contracts.get(contract_name)
    if contract is None:
        raise ConfigError(f"Contract '{contract_name}' not found for {subject}")

    required_missing: list[str] = []
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    for field_name, field_schema in contract.fields.items():
        if field_name not in payload:
            if field_schema.default is not None:
                normalized[field_name] = field_schema.default
                continue
            if field_schema.optional:
                continue
            required_missing.append(field_name)
            continue
        try:
            normalized[field_name] = normalize_value(payload[field_name], field_schema, config)
        except ValueError as exc:
            errors.append(f"field '{field_name}': {exc}")

    extra = sorted(set(payload.keys()) - set(contract.fields.keys()))
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


def query_execution_error(message: str) -> QueryExecutionError:
    """Factory used by runtime validation helpers."""
    return QueryExecutionError(message)
