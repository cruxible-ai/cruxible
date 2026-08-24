"""Exact Procedure-pin and LineSpec-slot closure."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifactAny
from cruxible_client.contracts.procedures.models import (
    ProcedurePinSlotRefV1,
    iter_pin_bindings,
)


class ProcedurePinClosureError(PlaybillFormatError):
    """A LineSpec binding cannot close an accepted Procedure exactly."""


class _StrictClosureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LineSlotBindingV1(_StrictClosureModel):
    tag: Literal["playbill-line-slot-binding-v1"] = "playbill-line-slot-binding-v1"
    slot_name: str
    artifact_pin: ArtifactPin


class ProcedureSlotInterfaceV1(_StrictClosureModel):
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
    def _nonempty(self) -> "ProcedureSlotInterfaceV1":
        if self.contract_in_digest is None and self.contract_out_digest is None:
            raise ValueError("slot interface must commit at least one contract digest")
        return self


def procedure_slot_interface_digest(interface: ProcedureSlotInterfaceV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-slot-interface-v1",
        interface.model_dump(mode="json", exclude={"tag"}),
    )


@dataclass(frozen=True)
class ClosedProcedurePinsV1:
    exact_pins: tuple[ArtifactPin, ...]
    bound_slot_names: tuple[str, ...]


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def close_procedure_pin_slots(
    procedure: ProcedureArtifactAny,
    *,
    bindings: tuple[LineSlotBindingV1, ...],
    interface_digests: Mapping[str, str],
) -> ClosedProcedurePinsV1:
    """Close every declared slot with one exact, role/kind/interface-matched pin.

    ``interface_digests`` is keyed by the bound artifact digest.  It is produced
    by the artifact family's frozen interface projection, never by a caller's
    assertion or by a mutable provider name.
    """

    binding_names = tuple(binding.slot_name for binding in bindings)
    if binding_names != tuple(sorted(set(binding_names), key=lambda item: item.encode("utf-8"))):
        raise ProcedurePinClosureError("Line slot bindings must be sorted and unique")
    declarations = {slot.slot_name: slot for slot in procedure.definition.pin_slots}
    referenced = {
        binding.slot_name
        for binding in iter_pin_bindings(procedure.definition)
        if isinstance(binding, ProcedurePinSlotRefV1)
    }
    supplied = set(binding_names)
    missing = referenced - supplied
    extra = supplied - referenced
    if missing:
        raise ProcedurePinClosureError(f"playbill.procedure.unfilled_pin_slot: {sorted(missing)}")
    if extra:
        raise ProcedurePinClosureError(f"LineSpec supplies extra pin slots: {sorted(extra)}")

    closed = list(procedure.pins)
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
        actual_interface = interface_digests.get(pin.artifact_digest)
        if actual_interface is None:
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} bound artifact has no verified interface"
            )
        if actual_interface != declaration.interface_digest:
            raise ProcedurePinClosureError(
                f"slot {binding.slot_name!r} interface digest does not match"
            )
        closed.append(pin)
    exact = tuple(sorted(set(closed), key=_pin_key))
    return ClosedProcedurePinsV1(exact_pins=exact, bound_slot_names=binding_names)


__all__ = [
    "ClosedProcedurePinsV1",
    "LineSlotBindingV1",
    "ProcedurePinClosureError",
    "ProcedureSlotInterfaceV1",
    "close_procedure_pin_slots",
    "procedure_slot_interface_digest",
]
