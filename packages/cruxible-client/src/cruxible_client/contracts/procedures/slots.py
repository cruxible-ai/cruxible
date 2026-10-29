"""Procedure slots.

A Procedure never names a provider. Each provider-calling node names a slot,
and the Procedure declares every slot's signature ``{in, out, effect}``
(:class:`~cruxible_client.contracts.provider_operations.ProcedureSlot`). A Line
binds each slot to one exact provider build and operation when it is accepted.

Moved out of :mod:`~cruxible_client.contracts.procedures.models`, which
re-exports every name here.
"""

from __future__ import annotations

from collections.abc import Mapping

from cruxible_client.contracts.provider_operations import ProcedureSlot, validate_slot_name

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
