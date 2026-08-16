"""Runtime types for candidate group resolve."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    computed_field,
    model_serializer,
    model_validator,
)

from cruxible_core.governance.actors import (
    DerivedActorKind,
    GovernedActorContext,
    derived_actor_kind,
)
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import RelationshipInstance
from cruxible_core.temporal import utc_now


@dataclass
class SuppressedProposalMember:
    """One proposal member suppressed by existing graph or pending-group state."""

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    reason: Literal["existing_edge", "pending_proposal"]
    existing_group_id: str | None = None
    existing_group_status: str | None = None
    existing_signature: str | None = None
    source_workflow_name: str | None = None


SignalValue = Literal["support", "contradict", "unsure"]
"""Tri-state signal value produced by a signal source about a candidate."""

ResolutionAction = Literal["approve", "reject"]
"""Action taken on a candidate group: approve (apply) or reject (discard)."""

TrustStatus = Literal["trusted", "watch", "invalidated"]
"""Trust posture for a persisted resolution, tuned by outcome analysis."""

GroupStatus = Literal["pending_review", "applying", "resolved", "withdrawn", "auto_resolved"]
"""Lifecycle status of a candidate group.

Deprecated: ``auto_resolved`` is READ-ONLY legacy. It is never written again as
of 0.3 (wi-group-auto-resolve-bug) and will be removed once no shipped 0.2.x
instance can still hold such a row.

It was a dead-end label: no code path transitioned a group out of it, and
because ``find_pending_group`` and the pending unique index both key on
``pending_review``, an auto-resolved group escaped both — so re-proposing the
same signature minted a DUPLICATE row instead of rewriting the live one.
Auto-resolution now runs the real receipted approve transition, and
``auto_resolved`` survives as :attr:`GroupResolution.resolution_source`.

The literal stays admissible on READ because shipped 0.2.x kits (auto-resolve is
enabled in them) persisted rows carrying it. Dropping it from the vocabulary
made ``_row_to_group`` raise a validation error on every list/get that touched
one, so a single legacy row bricked group reads for the whole instance after an
upgrade. Those rows are NOT migrated to ``withdrawn``: nobody withdrew them, and
inventing the act would be a fabricated governance event. They are terminal —
``resolve_group`` refuses them — and they sit outside the pending unique index,
so a re-propose of the same signature opens a fresh ``pending_review`` group.

``withdrawn`` replaces the hard DELETE the empty-delta refresh used to perform.
A pending group whose delta went empty is a governance artifact — it was
proposed, it was reviewed against, and its members are evidence — so it is
retired in place rather than erased. ``withdrawn`` is outside the pending unique
index, so a later re-propose of the same signature opens a fresh pending group.
"""

ResolutionSource = Literal["review", "auto_resolved"]
"""How a resolution came about: an explicit review, or policy auto-resolution."""

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
    resolution_source: ResolutionSource = "review"
    resolved_at: datetime
    resolved_actor_context: GovernedActorContext | None = None
    receipt_id: str | None = Field(
        default=None,
        description=(
            "Mutation receipt that resolved this group. Approvals are also "
            "reachable through the provenance stamped on the edges they created; "
            "rejections create no edges, so without this field a rejection joins "
            "to nothing. Resolutions predating this field load with null."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_by(self) -> DerivedActorKind:
        """Deprecated: read-only projection of the resolving actor's derived kind.

        The caller-declared ``resolved_by`` axis is retired — it was a claim, not
        evidence. This re-emits the old field name as a value DERIVED from
        ``resolved_actor_context`` (exactly what the ``resolved_by`` SQL column
        already stores) so 0.2.x readers keep parsing. Never writable; removal
        follows 0.3. Read ``resolved_actor_context`` instead.
        """
        return derived_actor_kind(self.resolved_actor_context)


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def proposed_by(self) -> DerivedActorKind:
        """Deprecated: read-only projection of the proposing actor's derived kind.

        Same retirement as :attr:`GroupResolution.resolved_by`: re-emitted under
        the old name as a value derived from ``proposed_actor_context``, which is
        what the ``proposed_by`` SQL column already stores. Removal follows 0.3.
        """
        return derived_actor_kind(self.proposed_actor_context)
