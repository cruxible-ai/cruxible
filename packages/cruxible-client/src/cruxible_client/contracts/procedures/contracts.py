"""Runtime validation of the values that cross a Procedure's boundaries."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.provider_operations import (
    OperationItemBudgetExceeded,
    OperationValueRefused,
    validate_operation_value,
)
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    EnumResolver,
    contract_ref_for_identity,
)
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
        # Name the first offending field, not its parent: at the root the
        # parent path is empty and would point the caller nowhere.
        raise ProcedureContractValidationError(
            f"unexpected fields: {extras}; declared fields: {sorted(fields)}",
            field_path=f"{path_prefix}.{extras[0]}" if path_prefix else extras[0],
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


def validate_contract_schema(schema: ContractSchema, payload: object) -> CanonicalValue:
    """Validate an operation boundary using the existing Contract field semantics."""
    if not isinstance(payload, Mapping):
        raise ProcedureContractValidationError("Contract payload must be an object")
    source = dict(payload)
    normalized = _normalize_fields(
        source,
        fields=schema.fields,
        allow_extra=schema.allow_extra,
        path_prefix="",
    )
    return normalize_canonical(normalized)


def validate_contract_schema_with_budget(
    schema: ContractSchema,
    payload: object,
    *,
    max_items: int,
) -> ValidatedProcedureContract:
    if not isinstance(payload, Mapping):
        raise ProcedureContractValidationError("Contract payload must be an object")
    observations: list[ProcedureContractListObservation] = []
    normalized = _normalize_fields(
        dict(payload),
        fields=schema.fields,
        allow_extra=schema.allow_extra,
        path_prefix="",
        max_items=max_items,
        observations=observations,
    )
    return ValidatedProcedureContract(
        value=normalize_canonical(normalized),
        list_observations=tuple(observations),
    )


class AcceptedProcedureContractValidator:
    """Check a Procedure boundary value against one governed Contract at the run's coordinate.

    A Procedure's input, output and parameters are governed Contracts (its
    generated ``<procedure>.input|output|params@N``, or any ``name@N`` it names).
    ``resolve`` reads a Contract version at the admitted coordinate; a pin's
    digest must reproduce. The value is checked exactly (undeclared keys and
    missing required fields refuse) and returned unchanged. ``max_items`` bounds
    every list at this boundary; intermediate values are not checked.
    """

    def __init__(
        self,
        resolve: Callable[[ContractRef], Contract | None],
        enum_members: EnumResolver | None = None,
    ) -> None:
        self._resolve = resolve
        self._enum_members = enum_members

    def contract(
        self,
        contract: ArtifactPin | ArtifactNameRef,
        *,
        direction: Literal["input", "output"],
    ) -> Contract:
        if contract.target.kind != "Contract":
            raise ProcedureContractValidationError(
                f"{direction} contract pin does not target Contract"
            )
        ref = contract_ref_for_identity(contract.target)
        if isinstance(contract, ArtifactPin):
            ref = ref.model_copy(update={"digest": contract.artifact_digest})
        found = self._resolve(ref)
        if found is None:
            raise ProcedureContractValidationError(
                f"{direction} Contract {ref.spelling} is not accepted at this coordinate"
            )
        return found

    def _check(
        self,
        *,
        contract: ArtifactPin | ArtifactNameRef,
        payload: object,
        direction: Literal["input", "output"],
        max_items: int | None,
        observations: list[ProcedureContractListObservation] | None,
    ) -> CanonicalValue:
        declared = self.contract(contract, direction=direction)
        try:
            validate_operation_value(
                payload,
                declared,
                resolve=self._resolve,
                enum_members=self._enum_members,
                max_items=max_items,
                observe_items=None
                if observations is None
                else lambda count, path: observations.append(
                    ProcedureContractListObservation(field_path=path, observed=count)
                ),
            )
        except OperationItemBudgetExceeded as exc:
            raise ProcedureContractItemBudgetExceeded(
                field_path=exc.field_path, limit=exc.limit, observed=exc.observed
            ) from exc
        except OperationValueRefused as exc:
            first = exc.problems[0].partition(":")[0] if exc.problems else None
            raise ProcedureContractValidationError(
                f"{declared.name}@{declared.version} refuses the value: {exc}",
                field_path=first or None,
            ) from exc
        return normalize_canonical(payload)

    def validate_contract(
        self,
        *,
        contract: ArtifactPin | ArtifactNameRef,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
    ) -> CanonicalValue:
        return self._check(
            contract=contract,
            payload=payload,
            direction=direction,
            max_items=None,
            observations=None,
        )

    def validate_contract_with_budget(
        self,
        *,
        contract: ArtifactPin | ArtifactNameRef,
        payload: CanonicalValue,
        direction: Literal["input", "output"],
        max_items: int,
    ) -> ValidatedProcedureContract:
        observations: list[ProcedureContractListObservation] = []
        value = self._check(
            contract=contract,
            payload=payload,
            direction=direction,
            max_items=max_items,
            observations=observations,
        )
        return ValidatedProcedureContract(value=value, list_observations=tuple(observations))


__all__ = [
    "AcceptedProcedureContractValidator",
    "ProcedureContractItemBudgetExceeded",
    "ProcedureContractListObservation",
    "ProcedureContractValidationError",
    "ValidatedProcedureContract",
]
