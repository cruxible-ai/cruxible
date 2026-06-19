"""Feedback and outcome types for the learning loop."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cruxible_core.config.schema import (
    FeedbackRemediationHint,
    OutcomeAnchorType,
    OutcomeLabel,
    OutcomeRemediationHint,
)
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.primitives import new_id
from cruxible_core.temporal import utc_now


class FeedbackRecord(BaseModel):
    """Human or AI feedback on a query result or specific relationship."""

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
    source: Literal["human", "agent"] = "human"
    model_id: str | None = None
    corrections: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)


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
    source: Literal["human", "agent"] = "human"
    detail: dict[str, Any] = Field(default_factory=dict)
    actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def default_anchor_id(self) -> OutcomeRecord:
        if self.anchor_type == "receipt" and self.anchor_id is None:
            self.anchor_id = self.receipt_id
        elif not self.anchor_id:
            raise ValueError(f"{self.anchor_type} outcomes require anchor_id")
        return self
