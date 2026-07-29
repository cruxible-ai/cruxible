"""Feedback and outcome types for the learning loop."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from cruxible_core.config.schema import (
    FeedbackRemediationHint,
    OutcomeAnchorType,
    OutcomeLabel,
    OutcomeRemediationHint,
)
from cruxible_core.deprecation import (
    FEEDBACK_SOURCE_INPUT,
    OUTCOME_SOURCE_INPUT,
    accept_deprecated_model_input,
)
from cruxible_core.governance.actors import (
    DerivedActorKind,
    GovernedActorContext,
    derived_actor_kind,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.primitives import new_id
from cruxible_core.temporal import utc_now

# The WRITE vocabulary: what a caller may ask for today.
FeedbackAction = Literal["accept", "reject", "correct"]

# The compatibility INPUT vocabulary. ``approve`` delegates to ``accept`` with
# a warning. ``flag`` reaches the service validation seam solely so its
# structured deprecation refusal can teach the replacement; no write path
# persists it.
FeedbackInputAction = Literal["accept", "reject", "correct", "approve", "flag"]

# The READ vocabulary: what a stored row may legally contain.
#
# These are deliberately DIFFERENT. ``flag`` was removed as a write in 2026-07
# (it un-approved an edge to ``pending`` while storing no annotation, so it
# destroyed the reviewer's signal), but 0.2.x instances have already persisted
# rows with ``action='flag'`` and those rows are permanent -- the feedback store
# is append-only history, not a mutable projection. Every read path
# (``FeedbackStore._row_to_feedback``, and therefore get/list/analysis/CLI
# rendering) reconstructs through :class:`FeedbackRecord`, so narrowing the
# STORED vocabulary would make historical instances raise ValidationError on an
# ordinary list.
#
# So the record model tolerates both historical ``approve`` and ``flag`` rows
# on read. Compatibility INPUT types admit ``approve`` as a warned alias that
# new writes normalize to ``accept``; they admit ``flag`` only far enough to
# reach the structured service refusal. The applier retains an ``approve`` read
# branch for historical records but has no ``flag`` branch.
StoredFeedbackAction = Literal["accept", "approve", "reject", "correct", "flag"]

RETIRED_FEEDBACK_ACTIONS: frozenset[str] = frozenset({"flag"})
"""Actions readable from history but refused on every write path."""


class FeedbackRecord(BaseModel):
    """Feedback on a query result or specific relationship.

    The caller-declared ``human``/``agent`` ``source`` axis is RETIRED. It was
    never reconciled with ``actor_context.actor_type``, it defaulted to
    ``"human"``, and it gated the reason-code requirement that exists precisely
    to hold non-human writers to a structured, analyzable reason — so an agent
    could opt out of the rule written for it by declaring itself a person.
    Readers derive the kind from ``actor_context`` via
    :func:`cruxible_core.governance.actors.derived_actor_kind`.
    """

    feedback_id: str = Field(default_factory=lambda: new_id("FB"))
    receipt_id: str | None = None
    action: StoredFeedbackAction
    """READ vocabulary -- see :data:`StoredFeedbackAction`.

    Wider than what any caller may write: this model is what the store
    reconstructs historical rows into, and 0.2.x history contains ``flag``.
    Write paths are narrowed at the input types and at the service seam.
    """

    target: RelationshipInstance
    reason: str = ""
    reason_code: str | None = None
    reason_remediation_hint: FeedbackRemediationHint | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    feedback_profile_key: str | None = None
    feedback_profile_version: int | None = None
    feedback_profile_digest: str | None = None
    """Digest of the profile body this row was coded under; the drift signal."""

    decision_context: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = None
    corrections: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def accept_deprecated_source(cls, value: Any) -> Any:
        """Accept and ignore the caller-declared source compatibility input."""
        return accept_deprecated_model_input(
            value,
            field="source",
            notice=FEEDBACK_SOURCE_INPUT,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source(self) -> DerivedActorKind:
        """Deprecated: read-only projection of ``derived_actor_kind(actor_context)``.

        The caller-DECLARED ``human``/``agent`` axis is gone — it was never
        reconciled with the actor context and it gated the reason-code rule it
        was supposed to be held to. This re-emits the same field name as a
        DERIVED value so a 0.2.x reader keeps parsing, and it is exactly what the
        ``source`` SQL column already stores. Never writable; scheduled for
        removal in the release after 0.3. Read ``actor_context`` instead.
        """
        return derived_actor_kind(self.actor_context)


class FeedbackBatchItem(BaseModel):
    """Input payload for one batch feedback item."""

    receipt_id: str
    action: FeedbackInputAction
    """Compatibility input vocabulary; service validation still refuses ``flag``."""

    target: RelationshipInstance
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    corrections: dict[str, Any] = Field(default_factory=dict)
    group_override: bool = False
    source: str | None = None
    """Deprecated and ignored; actor kind is derived from ``actor_context``."""


class OutcomeRecord(BaseModel):
    """Record of what actually happened after a decision was made."""

    outcome_id: str = Field(default_factory=lambda: new_id("OUT"))
    receipt_id: str
    anchor_type: OutcomeAnchorType = "receipt"
    anchor_id: str | None = None
    outcome: OutcomeLabel
    outcome_code: str | None = None
    outcome_remediation_hint: OutcomeRemediationHint | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    outcome_profile_key: str | None = None
    outcome_profile_version: int | None = None
    outcome_profile_digest: str | None = None
    """Digest of the profile body this row was coded under; the drift signal."""

    decision_context: dict[str, Any] = Field(default_factory=dict)
    lineage_snapshot: dict[str, Any] = Field(default_factory=dict)
    relationship_type: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def accept_deprecated_source(cls, value: Any) -> Any:
        """Accept and ignore the caller-declared source compatibility input."""
        return accept_deprecated_model_input(
            value,
            field="source",
            notice=OUTCOME_SOURCE_INPUT,
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source(self) -> DerivedActorKind:
        """Deprecated: read-only projection of ``derived_actor_kind(actor_context)``.

        Same retirement as :attr:`FeedbackRecord.source` — re-emitted under the
        old name as a derived value for 0.2.x readers, matching the denormalized
        ``source`` SQL column. Removal follows 0.3.
        """
        return derived_actor_kind(self.actor_context)

    @model_validator(mode="after")
    def default_anchor_id(self) -> OutcomeRecord:
        if self.anchor_type == "receipt" and self.anchor_id is None:
            self.anchor_id = self.receipt_id
        elif not self.anchor_id:
            raise ValueError(f"{self.anchor_type} outcomes require anchor_id")
        return self
