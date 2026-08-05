"""Install-ledger records, the install phase machine, and its derived reports.

THE PHASE MACHINE (RFC "Procedure Blueprints" §4). Install is preflighted,
ordered, and crash-recoverable — deliberately NOT one ACID transaction, because
config is a file, the compile lock is a file, governance state is SQLite, and
human acceptance is asynchronous by nature. The ledger is therefore the durable
record of WHERE an install got to, so a process that dies mid-install leaves a
phase behind instead of an unattributable half-applied config::

    preparing ──► pending_acceptance ──► active        (terminal: installed)
        │                 │
        └──────► failed ◄─┘
                   │
                   ▼
              rolling_back ──► rolled_back             (terminal: undone)

    phase                ownership of its (kind, name) claims
    ------------------   ------------------------------------
    preparing            HELD
    pending_acceptance   HELD
    active               HELD
    failed               HELD  — failing is not undoing; the objects it
                                 already wrote are still its to take back
    rolling_back         HELD  — cleanup in flight
    rolled_back          RELEASED, in the same transaction as the phase change

``active`` and ``rolled_back`` are terminal in phase 1. Uninstall (which would
move an ``active`` install onward) is phase 2 work and deliberately has no
transition here yet — an install ledger that cannot say what it has NOT built
is worse than none.

WHAT AN ARTIFACT REF IS. The ledger records four fields about the thing being
installed and interprets none of them. Digest is the identity that matters:
"the same id+version arriving with a different digest" is a different artifact
and the installer must refuse it, which it can only do because the ledger kept
the digest it installed.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.primitives import canonical_json, new_id

InstallPhase = Literal[
    "preparing",
    "pending_acceptance",
    "active",
    "failed",
    "rolling_back",
    "rolled_back",
]

INSTALL_PHASES: tuple[InstallPhase, ...] = (
    "preparing",
    "pending_acceptance",
    "active",
    "failed",
    "rolling_back",
    "rolled_back",
)

OwnedObjectKind = Literal["contract", "named_query", "procedure", "enum"]

OWNED_OBJECT_KINDS: tuple[OwnedObjectKind, ...] = (
    "contract",
    "named_query",
    "procedure",
    "enum",
)

LEGAL_PHASE_TRANSITIONS: dict[InstallPhase, tuple[InstallPhase, ...]] = {
    "preparing": ("pending_acceptance", "failed"),
    "pending_acceptance": ("active", "failed"),
    "active": (),
    "failed": ("rolling_back",),
    "rolling_back": ("rolled_back",),
    "rolled_back": (),
}
"""The whole legal transition graph, in one place.

Enforcement reads THIS map rather than re-deriving the rules per call site, so
"what may follow X" has exactly one answer and a typed refusal can quote it.
"""

TERMINAL_INSTALL_PHASES: frozenset[InstallPhase] = frozenset({"active", "rolled_back"})

OWNERSHIP_HOLDING_PHASES: frozenset[InstallPhase] = frozenset(
    {"preparing", "pending_acceptance", "active", "failed", "rolling_back"}
)
"""Phases in which an install's ownership claims are still live.

``rolled_back`` is the ONLY phase that releases, and it releases in the same
transaction that commits it. Everything before it holds, including the two
phases that look like giving up:

* ``preparing`` holds because two concurrent installs racing for the same
  contract name is exactly the collision the check exists to catch.
* ``failed`` holds because failing is not undoing. An install that fails out of
  ``pending_acceptance`` may already have written config and procedure objects,
  and it still has to traverse ``rolling_back`` to take them back. Releasing at
  ``failed`` would let a fresh install claim those names, and the first
  install's rollback would then remove or overwrite objects the second one now
  owns.
* ``rolling_back`` holds for the same reason, more obviously: the cleanup is in
  flight.

The cost is that a name is not reusable until the failed install is *actually*
rolled back. That is the intended price: an install that mutated nothing still
walks a no-op ``rolling_back``/``rolled_back`` before its names free, which
makes "these names are free" mean "nobody is still holding cleanup for them"
rather than "nobody intends to finish".
"""


def legal_next_phases(phase: InstallPhase) -> tuple[InstallPhase, ...]:
    """Return the phases that may legally follow *phase*."""
    return LEGAL_PHASE_TRANSITIONS[phase]


def compute_install_object_digest(definition: Any) -> str:
    """Return the canonical content digest for one installable config object.

    ``definition`` is a pydantic model or any JSON-shaped mapping. Callers may
    also supply an already-computed ``sha256:...`` digest to the ledger
    directly; this helper exists so the installer, the customization check, and
    tests all compute the SAME digest for the same object rather than three
    nearly-identical ones.
    """
    payload = (
        definition.model_dump(mode="json", exclude_none=True)
        if isinstance(definition, BaseModel)
        else definition
    )
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def new_install_id() -> str:
    """Mint one install identifier."""
    return new_id("inst")


class ArtifactRef(BaseModel):
    """The minimal identity of an installable artifact.

    Format-agnostic ON PURPOSE. The blueprint schema is being built separately;
    binding the ledger to it would make the ledger untestable until that lands
    and would re-couple two things the RFC keeps apart. An installer for any
    artifact class fills these four fields.
    """

    artifact_kind: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_version: str = Field(min_length=1)
    artifact_digest: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True)


class ObjectReference(BaseModel):
    """One (kind, name) pointer from an owned object to another config object."""

    object_kind: OwnedObjectKind
    object_name: str = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True)


class InstallRecord(BaseModel):
    """The authoritative row for one install attempt."""

    install_id: str
    artifact: ArtifactRef
    phase: InstallPhase
    created_at: str
    updated_at: str
    actor_context: GovernedActorContext | None = None
    failure_reason: str | None = None
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class InstallPhaseEvent(BaseModel):
    """One append-only step of an install's phase history."""

    event_id: str
    install_id: str
    sequence: int
    from_phase: InstallPhase | None
    to_phase: InstallPhase
    occurred_at: str
    actor_context: GovernedActorContext | None = None
    reason: str | None = None
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class OwnedObject(BaseModel):
    """One config object an install claims authorship of.

    ``installed_digest`` is the content the install PUT there. ``customized``
    is the recorded verdict of a later comparison against what is there now —
    the flag an update must honour so a customer edit is not silently reverted.

    ``references`` is what makes dependency-blocked removal possible at all: an
    owned object declares the (kind, name) pairs it depends on, so removing an
    object can be checked against the objects other installs pointed at it. It
    records only what an installer DECLARES; see
    :class:`UninstallPreconditionReport` for what that cannot see.
    """

    install_id: str
    object_kind: OwnedObjectKind
    object_name: str
    installed_digest: str
    customized: bool = False
    current_digest: str | None = None
    references: list[ObjectReference] = Field(default_factory=list)
    recorded_at: str
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class InstallDetail(BaseModel):
    """One install with its owned objects and full phase history."""

    install: InstallRecord
    owned_objects: list[OwnedObject] = Field(default_factory=list)
    phase_history: list[InstallPhaseEvent] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class OwnershipCollision(BaseModel):
    """A (kind, name) already claimed by an ownership-holding install."""

    object_kind: OwnedObjectKind
    object_name: str
    owning_install_id: str
    owning_install_phase: InstallPhase
    installed_digest: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomizationReport(BaseModel):
    """Whether one owned object still matches what its install put there."""

    install_id: str
    object_kind: OwnedObjectKind
    object_name: str
    installed_digest: str
    current_digest: str
    customized: bool
    receipt_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class UninstallBlocker(BaseModel):
    """One recorded reason an install's object may not be removed."""

    object_kind: OwnedObjectKind
    object_name: str
    referencing_install_id: str
    referencing_install_phase: InstallPhase
    referencing_object_kind: OwnedObjectKind
    referencing_object_name: str

    model_config = ConfigDict(extra="forbid", frozen=True)


class UninstallPreconditionReport(BaseModel):
    """What the LEDGER knows about removing one install, and what it does not.

    ``blockers`` are references recorded in the ledger by OTHER installs that
    still hold their ownership claims. ``customized_objects`` are this
    install's own objects whose recorded verdict says the customer changed
    them — not a blocker, but an uninstall that discards them destroys work.

    ``unobservable_reference_sources`` is the honest limit, carried in the
    payload rather than buried in prose, because a caller that treats
    ``blocked=False`` as "safe to delete" will be wrong. Phase 1 sees ONLY
    references that an installer declared into this ledger. It does not read
    config, so a hand-written named query referencing an installed contract is
    invisible; it does not read graph state or accepted procedure pins, so a
    live procedure compiled against an installed query is invisible too.
    Closing those gaps is installer/uninstaller work (phase 2), not a query
    this table can answer.
    """

    install_id: str
    install_phase: InstallPhase
    blocked: bool
    blockers: list[UninstallBlocker] = Field(default_factory=list)
    customized_objects: list[OwnedObject] = Field(default_factory=list)
    unobservable_reference_sources: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


UNOBSERVABLE_REFERENCE_SOURCES: tuple[str, ...] = (
    "config objects this ledger did not install (hand-written or upstream)",
    "accepted procedure definitions pinned against an installed object",
    "graph state and governed groups produced by an installed object",
)
"""The reference sources phase 1 provably cannot see. Reported, never guessed."""


__all__ = [
    "INSTALL_PHASES",
    "LEGAL_PHASE_TRANSITIONS",
    "OWNED_OBJECT_KINDS",
    "OWNERSHIP_HOLDING_PHASES",
    "TERMINAL_INSTALL_PHASES",
    "UNOBSERVABLE_REFERENCE_SOURCES",
    "ArtifactRef",
    "CustomizationReport",
    "InstallDetail",
    "InstallPhase",
    "InstallPhaseEvent",
    "InstallRecord",
    "ObjectReference",
    "OwnedObject",
    "OwnedObjectKind",
    "OwnershipCollision",
    "UninstallBlocker",
    "UninstallPreconditionReport",
    "compute_install_object_digest",
    "legal_next_phases",
    "new_install_id",
]
