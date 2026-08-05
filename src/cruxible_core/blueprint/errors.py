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

from pydantic import BaseModel, ConfigDict

from cruxible_core.errors import CoreError

_MAX_DISPLAY_ISSUES = 10
_MAX_DISPLAY_CANDIDATES = 5


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
    """A provider offered to the binder, described by its declared contracts."""

    name: str
    contract_in: str | None = None
    contract_out: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class BlueprintBindingError(BlueprintError):
    """A required compute slot has no usable provider binding.

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
        near_matches: Iterable[tuple[BlueprintSlotCandidate, str]] = (),
    ) -> None:
        self.slot = slot
        self.contract_in = contract_in
        self.contract_out = contract_out
        self.reason = reason
        self.near_matches: list[tuple[BlueprintSlotCandidate, str]] = list(near_matches)
        super().__init__(self._render())

    def _render(self) -> str:
        message = (
            f"Compute slot '{self.slot}' could not be bound: {self.reason}. "
            f"The slot requires contract_in='{self.contract_in}' and "
            f"contract_out='{self.contract_out}'."
        )
        if not self.near_matches:
            message += (
                " No candidate provider declared either contract. Pass a binding for this slot "
                "via bindings={'" + self.slot + "': '<provider name>'}."
            )
            return message
        shown = self.near_matches[:_MAX_DISPLAY_CANDIDATES]
        rendered = "; ".join(
            f"'{candidate.name}' (contract_in='{candidate.contract_in}', "
            f"contract_out='{candidate.contract_out}') — {why}"
            for candidate, why in shown
        )
        message += f" Near matches: {rendered}"
        if len(self.near_matches) > _MAX_DISPLAY_CANDIDATES:
            message += f" ... and {len(self.near_matches) - _MAX_DISPLAY_CANDIDATES} more"
        return message

    @property
    def near_match_names(self) -> list[str]:
        """Return the near-matching candidate provider names, in report order."""
        return [candidate.name for candidate, _ in self.near_matches]
