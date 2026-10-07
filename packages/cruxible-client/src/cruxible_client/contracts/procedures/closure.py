"""Exact Procedure-pin and slot closure: how a Blueprint's open slots are bound."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinition,
    ProcedurePinSlotRef,
    iter_pin_bindings,
)


class ProcedurePinClosureError(FormatError):
    """A slot binding cannot close a Blueprint's slots exactly."""


class _StrictClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProcedureSlotBinding(_StrictClosureModel):
    """One open slot bound to one exact accepted artifact, through the slot's interface.

    ``interface_digest`` is the interface the slot declares and the binding
    serves. One artifact may fill several slots of different interfaces, so the
    interface belongs to the binding, not to the artifact.
    """

    tag: Literal["cruxible-procedure-slot-binding-v1"] = "cruxible-procedure-slot-binding-v1"
    slot_name: str
    artifact_pin: ArtifactPin
    interface_digest: str

    @field_validator("interface_digest")
    @classmethod
    def _interface(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class ProcedureSlotInterface(_StrictClosureModel):
    """Frozen nominal interface preimage shared by Procedure and implementation."""

    tag: Literal["playbill-procedure-slot-interface-v1"] = "playbill-procedure-slot-interface-v1"
    artifact_kind: str
    pin_role: str
    contract_in_digest: str | None = None
    contract_out_digest: str | None = None

    @field_validator("contract_in_digest", "contract_out_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            ArtifactDigest.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _nonempty(self) -> "ProcedureSlotInterface":
        if self.contract_in_digest is None and self.contract_out_digest is None:
            raise ValueError("slot interface must commit at least one contract digest")
        return self


@dataclass(frozen=True)
class ClosedProcedurePins:
    exact_pins: tuple[ArtifactPin, ...]
    bound_slot_names: tuple[str, ...]


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def close_procedure_pin_slots(
    definition: ProcedureDefinition,
    pins: tuple[ArtifactPin, ...],
    *,
    bindings: tuple[ProcedureSlotBinding, ...],
    interface_digests: Mapping[str, str],
) -> ClosedProcedurePins:
    """Close every declared slot with one exact, role/kind/interface-matched pin.

    ``interface_digests`` is keyed by slot name: the interface the bound
    artifact was verified to implement for that slot (one artifact can serve
    several slots of different interfaces). It is produced by the artifact
    family's frozen interface projection, never by a caller's assertion or by a
    mutable provider name.
    """

    binding_names = tuple(binding.slot_name for binding in bindings)
    if binding_names != tuple(sorted(set(binding_names), key=lambda item: item.encode("utf-8"))):
        raise ProcedurePinClosureError("slot bindings must be sorted and unique")
    declarations = {slot.slot_name: slot for slot in definition.pin_slots}
    referenced = {
        binding.slot_name
        for binding in iter_pin_bindings(definition)
        if isinstance(binding, ProcedurePinSlotRef)
    }
    supplied = set(binding_names)
    missing = referenced - supplied
    extra = supplied - referenced
    if missing:
        raise ProcedurePinClosureError(f"cruxible.procedure.unfilled_pin_slot: {sorted(missing)}")
    if extra:
        raise ProcedurePinClosureError(f"bindings supply extra pin slots: {sorted(extra)}")

    closed = list(pins)
    for binding in bindings:
        declaration = declarations[binding.slot_name]
        pin = binding.artifact_pin
        if pin.role != declaration.pin_role:
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} requires role {declaration.pin_role!r}"
            )
        if pin.target.kind != declaration.artifact_kind:
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} requires kind {declaration.artifact_kind!r}"
            )
        actual_interface = interface_digests.get(binding.slot_name)
        if actual_interface is None:
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} bound artifact has no verified interface"
            )
        if (
            actual_interface != declaration.interface_digest
            or binding.interface_digest != declaration.interface_digest
        ):
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} interface digest does not match"
            )
        closed.append(pin)
    exact = tuple(sorted(set(closed), key=_pin_key))
    return ClosedProcedurePins(exact_pins=exact, bound_slot_names=binding_names)


__all__ = [
    "ClosedProcedurePins",
    "ProcedurePinClosureError",
    "ProcedureSlotBinding",
    "ProcedureSlotInterface",
    "close_procedure_pin_slots",
]
