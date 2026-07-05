"""Runtime types for candidate group resolve."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_serializer,
    model_validator,
)

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.temporal import utc_now

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


class SignalBucketBasis(BaseModel):
    """Auditable basis for a tri-state signal bucket decision."""

    mode: Literal["score", "enum"]
    path: str
    value: StrictStr | StrictInt | StrictFloat
    matched: str

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_value_matches_mode(self) -> SignalBucketBasis:
        if self.mode == "score" and isinstance(self.value, str):
            raise ValueError("score signal basis value must be numeric")
        if self.mode == "enum" and not isinstance(self.value, str):
            raise ValueError("enum signal basis value must be a string")
        return self


class CandidateSignal(BaseModel):
    """Tri-state signal from a signal source, attached to a candidate member.

    Pair identity is implicit in the containing member.
    """

    signal_source: str
    signal: SignalValue
    evidence: str = ""
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    basis: SignalBucketBasis | None = None


class QuerySourceEvidence(BaseModel):
    """Compact query evidence attached to proposed relationship members."""

    query_receipt_id: str
    row_index: int | None = None
    feedback_addressable: bool = True
    source_step: str | None = None
    row_shape: Literal["relationship", "path", "entity", "unknown"] = "unknown"
    relationship: dict[str, Any] | None = None
    path: list[dict[str, Any]] | None = None
    entry: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    entity: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_row_addressability(self) -> QuerySourceEvidence:
        if self.row_index is not None and self.row_index < 0:
            raise ValueError("query source evidence row_index must be non-negative")
        if self.row_index is None:
            self.feedback_addressable = False
        return self

    @model_serializer(mode="wrap")
    def _serialize_compact(self, handler: Any) -> dict[str, Any]:
        data = {key: value for key, value in handler(self).items() if value is not None}
        if self.row_index is not None and self.feedback_addressable is True:
            data.pop("feedback_addressable", None)
        return data


class CandidateMember(RelationshipInstance):
    """A candidate edge within a group proposal.

    Extends ``RelationshipInstance`` with signal-source evidence. ``edge_key``
    is inherited but stays ``None`` for candidates since the edge does not
    yet exist in the graph.
    """

    signals: list[CandidateSignal] = Field(default_factory=list)
    source_query_evidence: list[QuerySourceEvidence] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    evidence_rationale: str | None = None

    def as_relationship(self) -> RelationshipInstance:
        """Return this candidate as a graph relationship without governance fields."""
        return RelationshipInstance(
            relationship_type=self.relationship_type,
            from_type=self.from_type,
            from_id=self.from_id,
            to_type=self.to_type,
            to_id=self.to_id,
            properties=dict(self.properties),
        )


def is_unevidenced_support_signal(signal: CandidateSignal) -> bool:
    """Return whether a support signal lacks signal-attributable evidence."""
    if signal.signal != "support":
        return False
    if signal.evidence.strip():
        return False
    if signal.evidence_refs:
        return False
    return True


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
    trust_actor_context: GovernedActorContext | None = None
    confirmed: bool = False
    resolved_by: Literal["human", "agent"] = "human"
    resolved_at: datetime
    resolved_actor_context: GovernedActorContext | None = None


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
    source_query_receipt_ids: list[str] = Field(default_factory=list)
    source_trace_ids: list[str] = Field(default_factory=list)
    source_step_ids: list[str] = Field(default_factory=list)
    resolution_id: str | None = None
    proposed_actor_context: GovernedActorContext | None = None
    created_at: datetime = Field(default_factory=utc_now)
