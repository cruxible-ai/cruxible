"""Procedure slots.

A Procedure never names a provider. Each provider-calling node names a slot,
and each slot is a named field of the Procedure that points at one capability
version, ``{capability, may_truncate}``: wave 0's
:class:`~cruxible_client.contracts.capabilities.CapabilitySlot`, which this
module names :data:`ProcedureSlot` on the Procedure side. A slot is never a
thing to define or look up; its signature ``{in, out, effect}`` is derived from
its capability (:func:`~cruxible_client.contracts.capabilities.capability_slot_signature`).
Authoring pins the capability by digest; a Line binds each slot to one exact
provider build and operation when it is accepted.

Moved out of :mod:`~cruxible_client.contracts.procedures.models`, which
re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.contracts.capabilities import CapabilitySlot
from cruxible_client.contracts.provider_operations import validate_slot_name

#: A Procedure's slot: wave 0's capability slot, ``{capability, may_truncate}``.
ProcedureSlot = CapabilitySlot

#: The most slots one Procedure may declare.
MAX_PROCEDURE_SLOTS = 256


def validate_procedure_slots(value: Mapping[str, ProcedureSlot]) -> dict[str, ProcedureSlot]:
    """Slots are keyed by canonical name, sorted, and bounded."""

    if len(value) > MAX_PROCEDURE_SLOTS:
        raise ValueError(f"a Procedure declares at most {MAX_PROCEDURE_SLOTS} slots")
    for name in value:
        validate_slot_name(name)
    return dict(sorted(value.items(), key=lambda item: item[0].encode("utf-8")))


__all__ = [
    "MAX_PROCEDURE_SLOTS",
    "ProcedureSlot",
    "validate_procedure_slots",
]
