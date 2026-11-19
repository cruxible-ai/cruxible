"""Runtime validation of the values that cross a Procedure's boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
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

if TYPE_CHECKING:
    from cruxible_client.contracts.procedures.models import ProcedureDefinition


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


#: A Line run's Procedure input: a Line supplies parameters, never input.
LINE_RUN_INPUT: dict[str, CanonicalValue] = {}


def validate_line_parameters(
    validator: AcceptedProcedureContractValidator,
    definition: ProcedureDefinition,
    parameters: object,
) -> None:
    """Refuse a Line whose runs the Procedure would refuse at the boundary.

    A Line supplies the Procedure's parameters and its runs take an empty
    input: the input Contract must accept ``{}``, and the Line's values over
    the Procedure's defaults must satisfy its params Contract. A refusal names
    the parameter (``field_path``) it is about.
    """

    try:
        validator.validate_contract(
            contract=definition.contract_in, payload=LINE_RUN_INPUT, direction="input"
        )
    except ProcedureContractValidationError as exc:
        raise ProcedureContractValidationError(
            f"a Line run's input is empty, and the Procedure's input refuses it: {exc}; "
            "a Line supplies parameters (declare them as the Procedure's params)",
        ) from exc
    supplied = normalize_canonical(parameters)
    if not isinstance(supplied, dict):
        raise ProcedureContractValidationError("Line parameters are a record")
    if definition.parameter_contract is None:
        if supplied:
            raise ProcedureContractValidationError(
                f"Procedure {definition.name!r} declares no parameters; the Line names "
                + ", ".join(sorted(supplied)),
                field_path=sorted(supplied)[0],
            )
        return
    validator.validate_contract(
        contract=definition.parameter_contract,
        payload=definition.effective_parameters(supplied),
        direction="input",
    )


__all__ = [
    "LINE_RUN_INPUT",
    "AcceptedProcedureContractValidator",
    "ProcedureContractItemBudgetExceeded",
    "ProcedureContractListObservation",
    "ProcedureContractValidationError",
    "ValidatedProcedureContract",
    "validate_line_parameters",
]
