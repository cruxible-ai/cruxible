"""How far a settle grant reaches: per ClaimType and change kind, the evidence bases.

A leaf module (pydantic only) so authoring inputs, Mandates and proposal
records share one definition of the scope vocabulary.
"""

from __future__ import annotations

import json
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictMandateScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MandateChangeKind = Literal["create", "revise", "retire"]
MANDATE_CHANGE_KIND_ORDER: Final[tuple[MandateChangeKind, ...]] = ("create", "revise", "retire")


#: Where a changed value came from (its evidence receipt): core-extracted from a
#: Capture, parsed by a provider (``measured``), computed by Procedure logic
#: (``derived``), or concluded from a complete source's absence (``absence``:
#: mirror retirements and inverse writes).
MandateBasis = Literal["extracted", "measured", "derived", "absence"]
MANDATE_BASIS_ORDER: Final[tuple[MandateBasis, ...]] = (
    "extracted",
    "measured",
    "derived",
    "absence",
)


class MandateChangeScope(_StrictMandateScopeModel):
    """The evidence bases one change kind of a scoped ClaimType settles on."""

    basis: tuple[MandateBasis, ...]

    @field_validator("basis")
    @classmethod
    def _basis(cls, value: tuple[MandateBasis, ...]) -> tuple[MandateBasis, ...]:
        if not value or value != tuple(item for item in MANDATE_BASIS_ORDER if item in value):
            raise ValueError(
                "a mandate basis list must be nonempty, unique and in canonical order "
                "(extracted, measured, derived, absence)"
            )
        return value


class MandateChanges(_StrictMandateScopeModel):
    """Per change kind, the bases a settle grant covers; a kind left out never settles."""

    create: MandateChangeScope | None = Field(default=None, exclude_if=lambda value: value is None)
    revise: MandateChangeScope | None = Field(default=None, exclude_if=lambda value: value is None)
    retire: MandateChangeScope | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _shape(self) -> "MandateChanges":
        if self.create is None and self.revise is None and self.retire is None:
            raise ValueError("a mandate Claim scope must cover at least one change kind")
        if self.retire is not None and self.retire.basis != ("absence",):
            # A Procedure retires a Claim only on a complete source's absence.
            raise ValueError("a mandate retire scope settles only on basis absence")
        return self

    def of(self, kind: MandateChangeKind) -> MandateChangeScope | None:
        return {"create": self.create, "revise": self.revise, "retire": self.retire}[kind]

    @property
    def kinds(self) -> tuple[MandateChangeKind, ...]:
        return tuple(kind for kind in MANDATE_CHANGE_KIND_ORDER if self.of(kind) is not None)


MandateValueMember: TypeAlias = str | bool


def _value_members(
    value: tuple[MandateValueMember, ...] | None,
) -> tuple[MandateValueMember, ...] | None:
    if value is None:
        return None
    if not value or value != tuple(
        sorted(set(value), key=lambda item: json.dumps(item).encode("utf-8"))
    ):
        raise ValueError("mandate value members must be nonempty, sorted and unique")
    return value


class MandateValueCondition(_StrictMandateScopeModel):
    """Enum and bool ClaimTypes only: the values a settled change may move from and to.

    ``from`` lists the held values a change may replace (a create holds none, so
    it settles only when ``from`` is omitted); ``to`` the values it may write (a
    retirement or an inverse writes none, so it settles only when ``to`` is
    omitted). At least one is declared.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, serialize_by_alias=True
    )

    from_: tuple[MandateValueMember, ...] | None = Field(
        default=None, alias="from", exclude_if=lambda value: value is None
    )
    to: tuple[MandateValueMember, ...] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    _members = field_validator("from_", "to")(_value_members)

    @model_validator(mode="after")
    def _shape(self) -> "MandateValueCondition":
        if self.from_ is None and self.to is None:
            raise ValueError("a mandate value condition declares from, to, or both")
        return self


#: Which scope term left a changed Claim out of a settle grant.
SettleScopeTerm = Literal["claim_type", "change_kind", "basis", "value", "subject"]


class SettleScopeGap(_StrictMandateScopeModel):
    """One reason a settle grant's scope left a change out, so the whole change proposed.

    ``basis`` is the changed value's evidence basis (``None`` when the change
    carries none a grant can name).
    """

    claim_type: str
    change: MandateChangeKind
    basis: MandateBasis | None = Field(default=None, exclude_if=lambda value: value is None)
    term: SettleScopeTerm


def settle_scope_gap_key(gap: SettleScopeGap) -> bytes:
    return json.dumps(gap.model_dump(mode="json"), sort_keys=True).encode("utf-8")


__all__ = [
    "MANDATE_BASIS_ORDER",
    "MANDATE_CHANGE_KIND_ORDER",
    "MandateBasis",
    "MandateChangeKind",
    "MandateChangeScope",
    "MandateChanges",
    "MandateValueCondition",
    "MandateValueMember",
    "SettleScopeGap",
    "SettleScopeTerm",
    "settle_scope_gap_key",
]
