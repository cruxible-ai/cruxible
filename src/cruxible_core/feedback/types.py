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
from cruxible_core.governance.actors import (
    DerivedActorKind,
    GovernedActorContext,
    derived_actor_kind,
)
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.primitives import new_id
from cruxible_core.temporal import utc_now


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
    action: Literal["approve", "reject", "correct", "flag"]
    target: RelationshipInstance
    reason: str = ""
    reason_code: str | None = None
    reason_remediation_hint: FeedbackRemediationHint | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    feedback_profile_key: str | None = None
    feedback_profile_version: int | None = None
    decision_context: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_id: str | None = None
    corrections: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)

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
    action: Literal["approve", "reject", "correct", "flag"]
    target: RelationshipInstance
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    corrections: dict[str, Any] = Field(default_factory=dict)
    group_override: bool = False


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
    decision_context: dict[str, Any] = Field(default_factory=dict)
    lineage_snapshot: dict[str, Any] = Field(default_factory=dict)
    relationship_type: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)

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
