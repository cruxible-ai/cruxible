"""Typed errors for blueprint parsing, validation, and lowering.

Every error in this module is *self-correcting*: it names the field path that
failed, what was found, and what shape would have been accepted. Blueprints are
authored by publishers and by agents, so a refusal that does not say how to fix
itself costs a round trip.

    CoreError
    └── BlueprintError
        ├── BlueprintValidationError    (document shape / cross-reference)
        ├── BlueprintDigestError        (canonicalization or attachment manifest)
        ├── BlueprintUnsupportedError   (parsed, but not executable in this core)
        └── BlueprintBindingError       (slot -> provider resolution at lowering)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_core.errors import CoreError

_MAX_DISPLAY_ISSUES = 10
_MAX_DISPLAY_CANDIDATES = 5

BillingMode = Literal["platform", "byok"]
"""Billing-compatibility vocabulary shared by a slot and a provider candidate.

It lives here rather than in :mod:`.schema` because
:class:`BlueprintSlotCandidate` -- the catalog side of the same constraint --
lives here, and :mod:`.schema` imports this module. One definition means the
constraint a slot declares and the fact a candidate reports cannot drift.
"""


class BlueprintIssue(BaseModel):
    """One field-pathed validation problem.

    ``path`` is a dotted/bracketed path into the *document as authored*
    (``procedures[0].steps[3].provider``), never into the internal model tree.
    ``expected`` carries the allowed values or the expected shape so the author
    can correct the document without opening the schema source.
    """

    path: str
    message: str
    expected: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)

    def render(self) -> str:
        rendered = f"{self.path or '<document>'}: {self.message}"
        if self.expected:
            rendered += f" (expected: {self.expected})"
        return rendered


class BlueprintError(CoreError):
    """Base for every blueprint-format failure."""


class BlueprintValidationError(BlueprintError):
    """A blueprint document is malformed or internally inconsistent."""

    error_code = "blueprint_invalid"

    def __init__(self, summary: str, issues: Sequence[BlueprintIssue] | None = None) -> None:
        self.summary = summary
        self.issues: list[BlueprintIssue] = list(issues or [])
        super().__init__(summary)

    def __str__(self) -> str:
        if not self.issues:
            return self.summary
        shown = [issue.render() for issue in self.issues[:_MAX_DISPLAY_ISSUES]]
        detail = "; ".join(shown)
        if len(self.issues) > _MAX_DISPLAY_ISSUES:
            detail += f" ... and {len(self.issues) - _MAX_DISPLAY_ISSUES} more issue(s)"
        return f"{self.summary}: {detail}"

    @property
    def paths(self) -> list[str]:
        """Return the failing field paths, in report order."""
        return [issue.path for issue in self.issues]


class BlueprintDigestError(BlueprintError):
    """A blueprint could not be canonicalized or its attachments were unusable."""

    error_code = "blueprint_digest_failed"


class BlueprintUnsupportedError(BlueprintError):
    """The document is valid, but names machinery this core cannot execute.

    Parsing accepts triggers and pipelines so that a publisher can author the
    whole artifact today and so tooling (catalogs, the railway visualization)
    can read it. Lowering refuses them, because there is no trigger runtime to
    lower them onto.
    """

    error_code = "blueprint_feature_unsupported"

    def __init__(self, feature: str, *, work_item: str, detail: str | None = None) -> None:
        self.feature = feature
        self.work_item = work_item
        self.detail = detail
        message = (
            f"Blueprint feature '{feature}' is parsed but not yet supported for execution; "
            f"it is tracked by {work_item}."
        )
        if detail:
            message += f" {detail}"
        super().__init__(message)


class BlueprintSlotCandidate(BaseModel):
    """A provider offered to the binder, described by what it declares.

    A candidate carries every fact a compute slot can constrain: its contracts,
    the billing modes it can run under, and the capability tags it claims.
    Anything the candidate leaves empty is *not* an unconstrained wildcard --
    lowering cannot certify a compatibility it was never told about, so an
    empty ``billing`` fails a slot's billing constraint, and missing capability
    tags fail a slot that requires them.
    """

    name: str
    contract_in: str | None = None
    contract_out: str | None = None
    billing: tuple[BillingMode, ...] = Field(default_factory=tuple)
    capabilities: tuple[str, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(extra="forbid", frozen=True)

    def describe(self) -> str:
        """Render the candidate's declared facts for a refusal message."""
        parts = [
            f"contract_in='{self.contract_in}'",
            f"contract_out='{self.contract_out}'",
        ]
        if self.billing:
            parts.append(f"billing={list(self.billing)}")
        if self.capabilities:
            parts.append(f"capabilities={list(self.capabilities)}")
        return ", ".join(parts)


class BlueprintBindingError(BlueprintError):
    """A compute slot has no usable provider binding.

    Raised when a needed slot is unbound, when the bound provider is not in the
    candidate catalog at all, and when the bound candidate fails any constraint
    the slot declares (contracts, billing modes, capabilities). Each individual
    violation is carried as its own field-pathed :class:`BlueprintIssue`.

    The message lists near-matching candidates and *why each one failed*, which
    is the discoverability lesson from the pilot: an unbindable slot that only
    says "unbound" forces the operator to go read every provider definition.
    """

    error_code = "blueprint_slot_unbound"

    def __init__(
        self,
        slot: str,
        *,
        contract_in: str,
        contract_out: str,
        reason: str,
        issues: Sequence[BlueprintIssue] | None = None,
        near_matches: Iterable[tuple[BlueprintSlotCandidate, str]] = (),
    ) -> None:
        self.slot = slot
        self.contract_in = contract_in
        self.contract_out = contract_out
        self.reason = reason
        self.issues: list[BlueprintIssue] = list(issues or [])
        self.near_matches: list[tuple[BlueprintSlotCandidate, str]] = list(near_matches)
        super().__init__(self._render())

    def _render(self) -> str:
        message = (
            f"Compute slot '{self.slot}' could not be bound: {self.reason}. "
            f"The slot requires contract_in='{self.contract_in}' and "
            f"contract_out='{self.contract_out}'."
        )
        if self.issues:
            shown = [issue.render() for issue in self.issues[:_MAX_DISPLAY_ISSUES]]
            message += f" Violations: {'; '.join(shown)}."
            if len(self.issues) > _MAX_DISPLAY_ISSUES:
                message += f" ... and {len(self.issues) - _MAX_DISPLAY_ISSUES} more violation(s)."
        if not self.near_matches:
            message += (
                " No candidate provider declared either contract. Pass a binding for this slot "
                "via bindings={'" + self.slot + "': '<provider name>'}, and offer the provider "
                "in candidates=[...] so its contracts, billing modes, and capabilities can be "
                "checked."
            )
            return message
        shown_candidates = self.near_matches[:_MAX_DISPLAY_CANDIDATES]
        rendered = "; ".join(
            f"'{candidate.name}' ({candidate.describe()}) — {why}"
            for candidate, why in shown_candidates
        )
        message += f" Near matches: {rendered}"
        if len(self.near_matches) > _MAX_DISPLAY_CANDIDATES:
            message += f" ... and {len(self.near_matches) - _MAX_DISPLAY_CANDIDATES} more"
        return message

    @property
    def paths(self) -> list[str]:
        """Return the failing field paths, in report order."""
        return [issue.path for issue in self.issues]

    @property
    def near_match_names(self) -> list[str]:
        """Return the near-matching candidate provider names, in report order."""
        return [candidate.name for candidate, _ in self.near_matches]
