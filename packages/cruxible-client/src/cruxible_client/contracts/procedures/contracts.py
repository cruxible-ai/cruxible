"""Runtime validation for Contract schemas carried by Procedure-v2 envelopes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV2,
    ProcedureOwnedContractV1,
    procedure_owned_contract_digest,
)
from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.temporal import format_datetime, parse_datetime


class ProcedureContractValidationError(ValueError):
    """A payload or Contract pin does not match an owner's frozen closure."""

    def __init__(
        self,
        message: str,
        *,
        field_path: str | None = None,
        element_index: int | None = None,
    ) -> None:
        super().__init__(message)
        self.field_path = field_path
        self.element_index = element_index


class ProcedureContractItemBudgetExceeded(ProcedureContractValidationError):
    """A list field crossed a Contract boundary above its effective bound."""

    def __init__(self, *, field_path: str, limit: int, observed: int) -> None:
        super().__init__(
            "list field exceeds its effective item bound",
            field_path=field_path,
        )
        self.limit = limit
        self.observed = observed


@dataclass(frozen=True)
class ProcedureContractListObservation:
    field_path: str
    observed: int


@dataclass(frozen=True)
class ValidatedProcedureContract:
    value: CanonicalValue
    list_observations: tuple[ProcedureContractListObservation, ...]


def _normalize_value(
    value: object,
    schema: PropertySchema,
    *,
    field_path: str,
    max_items: int | None = None,
    observations: list[ProcedureContractListObservation] | None = None,
) -> object:
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
    if schema.type == "list":
        if not isinstance(value, list):
            raise ProcedureContractValidationError(
                "must be a list",
                field_path=field_path,
            )
        assert schema.item_fields is not None
        if observations is not None:
            observations.append(
                ProcedureContractListObservation(
                    field_path=field_path,
                    observed=len(value),
                )
            )
        if max_items is not None and len(value) > max_items:
            raise ProcedureContractItemBudgetExceeded(
                field_path=field_path,
                limit=max_items,
                observed=len(value),
            )
        normalized_items: list[object] = []
        for index, item in enumerate(value):
            item_path = f"{field_path}[{index}]"
            if not isinstance(item, Mapping):
                raise ProcedureContractValidationError(
                    "list elements must be objects",
                    field_path=item_path,
                    element_index=index,
                )
            try:
                normalized_items.append(
                    _normalize_fields(
                        dict(item),
                        fields=schema.item_fields,
                        allow_extra=False,
                        path_prefix=item_path,
                        max_items=max_items,
                        observations=observations,
                    )
                )
            except ProcedureContractValidationError as exc:
                if exc.element_index is None:
                    exc.element_index = index
                raise
        return normalized_items
    raise ProcedureContractValidationError(f"unsupported field type {schema.type!r}")


def _normalize_fields(
    source: Mapping[str, object],
    *,
    fields: Mapping[str, PropertySchema],
    allow_extra: bool,
    path_prefix: str,
    max_items: int | None = None,
    observations: list[ProcedureContractListObservation] | None = None,
) -> dict[str, object]:
    extras = sorted(set(source) - set(fields), key=lambda item: item.encode("utf-8"))
    if extras and not allow_extra:
        raise ProcedureContractValidationError(
            f"unexpected fields: {extras}",
            field_path=path_prefix,
        )
    normalized: dict[str, object] = {}
    if allow_extra:
        normalized.update({name: source[name] for name in extras})
    for name in sorted(fields, key=lambda item: item.encode("utf-8")):
        field = fields[name]
        field_path = f"{path_prefix}.{name}" if path_prefix else name
        if name not in source:
            if field.default is not None:
                normalized[name] = _normalize_value(
                    field.default,
                    field,
                    field_path=field_path,
                    max_items=max_items,
                    observations=observations,
                )
            elif not field.optional:
                raise ProcedureContractValidationError(
                    f"missing required field {name!r}",
                    field_path=field_path,
                )
            continue
        try:
            normalized[name] = _normalize_value(
                source[name],
                field,
                field_path=field_path,
                max_items=max_items,
                observations=observations,
            )
        except ProcedureContractValidationError as exc:
            if exc.field_path is None:
                exc.field_path = field_path
            raise
    return normalized


def _validate_payload(
    contract: ProcedureOwnedContractV1,
    payload: CanonicalValue,
) -> CanonicalValue:
    if not isinstance(payload, Mapping):
        raise ProcedureContractValidationError("Contract payload must be an object")
    source = dict(payload)
    schema = contract.contract_schema
    normalized = _normalize_fields(
        source,
        fields=schema.fields,
        allow_extra=schema.allow_extra,
        path_prefix="",
    )
    return normalize_canonical(normalized)


def _validate_payload_with_budget(
    contract: ProcedureOwnedContractV1,
    payload: CanonicalValue,
    *,
    max_items: int,
) -> ValidatedProcedureContract:
    if not isinstance(payload, Mapping):
        raise ProcedureContractValidationError("Contract payload must be an object")
    observations: list[ProcedureContractListObservation] = []
    normalized = _normalize_fields(
        dict(payload),
        fields=contract.contract_schema.fields,
        allow_extra=contract.contract_schema.allow_extra,
        path_prefix="",
        max_items=max_items,
        observations=observations,
    )
    return ValidatedProcedureContract(
        value=normalize_canonical(normalized),
        list_observations=tuple(observations),
    )


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

    def validate_contract_with_budget(
        self,
        *,
        contract: ArtifactPin,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
        max_items: int,
    ) -> ValidatedProcedureContract:
        if contract.target.kind != "Contract":
            raise ProcedureContractValidationError(
                f"{direction} contract pin does not target Contract"
            )
        owned = self._contracts.get(contract.artifact_digest)
        if owned is None or owned.identity != contract.target:
            raise ProcedureContractValidationError(
                f"{direction} contract pin is outside the Procedure owner closure"
            )
        return _validate_payload_with_budget(
            owned,
            payload,
            max_items=max_items,
        )

    def unique_list_field_path(self, contract: ArtifactPin) -> str | None:
        owned = self._contracts.get(contract.artifact_digest)
        if owned is None or owned.identity != contract.target:
            return None
        paths: list[str] = []

        def visit(fields: Mapping[str, PropertySchema], prefix: str) -> None:
            for name in sorted(fields, key=lambda item: item.encode("utf-8")):
                field = fields[name]
                path = f"{prefix}.{name}" if prefix else name
                if field.type == "list":
                    paths.append(path)
                    if field.item_fields is not None:
                        visit(field.item_fields, f"{path}[*]")

        visit(owned.contract_schema.fields, "")
        return paths[0] if len(paths) == 1 else None


__all__ = [
    "OwnedProcedureContractValidator",
    "ProcedureContractItemBudgetExceeded",
    "ProcedureContractListObservation",
    "ProcedureContractValidationError",
    "ValidatedProcedureContract",
]
