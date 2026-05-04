"""Typed HTTP request models matching MCP handler signatures."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from cruxible_client import contracts


class InitRequest(BaseModel):
    root_dir: str
    config_path: str | None = None
    config_yaml: str | None = None
    data_dir: str | None = None
    kit: str | None = None


class ValidateRequest(BaseModel):
    config_path: str | None = None
    config_yaml: str | None = None


class QueryRequest(BaseModel):
    query_name: str
    params: dict[str, Any] | None = None
    limit: int | None = None
    decision_record_id: str | None = None


class RenderWikiRequest(BaseModel):
    focus: list[str] = Field(default_factory=list)
    include_types: list[str] = Field(default_factory=list)
    scope: Literal["local", "evidence", "all"] | None = None
    max_per_type: int = Field(default=50, ge=1)
    all_subjects: bool = False

    @model_validator(mode="after")
    def validate_scope_compatibility(self) -> RenderWikiRequest:
        if self.all_subjects and self.scope not in (None, "all"):
            raise ValueError("all_subjects=true can only be combined with scope=all")
        return self


class IngestRequest(BaseModel):
    mapping_name: str
    data_csv: str | None = None
    data_json: str | list[dict[str, Any]] | None = None
    data_ndjson: str | None = None
    upload_id: str | None = None


class AddEntitiesRequest(BaseModel):
    entities: list[contracts.EntityInput]


class AddRelationshipsRequest(BaseModel):
    relationships: list[contracts.RelationshipInput]


class FeedbackRequest(BaseModel):
    receipt_id: str
    action: contracts.FeedbackAction
    source: contracts.FeedbackSource
    from_type: str
    from_id: str
    relationship: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] | None = None
    corrections: dict[str, Any] | None = None
    group_override: bool = False


class FeedbackBatchRequest(BaseModel):
    source: contracts.FeedbackSource
    items: list[contracts.FeedbackBatchItemInput]


class OutcomeRequest(BaseModel):
    receipt_id: str | None = None
    outcome: contracts.OutcomeValue
    anchor_type: contracts.OutcomeAnchorType = "receipt"
    anchor_id: str | None = None
    source: contracts.FeedbackSource = "human"
    outcome_code: str | None = None
    scope_hints: dict[str, Any] | None = None
    outcome_profile_key: str | None = None
    detail: dict[str, Any] | None = None


class FindCandidatesRequest(BaseModel):
    relationship_type: str
    strategy: contracts.CandidateStrategy
    match_rules: list[dict[str, str]] | None = None
    via_relationship: str | None = None
    min_overlap: float = 0.5
    min_confidence: float = 0.5
    limit: int = 20
    min_distinct_neighbors: int = 2


class ProposeGroupRequest(BaseModel):
    relationship_type: str
    members: list[contracts.MemberInput]
    thesis_text: str = ""
    thesis_facts: dict[str, Any] | None = None
    analysis_state: dict[str, Any] | None = None
    integrations_used: list[str] | None = None
    proposed_by: contracts.GroupProposedBy = "agent"
    suggested_priority: str | None = None


class ResolveGroupRequest(BaseModel):
    action: contracts.GroupAction
    rationale: str = ""
    resolved_by: contracts.GroupResolvedBy = "human"
    expected_pending_version: int


class UpdateTrustStatusRequest(BaseModel):
    trust_status: contracts.GroupTrustStatus
    reason: str = ""


class EvaluateRequest(BaseModel):
    max_findings: int = 100
    exclude_orphan_types: list[str] | None = None


class LintRequest(BaseModel):
    max_findings: int = 100
    analysis_limit: int = 200
    min_support: int = 5
    exclude_orphan_types: list[str] | None = None


class AnalyzeFeedbackRequest(BaseModel):
    relationship_type: str
    limit: int = 200
    min_support: int = 5
    decision_surface_type: str | None = None
    decision_surface_name: str | None = None
    property_pairs: list[contracts.PropertyPairInput] | None = None


class AnalyzeOutcomesRequest(BaseModel):
    anchor_type: contracts.OutcomeAnchorType
    relationship_type: str | None = None
    workflow_name: str | None = None
    query_name: str | None = None
    surface_type: str | None = None
    surface_name: str | None = None
    limit: int = 200
    min_support: int = 5


class AddConstraintRequest(BaseModel):
    name: str
    rule: str
    severity: contracts.ConstraintSeverity = "warning"
    description: str | None = None


class AddDecisionPolicyRequest(BaseModel):
    name: str
    applies_to: contracts.DecisionPolicyAppliesTo
    relationship_type: str
    effect: contracts.DecisionPolicyEffect
    match: contracts.DecisionPolicyMatchInput | None = None
    description: str | None = None
    rationale: str = ""
    query_name: str | None = None
    workflow_name: str | None = None
    expires_at: str | None = None


class WorkflowInputRequest(BaseModel):
    workflow_name: str
    input: dict[str, Any] | None = None
    decision_record_id: str | None = None


class WorkflowApplyRequest(BaseModel):
    workflow_name: str
    input: dict[str, Any] | None = None
    expected_apply_digest: str
    expected_head_snapshot_id: str | None = None
    decision_record_id: str | None = None


class DecisionRecordCreateRequest(BaseModel):
    question: str
    subject_type: str | None = None
    subject_id: str | None = None
    opened_by: Literal["human", "agent", "service"] = "human"


class DecisionRecordFinalizeRequest(BaseModel):
    final_decision: str
    decision_class: contracts.DecisionClass
    rationale: str = ""


class DecisionRecordAbandonRequest(BaseModel):
    reason: str = ""


class WorkflowTestRequest(BaseModel):
    name: str | None = None


class WorkflowLockRequest(BaseModel):
    force: bool = False


class ReloadConfigRequest(BaseModel):
    config_path: str | None = None
    config_yaml: str | None = None


class SnapshotCreateRequest(BaseModel):
    label: str | None = None


class CloneSnapshotRequest(BaseModel):
    snapshot_id: str
    root_dir: str


class WorldPublishRequest(BaseModel):
    transport_ref: str
    world_id: str
    release_id: str
    compatibility: contracts.WorldCompatibility


class WorldOverlayRequest(BaseModel):
    transport_ref: str | None = None
    world_ref: str | None = None
    kit: str | None = None
    no_kit: bool = False
    root_dir: str

    @model_validator(mode="after")
    def validate_source(self) -> WorldOverlayRequest:
        if bool((self.transport_ref or "").strip()) == bool((self.world_ref or "").strip()):
            raise ValueError("Provide exactly one of transport_ref or world_ref")
        if bool((self.kit or "").strip()) and self.no_kit:
            raise ValueError("Provide kit or no_kit, not both")
        return self


class WorldPullApplyRequest(BaseModel):
    expected_apply_digest: str
