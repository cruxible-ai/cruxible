"""Runtime types for candidate group resolve."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from cruxible_core.graph.types import RelationshipInstance

SignalValue = Literal["support", "contradict", "unsure"]
"""Tri-state signal value produced by a signal source about a candidate."""

ResolutionAction = Literal["approve", "reject"]
"""Action taken on a candidate group: approve (apply) or reject (discard)."""

TrustStatus = Literal["trusted", "watch", "invalidated"]
"""Trust posture for a persisted resolution, tuned by outcome analysis."""

GroupStatus = Literal["pending_review", "auto_resolved", "applying", "resolved"]
"""Lifecycle status of a candidate group."""

GroupKind = Literal["propose", "revoke"]
"""Intent of a candidate group. ``revoke`` is reserved for future flows."""

ReviewPriority = Literal["critical", "review", "normal"]
"""Review priority bucket for a candidate group."""


class CandidateSignal(BaseModel):
    """Tri-state signal from a signal source, attached to a candidate member.

    Pair identity is implicit in the containing member.
    """

    signal_source: str
    signal: SignalValue
    evidence: str = ""


class CandidateMember(RelationshipInstance):
    """A candidate edge within a group proposal.

    Extends ``RelationshipInstance`` with signal-source evidence. ``edge_key``
    is inherited but stays ``None`` for candidates since the edge does not
    yet exist in the graph.
    """

    signals: list[CandidateSignal] = Field(default_factory=list)


class GroupResolution(BaseModel):
    """Persisted resolution of a candidate group (approve or reject)."""

    resolution_id: str  # RES-{12 lowercase hex chars}
    relationship_type: str
    group_signature: str
    action: ResolutionAction
    rationale: str = ""
    thesis_text: str = ""
    thesis_facts: dict[str, Any] = Field(default_factory=dict)
    analysis_state: dict[str, Any] = Field(default_factory=dict)
    trust_status: TrustStatus = "watch"
    trust_reason: str = ""
    confirmed: bool = False
    resolved_by: Literal["human", "agent"] = "human"
    resolved_at: datetime


class CandidateGroup(BaseModel):
    """A group of candidate edges proposed before they exist in the graph."""

    group_id: str  # GRP-{12 lowercase hex chars}
    relationship_type: str
    signature: str
    status: GroupStatus = "pending_review"
    group_kind: GroupKind = "propose"
    thesis_text: str = ""
    thesis_facts: dict[str, Any] = Field(default_factory=dict)
    analysis_state: dict[str, Any] = Field(default_factory=dict)
    signal_sources_used: list[str] = Field(default_factory=list)
    proposed_by: Literal["human", "agent"] = "agent"
    member_count: int = 0
    pending_version: int = 1
    review_priority: ReviewPriority = "normal"
    suggested_priority: str | None = None
    source_workflow_name: str | None = None
    source_workflow_receipt_id: str | None = None
    source_trace_ids: list[str] = Field(default_factory=list)
    source_step_ids: list[str] = Field(default_factory=list)
    resolution_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
