"""Runtime validation for Contract schemas carried by Procedure-v2 envelopes."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Literal

from cruxible_core.config.schema import PropertySchema
from cruxible_core.playbill.artifacts import ArtifactPin
from cruxible_core.playbill.canonical import CanonicalValue, normalize_canonical
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    procedure_owned_contract_digest,
)
from cruxible_core.primitives import canonical_json
from cruxible_core.temporal import format_datetime, parse_datetime


class ProcedureContractValidationError(ValueError):
    """A payload or Contract pin does not match an owner's frozen closure."""


def _normalize_value(value: object, schema: PropertySchema) -> object:
    if value is None:
        if schema.optional:
            return None
        raise ProcedureContractValidationError("value may not be null")
    if schema.enum_ref is not None:
        raise ProcedureContractValidationError(
            "owner-carried Contracts cannot resolve config-scoped enum_ref values"
        )
    if schema.enum is not None and value not in schema.enum:
        raise ProcedureContractValidationError("value is outside the declared enum")
    if schema.type == "string":
        if not isinstance(value, str):
            raise ProcedureContractValidationError("must be a string")
        return value
    if schema.type in {"int", "integer"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProcedureContractValidationError("must be an int")
        return value
    if schema.type in {"float", "number"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProcedureContractValidationError("must be a number")
        return float(value)
    if schema.type == "bool":
        if not isinstance(value, bool):
            raise ProcedureContractValidationError("must be a bool")
        return value
    if schema.type == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ProcedureContractValidationError("must be an ISO date string") from exc
            return value
        raise ProcedureContractValidationError("must be an ISO date string")
    if schema.type == "datetime":
        if not isinstance(value, str | datetime):
            raise ProcedureContractValidationError("must be an ISO datetime string")
        try:
            parsed = parse_datetime(value)
        except (TypeError, ValueError) as exc:
            raise ProcedureContractValidationError("must be an ISO datetime string") from exc
        if parsed is None:
            raise ProcedureContractValidationError("must be an ISO datetime string")
        return format_datetime(parsed)
    if schema.type == "json":
        try:
            canonical_json(value)
        except (TypeError, ValueError) as exc:
            raise ProcedureContractValidationError("must be JSON-serializable") from exc
        return value
    raise ProcedureContractValidationError(f"unsupported field type {schema.type!r}")


def _validate_payload(
    contract: ProcedureOwnedContractV1,
    payload: CanonicalValue,
) -> CanonicalValue:
    if not isinstance(payload, Mapping):
        raise ProcedureContractValidationError("Contract payload must be an object")
    source = dict(payload)
    schema = contract.contract_schema
    extras = sorted(set(source) - set(schema.fields), key=lambda item: item.encode("utf-8"))
    if extras and not schema.allow_extra:
        raise ProcedureContractValidationError(f"unexpected fields: {extras}")
    normalized: dict[str, object] = {}
    if schema.allow_extra:
        normalized.update({name: source[name] for name in extras})
    for name in sorted(schema.fields, key=lambda item: item.encode("utf-8")):
        field = schema.fields[name]
        if name not in source:
            if field.default is not None:
                normalized[name] = _normalize_value(field.default, field)
            elif not field.optional:
                raise ProcedureContractValidationError(f"missing required field {name!r}")
            continue
        try:
            normalized[name] = _normalize_value(source[name], field)
        except ProcedureContractValidationError as exc:
            raise ProcedureContractValidationError(f"field {name!r}: {exc}") from exc
    return normalize_canonical(normalized)


class OwnedProcedureContractValidator:
    """Resolve Contract pins only from one accepted Procedure-v2 owner."""

    def __init__(self, accepted: AcceptedProcedureV1) -> None:
        if not isinstance(accepted.procedure, ProcedureArtifactV2):
            raise ProcedureContractValidationError(
                "owner-carried Contract validation requires playbill-procedure-v2"
            )
        self._contracts = {
            procedure_owned_contract_digest(contract).tagged: contract
            for contract in accepted.procedure.owned_contracts
        }

    def validate_contract(
        self,
        *,
        contract: ArtifactPin,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
    ) -> CanonicalValue:
        if contract.target.kind != "Contract":
            raise ProcedureContractValidationError(
                f"{direction} contract pin does not target Contract"
            )
        owned = self._contracts.get(contract.artifact_digest)
        if owned is None or owned.identity != contract.target:
            raise ProcedureContractValidationError(
                f"{direction} contract pin is outside the Procedure owner closure"
            )
        return _validate_payload(owned, payload)


__all__ = [
    "OwnedProcedureContractValidator",
    "ProcedureContractValidationError",
]
