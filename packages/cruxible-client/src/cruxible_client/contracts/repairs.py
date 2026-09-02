"""Structured, runnable repair carriers shared by served refusal envelopes."""

from __future__ import annotations

from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RepairOperationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class HandEditInstructionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1)
    required_change: str = Field(min_length=1)


class HandEditRepairV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hand_edit: HandEditInstructionV1


ServedRepairV1: TypeAlias = RepairOperationV1 | HandEditRepairV1


class ServedRepairEnvelopeV1(BaseModel):
    """Validation utility proving the repair union has exactly one branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repair: ServedRepairV1

    @model_validator(mode="after")
    def _one_branch(self) -> "ServedRepairEnvelopeV1":
        if isinstance(self.repair, RepairOperationV1) == isinstance(self.repair, HandEditRepairV1):
            raise ValueError("repair must select exactly one structured branch")
        return self


def hand_edit_repair(code: str) -> HandEditRepairV1:
    """Return an explicit non-command repair for a refusal needing judgment."""

    normalized = code.replace(".", "_").replace("-", "_")
    return HandEditRepairV1(
        hand_edit=HandEditInstructionV1(
            target=f"refusal/{code}",
            required_change=f"repair_{normalized}",
        )
    )


__all__ = [
    "HandEditInstructionV1",
    "HandEditRepairV1",
    "RepairOperationV1",
    "ServedRepairEnvelopeV1",
    "ServedRepairV1",
    "hand_edit_repair",
]
