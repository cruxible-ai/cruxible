"""Procedure slots and pin bindings: where a node names an implementation.

Moved out of :mod:`~cruxible_client.contracts.procedures.models`, which
re-exports every name here.
"""

from __future__ import annotations

import re
import unicodedata
from typing import ClassVar, Literal

from pydantic import field_validator

from cruxible_client.contracts.artifacts import ArtifactNameRef, ArtifactPin
from cruxible_client.contracts.canonical import ArtifactDigest
from cruxible_client.contracts.procedures.model_base import (
    _NAME_RE,
    _ROLE_RE,
    _canonical_identifier,
    _StrictProcedureModel,
)


class ProcedurePinSlot(_StrictProcedureModel):
    """One interface-typed binding point; it never chooses an implementation."""

    tag: Literal["playbill-procedure-pin-slot-v1"] = "playbill-procedure-pin-slot-v1"
    slot_name: str
    pin_role: str
    artifact_kind: str
    interface_digest: str

    @field_validator("slot_name")
    @classmethod
    def _slot_name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure slot_name")

    @field_validator("pin_role", check_fields=False)
    @classmethod
    def _pin_role(cls, value: str) -> str:
        return _canonical_identifier(value, _ROLE_RE, label="Procedure slot pin_role")

    @field_validator("artifact_kind", check_fields=False)
    @classmethod
    def _artifact_kind(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value or not re.fullmatch(
            r"^[A-Z][A-Za-z0-9_.-]{0,63}$", value
        ):
            raise ValueError("Procedure slot artifact_kind must be a canonical artifact kind")
        return value

    @field_validator("interface_digest", check_fields=False)
    @classmethod
    def _interface_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value


class ProcedurePinSlotRef(_StrictProcedureModel):
    tag: Literal["playbill-procedure-pin-slot-ref-v1"] = "playbill-procedure-pin-slot-ref-v1"
    slot_name: str

    @field_validator("slot_name")
    @classmethod
    def _slot_name(cls, value: str) -> str:
        return _canonical_identifier(value, _NAME_RE, label="Procedure slot reference")


LegacyProcedurePinBinding = ArtifactPin | ProcedurePinSlotRef
ProcedurePinBinding = LegacyProcedurePinBinding | ArtifactNameRef


# Graph format 7 (name-referenced) slots and bindings; re-exported by
# ``named_models``.
NameBinding = ArtifactNameRef | ProcedurePinSlotRef


class NamedProcedurePinSlot(ProcedurePinSlot):
    tag: Literal["cruxible-procedure-slot-v2"] = "cruxible-procedure-slot-v2"  # type: ignore[assignment]
    pin_role: ClassVar[str] = "provider"  # type: ignore[misc]
    artifact_kind: ClassVar[str] = "Provider"  # type: ignore[misc]
    interface_digest: ClassVar[None] = None  # type: ignore[misc, assignment]
    interface: ArtifactNameRef


__all__ = [
    "LegacyProcedurePinBinding",
    "NameBinding",
    "NamedProcedurePinSlot",
    "ProcedurePinBinding",
    "ProcedurePinSlot",
    "ProcedurePinSlotRef",
]
