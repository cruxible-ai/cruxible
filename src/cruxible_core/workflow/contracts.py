"""Contract validation helpers for workflow/provider payloads."""

from __future__ import annotations

import re
from typing import Any, Callable

from cruxible_core.config.json_schema_validation import validate_value_against_json_schema
from cruxible_core.config.property_validation import enum_values, normalize_value
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


def contract_field_is_required(field_schema: PropertySchema) -> bool:
    """Return whether a caller must supply this contract field.

    The one requiredness predicate for contracts, shared by the rejection
    message :func:`declared_fields` renders and by the discovery surface
    ``get_procedure`` returns. It answers the only question a caller building a
    payload has -- *must I supply this key?* -- and a defaulted field is the case
    the two surfaces used to answer differently: ``validate_contract_payload``
    fills a declared ``default`` before it ever reaches the ``optional`` check,
    so a non-optional field carrying a default is never missing, and calling it
    required is a lie the caller would obey.
    """
    return not field_schema.optional and field_schema.default is None


def declared_fields(contract: ContractSchema) -> str:
    """Render a contract's declared fields as ``name (type, required|optional)``."""
    entries = [
        f"{field_name} ({field_schema.type}, "
        f"{'required' if contract_field_is_required(field_schema) else 'optional'})"
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


_DESCRIPTION_BACKTICK_HINT = re.compile(r"`([^`\n]{1,64})`")
_DESCRIPTION_EG_HINT = re.compile(r"e\.g\.\s*[\"'`]?([^\"'`,.;\n]{1,64})", flags=re.IGNORECASE)

_EXAMPLE_BY_TYPE: dict[str, Any] = {
    "int": 1,
    "integer": 1,
    "float": 1.0,
    "number": 1.0,
    "bool": True,
    "date": "2026-01-01",
    "datetime": "2026-01-01T00:00:00Z",
}


def contract_input_example(
    config: CoreConfig,
    contract: ContractSchema,
) -> dict[str, Any] | None:
    """Build one worked payload satisfying a contract, or ``None`` if it takes none.

    A caller reading a field list still has to invent values for it; a worked
    example is the thing that gets pasted. The example carries exactly the keys
    the caller MUST supply (:func:`contract_field_is_required`) -- defaulted and
    optional fields are deliberately absent, because including them would teach
    a payload wider than the contract demands.

    ``None`` means "this contract accepts no payload at all"
    (``cruxible.EmptyInput``: no fields, no extras). A contract that declares no
    fields but sets ``allow_extra`` (``cruxible.JsonObject``) yields ``{}`` --
    an empty object is a valid payload there and the caller may add any keys.
    """
    if not contract.fields and not contract.allow_extra:
        return None
    return {
        field_name: _example_field_value(config, field_name, field_schema)
        for field_name, field_schema in sorted(contract.fields.items())
        if contract_field_is_required(field_schema)
    }


def _example_field_value(config: CoreConfig, field_name: str, field_schema: PropertySchema) -> Any:
    """Return one type-appropriate example value for a contract field.

    Preference order is most-specific-first: an enumerated vocabulary pins the
    value exactly, a declared default is the author's own worked value, a
    description that quotes a literal is the author spelling one out, and only
    then does the field type supply a placeholder.
    """
    allowed = enum_values(config, field_schema)
    if allowed:
        return allowed[0]
    if field_schema.default is not None:
        return field_schema.default
    if field_schema.type == "string":
        hint = _description_example(field_schema.description)
        return hint if hint is not None else f"<{field_name}>"
    if field_schema.type == "json":
        return _json_schema_example(field_schema.json_schema)
    return _EXAMPLE_BY_TYPE.get(field_schema.type, f"<{field_name}>")


def _description_example(description: str | None) -> str | None:
    """Extract a literal example a field description spells out, if any.

    Only two mechanical shapes are read -- a backticked token and the token
    after ``e.g.`` -- so the hint is deterministic prose extraction, never a
    guess at what free text means.
    """
    if not description:
        return None
    for pattern in (_DESCRIPTION_BACKTICK_HINT, _DESCRIPTION_EG_HINT):
        match = pattern.search(description)
        if match is not None:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
    return None


def _json_schema_example(json_schema: dict[str, Any] | None) -> Any:
    """Build a minimal example for a json-typed field from its nested schema."""
    if not isinstance(json_schema, dict):
        return {}
    schema_type = json_schema.get("type")
    if schema_type == "array":
        item_schema = json_schema.get("items")
        item = _json_schema_example(item_schema) if isinstance(item_schema, dict) else {}
        return [item]
    if schema_type in {"integer", "number"}:
        return 1 if schema_type == "integer" else 1.0
    if schema_type == "boolean":
        return True
    if schema_type == "string":
        enum = json_schema.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        return "<value>"
    properties = json_schema.get("properties")
    required = json_schema.get("required")
    if not isinstance(properties, dict):
        return {}
    names = required if isinstance(required, list) else sorted(properties)
    return {
        str(name): _json_schema_example(properties[name])
        for name in names
        if name in properties and isinstance(properties[name], dict)
    }


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
