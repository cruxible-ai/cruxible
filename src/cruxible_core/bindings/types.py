"""Types for the compute-slot binding ledger.

A BINDING is a deployment record: it says which provider this install resolved
one of a procedure's compute slots to, and it lives in state, never in config.
A procedure pins the slot INTERFACE (the contract names in and out); the
binding names the provider that satisfies it here. Rebinding is a governed,
receipted state update — it never edits config and never re-runs acceptance.

Slot identity is deliberately OPAQUE at this layer: ``install_id``,
``slot_name``, ``provider_name``, and the contract names are plain strings that
the caller supplies from whatever declares them. Nothing here imports a
blueprint or procedure schema, so the ledger can be reasoned about (and tested)
without the authoring surface that produces its inputs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.primitives import new_id
from cruxible_core.temporal import utc_now

BindingStatus = Literal["active", "retired"]
"""Lifecycle of a binding row. Exactly one ``active`` row per install+slot."""

BINDING_STATUSES: tuple[BindingStatus, ...] = ("active", "retired")

BindingChangeKind = Literal["bind", "rebind", "retire"]
"""What produced a revision row in the binding history."""

BINDING_CHANGE_KINDS: tuple[BindingChangeKind, ...] = ("bind", "rebind", "retire")


class SlotInterface(BaseModel):
    """The compute-slot interface a procedure pins, as the caller declares it.

    ``allowed_billing_modes`` is the slot's constraint on how the bound provider
    may be paid for. ``None`` means unconstrained; an empty tuple would mean "no
    mode is acceptable", which is never a useful declaration, so it is rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_name: str = Field(min_length=1)
    contract_in: str = Field(min_length=1)
    contract_out: str = Field(min_length=1)
    allowed_billing_modes: tuple[str, ...] | None = None
    requires_third_party_consent: bool = False
    """Whether binding a third-party provider needs recorded operator consent."""

    @field_validator("allowed_billing_modes")
    @classmethod
    def _reject_empty_constraint(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and not value:
            raise ValueError(
                "allowed_billing_modes must name at least one mode; "
                "omit it entirely to leave billing unconstrained"
            )
        return value


class ProviderDescriptor(BaseModel):
    """What a candidate provider declares about itself.

    Supplied by the CALLER on both sides of the check. The binding service never
    reads config or a provider registry: it compares two caller-supplied
    declarations and records the comparison it made.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str = Field(min_length=1)
    contract_in: str = Field(min_length=1)
    contract_out: str = Field(min_length=1)
    billing_mode: str = Field(min_length=1)
    third_party: bool = False


class NearMatchCandidate(BaseModel):
    """One candidate provider that failed to satisfy a slot, and why.

    ``mismatches`` is ordered and human-readable: every reason the candidate was
    rejected, never just the first. A near-match report that stops at the first
    failure sends the operator round the loop once per reason.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    contract_in: str
    contract_out: str
    billing_mode: str
    matched_contract_in: bool
    matched_contract_out: bool
    mismatches: tuple[str, ...]

    @property
    def matched_sides(self) -> int:
        """How many of the two contract sides lined up (the ranking key)."""
        return int(self.matched_contract_in) + int(self.matched_contract_out)


class NearMatchReport(BaseModel):
    """Ranked near-match reporting for an unbindable slot.

    Ranked by contract sides matched (descending), then by mismatch count
    (ascending), then by provider name — a total order, so the report is
    reproducible rather than dependent on candidate submission order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_name: str
    contract_in: str
    contract_out: str
    allowed_billing_modes: tuple[str, ...] | None = None
    candidates: tuple[NearMatchCandidate, ...] = ()

    def render(self) -> str:
        """Render the report as the operator-facing refusal text."""
        header = (
            f"no provider satisfies slot '{self.slot_name}' "
            f"(contract_in='{self.contract_in}', contract_out='{self.contract_out}')"
        )
        if not self.candidates:
            return header + "; no candidate providers were offered"
        lines = [f"{header}; {len(self.candidates)} candidate(s) nearly matched:"]
        for position, candidate in enumerate(self.candidates, start=1):
            reasons = "; ".join(candidate.mismatches)
            lines.append(
                f"  {position}. '{candidate.provider_name}' "
                f"[{candidate.matched_sides}/2 contract sides matched]: {reasons}"
            )
        return "\n".join(lines)


class SlotBinding(BaseModel):
    """The current binding row for one install+slot.

    THE PINNED SLOT INTERFACE IS PART OF THE ROW. ``contract_in``,
    ``contract_out``, ``allowed_billing_modes`` and ``requires_third_party_consent``
    are the slot interface this binding was FIRST validated against — recorded
    at bind time, never a copy of the provider's declaration, and never revised.
    They are what makes a later rebind checkable without trusting the rebind
    request: the ledger re-reads its own pinned interface (:meth:`pinned_slot`)
    rather than whatever the caller says the slot is now, so a rebind cannot
    redefine the contract it is supposedly being checked against.

    They are deliberately absent from :class:`SlotBindingRevision`: a revision
    row records what CHANGED, and the pinned interface is the one thing that
    cannot.
    """

    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(default_factory=lambda: new_id("bnd", length=16, separator="_"))
    install_id: str
    slot_name: str
    provider_name: str
    contract_in: str
    contract_out: str
    allowed_billing_modes: tuple[str, ...] | None = None
    requires_third_party_consent: bool = False
    billing_mode: str
    third_party_consent: bool = False
    consent_actor_id: str | None = None
    consent_org_id: str | None = None
    consent_at: datetime | None = None
    revision: int = 1
    status: BindingStatus = "active"
    bound_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    retired_at: datetime | None = None
    actor_context: GovernedActorContext | None = None
    receipt_id: str | None = None

    def pinned_slot(self) -> SlotInterface:
        """Return the slot interface this binding was pinned to at bind time.

        This — never the rebind request — is what a rebind is validated against.
        A caller who supplies a different interface is describing a different
        slot, and the ledger says so rather than adopting it.
        """
        return SlotInterface(
            slot_name=self.slot_name,
            contract_in=self.contract_in,
            contract_out=self.contract_out,
            allowed_billing_modes=self.allowed_billing_modes,
            requires_third_party_consent=self.requires_third_party_consent,
        )


class SlotBindingRevision(BaseModel):
    """One append-only history row: the binding as of a single revision."""

    model_config = ConfigDict(extra="forbid")

    binding_id: str
    revision: int
    change_kind: BindingChangeKind
    install_id: str
    slot_name: str
    provider_name: str
    contract_in: str
    contract_out: str
    billing_mode: str
    third_party_consent: bool = False
    consent_actor_id: str | None = None
    consent_org_id: str | None = None
    consent_at: datetime | None = None
    status: BindingStatus = "active"
    note: str | None = None
    recorded_at: datetime = Field(default_factory=utc_now)
    actor_context: GovernedActorContext | None = None
    receipt_id: str | None = None


class BindingWriteResult(BaseModel):
    """Result of a receipted binding write."""

    model_config = ConfigDict(extra="forbid")

    binding: SlotBinding
    change_kind: BindingChangeKind
    previous_provider_name: str | None = None
    previous_revision: int | None = None
    receipt_id: str | None = None


class SlotBindingListResult(BaseModel):
    """Standard list envelope over current binding rows."""

    model_config = ConfigDict(extra="forbid")

    items: list[SlotBinding] = Field(default_factory=list)
    total: int = 0
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    read_revision: int | None = None


class SlotBindingHistoryResult(BaseModel):
    """Standard list envelope over one binding's revision history."""

    model_config = ConfigDict(extra="forbid")

    binding_id: str
    items: list[SlotBindingRevision] = Field(default_factory=list)
    total: int = 0
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    read_revision: int | None = None


__all__ = [
    "BINDING_CHANGE_KINDS",
    "BINDING_STATUSES",
    "BindingChangeKind",
    "BindingStatus",
    "BindingWriteResult",
    "NearMatchCandidate",
    "NearMatchReport",
    "ProviderDescriptor",
    "SlotBinding",
    "SlotBindingHistoryResult",
    "SlotBindingListResult",
    "SlotBindingRevision",
    "SlotInterface",
]
