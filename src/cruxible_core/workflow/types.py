"""Workflow lock, plan, and execution types."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cruxible_core.config.schema import (
    AggregateItemsSpec,
    ApplyAllSpec,
    ApplyEntitiesSpec,
    ApplyRelationshipsSpec,
    AssertCountSpec,
    AssertExistsSpec,
    AssertNotTruncatedSpec,
    AssertSpec,
    DedupeItemsSpec,
    FilterItemsSpec,
    JoinItemsSpec,
    ListEntitiesSpec,
    ListRelationshipsSpec,
    MakeCandidatesSpec,
    MakeEntitiesSpec,
    MakeRelationshipsSpec,
    MapSignalsSpec,
    ProposeRelationshipGroupSpec,
    ShapeItemsSpec,
    StepKind,
    WorkflowType,
)
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import (
    CandidateMember,
    QuerySourceEvidence,
    SignalBucketBasis,
    SignalValue,
)
from cruxible_core.provider.types import ExecutionTrace, ProviderRuntime
from cruxible_core.receipt.types import Receipt
from cruxible_core.temporal import utc_now
from cruxible_core.workflow_execution_types import WorkflowResultMode


class _DuplicateTrackedCollection(BaseModel):
    """Mixin for workflow artifacts that report input-duplicate diagnostics.

    Tracks how many inputs were deduped and how many were dropped due to
    conflicting duplicates, plus a bounded sample for debugging.
    """

    duplicate_input_count: int = 0
    conflicting_duplicate_count: int = 0
    duplicate_examples: list[dict[str, Any]] = Field(default_factory=list)


class LockedArtifact(BaseModel):
    """Artifact details captured in a generated lock file."""

    kind: str
    uri: str
    sha256: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class LockedProvider(BaseModel):
    """Resolved provider metadata captured in a generated lock file."""

    version: str
    ref: str
    provider_entrypoint_sha256: str | None = None
    runtime: ProviderRuntime
    deterministic: bool
    side_effects: bool
    artifact: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowLock(BaseModel):
    """Generated lock file for workflow execution."""

    version: str = "1"
    config_digest: str
    lock_digest: str | None = None
    generated_at: datetime = Field(default_factory=utc_now)
    artifacts: dict[str, LockedArtifact] = Field(default_factory=dict)
    providers: dict[str, LockedProvider] = Field(default_factory=dict)


class CompiledPlanStep(BaseModel):
    """Single compiled workflow step."""

    step_id: str
    kind: StepKind
    workflow_type: WorkflowType = "utility"
    as_name: str | None = None
    query_name: str | None = None
    provider_name: str | None = None
    provider_ref: str | None = None
    provider_version: str | None = None
    provider_entrypoint_sha256: str | None = None
    artifact_name: str | None = None
    artifact_sha256: str | None = None
    params_template: dict[str, Any] = Field(default_factory=dict)
    params_preview: dict[str, Any] = Field(default_factory=dict)
    relationship_state_template: Any | None = None
    include_source: bool = False
    input_template: dict[str, Any] = Field(default_factory=dict)
    input_preview: dict[str, Any] = Field(default_factory=dict)
    assert_spec: AssertSpec | None = None
    assert_not_truncated_spec: AssertNotTruncatedSpec | None = None
    assert_count_spec: AssertCountSpec | None = None
    assert_exists_spec: AssertExistsSpec | None = None
    list_entities_spec: ListEntitiesSpec | None = None
    list_relationships_spec: ListRelationshipsSpec | None = None
    shape_items_spec: ShapeItemsSpec | None = None
    join_items_spec: JoinItemsSpec | None = None
    filter_items_spec: FilterItemsSpec | None = None
    aggregate_items_spec: AggregateItemsSpec | None = None
    dedupe_items_spec: DedupeItemsSpec | None = None
    make_candidates_spec: MakeCandidatesSpec | None = None
    map_signals_spec: MapSignalsSpec | None = None
    propose_relationship_group_spec: ProposeRelationshipGroupSpec | None = None
    make_entities_spec: MakeEntitiesSpec | None = None
    make_relationships_spec: MakeRelationshipsSpec | None = None
    apply_entities_spec: ApplyEntitiesSpec | None = None
    apply_relationships_spec: ApplyRelationshipsSpec | None = None
    apply_all_spec: ApplyAllSpec | None = None

    @property
    def canonical(self) -> bool:
        """Whether this step belongs to a canonical workflow."""
        return self.workflow_type == "canonical"


class CompiledPlan(BaseModel):
    """Compiled workflow plan artifact."""

    workflow: str
    contract_in: str
    contract_out: str | None = None
    config_digest: str
    lock_digest: str | None = None
    workflow_type: WorkflowType = "utility"
    steps: list[CompiledPlanStep]
    returns: str
    input_payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def canonical(self) -> bool:
        """Whether this plan belongs to a canonical workflow."""
        return self.workflow_type == "canonical"


class WorkflowExecutionResult(BaseModel):
    """Runtime workflow execution result."""

    workflow: str
    output: Any
    receipt: Receipt
    mode: WorkflowResultMode = "run"
    workflow_type: WorkflowType = "utility"
    apply_digest: str | None = None
    head_snapshot_id: str | None = None
    committed_snapshot_id: str | None = None
    apply_previews: dict[str, Any] = Field(default_factory=dict)
    query_receipt_ids: list[str] = Field(default_factory=list)
    read_metadata: dict[str, Any] = Field(default_factory=dict)
    traces: list[ExecutionTrace] = Field(default_factory=list)
    step_outputs: dict[str, Any] = Field(default_factory=dict)
    alias_step_ids: dict[str, str] = Field(default_factory=dict)
    step_trace_ids: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def canonical(self) -> bool:
        """Whether this result came from a canonical workflow."""
        return self.workflow_type == "canonical"


class CandidateSet(_DuplicateTrackedCollection):
    """Internal workflow artifact containing candidate relationship pairs.

    Duplicate inputs are deduped by relationship tuple, with diagnostics retained
    so proposal workflows can stay forgiving without hiding kit-author mistakes.
    """

    relationship_type: str
    candidates: list[CandidateMember] = Field(default_factory=list)
    query_receipt_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_member_type(self) -> CandidateSet:
        for candidate in self.candidates:
            if candidate.relationship_type != self.relationship_type:
                raise ValueError(
                    f"CandidateSet relationship_type '{self.relationship_type}' does not "
                    f"match candidate {candidate.from_id}->{candidate.to_id} with "
                    f"relationship_type '{candidate.relationship_type}'"
                )
        return self


class SignalBatchSignal(BaseModel):
    """Governed signal produced for a specific candidate pair.

    Signal-source context is carried by the containing ``SignalBatch``.
    """

    from_id: str
    to_id: str
    signal: SignalValue
    evidence: str = ""
    basis: SignalBucketBasis | None = None
    source_query_evidence: list[QuerySourceEvidence] = Field(default_factory=list)


class SignalBatch(BaseModel):
    """Internal workflow artifact containing one signal source's signals."""

    signal_source: str
    signals: list[SignalBatchSignal] = Field(default_factory=list)
    query_receipt_ids: list[str] = Field(default_factory=list)


class RelationshipGroupProposalArtifact(BaseModel):
    """Internal workflow artifact bridged into a governed relationship proposal."""

    relationship_type: str
    proposal_step_id: str | None = None
    candidates_from: str | None = None
    members: list[CandidateMember]
    status: Literal["ready", "no_candidates"] = "ready"
    candidate_count: int = 0
    on_empty: Literal["complete"] | None = None
    group_created: bool | None = None
    thesis_text: str = ""
    thesis_facts: dict[str, Any] = Field(default_factory=dict)
    pending_refresh_mode: Literal["replace", "retain_missing"] = "replace"
    analysis_state: dict[str, Any] = Field(default_factory=dict)
    signal_sources_used: list[str] = Field(default_factory=list)
    query_receipt_ids: list[str] = Field(default_factory=list)
    suggested_priority: str | None = None
    proposed_by: Literal["human", "agent"] = "agent"


class EntitySet(_DuplicateTrackedCollection):
    """Internal workflow artifact containing entity upserts."""

    entity_type: str
    entities: list[EntityInstance] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_member_type(self) -> EntitySet:
        for entity in self.entities:
            if entity.entity_type != self.entity_type:
                raise ValueError(
                    f"EntitySet entity_type '{self.entity_type}' does not match "
                    f"entity '{entity.entity_id}' with entity_type "
                    f"'{entity.entity_type}'"
                )
        return self


class RelationshipSet(_DuplicateTrackedCollection):
    """Internal workflow artifact containing relationship upserts."""

    relationship_type: str
    relationships: list[RelationshipInstance] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_member_type(self) -> RelationshipSet:
        for rel in self.relationships:
            if rel.relationship_type != self.relationship_type:
                raise ValueError(
                    f"RelationshipSet relationship_type '{self.relationship_type}' "
                    f"does not match relationship {rel.from_id}->{rel.to_id} with "
                    f"relationship_type '{rel.relationship_type}'"
                )
        return self


class ApplyEntitiesPreview(_DuplicateTrackedCollection):
    """Preview summary for applying an entity set."""

    entity_type: str
    create_count: int = 0
    update_count: int = 0
    noop_count: int = 0


class ApplyRelationshipsPreview(_DuplicateTrackedCollection):
    """Preview summary for applying a relationship set."""

    relationship_type: str
    create_count: int = 0
    update_count: int = 0
    noop_count: int = 0
