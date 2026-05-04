"""Shared Pydantic contracts for MCP tools.

Single source of truth for tool return shapes and constrained input types.
Both handlers.py and tools.py import from here.
FastMCP auto-generates outputSchema from the BaseModel return annotations.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ── Constrained input types ───────────────────────────────────────────

ConstraintSeverity = Literal["warning", "error"]
FeedbackAction = Literal["approve", "reject", "correct", "flag"]
FeedbackSource = Literal["human", "agent"]
OutcomeValue = Literal["correct", "incorrect", "partial", "unknown"]
OutcomeAnchorType = Literal["resolution", "receipt"]
ResourceType = Literal["entities", "edges", "receipts", "feedback", "outcomes"]
CandidateStrategy = Literal["property_match", "shared_neighbors"]
GroupAction = Literal["approve", "reject"]
GroupResolvedBy = Literal["human", "agent"]
GroupStatus = Literal["pending_review", "auto_resolved", "applying", "resolved", "suppressed"]
GroupProposedBy = Literal["human", "agent"]
GroupTrustStatus = Literal["trusted", "watch", "invalidated"]
DecisionPolicyAppliesTo = Literal["query", "workflow"]
DecisionPolicyEffect = Literal["suppress", "require_review"]
DecisionClass = Literal["recommended", "rejected", "deferred", "escalated"]
WorldCompatibility = Literal["data_only", "additive_schema", "breaking"]


# ── Structured input types ───────────────────────────────────────────


class RelationshipInput(BaseModel):
    from_type: str
    from_id: str
    relationship: str
    to_type: str
    to_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class EntityInput(BaseModel):
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class SignalInput(BaseModel):
    integration: str
    signal: Literal["support", "contradict", "unsure"]
    evidence: str = ""


class EdgeTargetInput(BaseModel):
    from_type: str
    from_id: str
    relationship: str
    to_type: str
    to_id: str
    edge_key: int | None = None


class MemberInput(BaseModel):
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relationship_type: str
    signals: list[SignalInput] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class SuppressedProposalMember(BaseModel):
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


class PropertyPairInput(BaseModel):
    from_property: str
    to_property: str


class FeedbackBatchItemInput(BaseModel):
    receipt_id: str
    action: FeedbackAction
    target: EdgeTargetInput
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    corrections: dict[str, Any] | None = None
    group_override: bool = False


class DecisionPolicyMatchInput(BaseModel):
    from_match: dict[str, Any] = Field(default_factory=dict, alias="from")
    to: dict[str, Any] = Field(default_factory=dict)
    edge: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}



# ── Tool return contracts ─────────────────────────────────────────────


class InitResult(BaseModel):
    instance_id: str
    status: str
    warnings: list[str] = Field(default_factory=list)


class ValidateResult(BaseModel):
    valid: bool
    name: str
    entity_types: list[str]
    relationships: list[str]
    named_queries: list[str]
    warnings: list[str]


class IngestResult(BaseModel):
    records_ingested: int
    records_updated: int = 0
    mapping: str
    entity_type: str | None
    relationship_type: str | None
    receipt_id: str | None = None


class QueryToolResult(BaseModel):
    results: list[dict[str, Any]]
    receipt_id: str | None
    receipt: dict[str, Any] | None
    total_results: int
    truncated: bool = False
    steps_executed: int
    param_hints: "QueryParamHints | None" = None
    policy_summary: dict[str, int] = Field(default_factory=dict)


class DecisionRecordResult(BaseModel):
    record: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


class DecisionRecordListResult(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class DecisionEventListResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class FeedbackResult(BaseModel):
    feedback_id: str
    applied: bool
    receipt_id: str | None = None


class FeedbackBatchResult(BaseModel):
    feedback_ids: list[str] = Field(default_factory=list)
    applied_count: int
    total: int
    receipt_id: str | None = None


class OutcomeResult(BaseModel):
    outcome_id: str


class OutcomeProfileResult(BaseModel):
    found: bool
    profile_key: str | None = None
    anchor_type: OutcomeAnchorType
    profile: dict[str, Any] = Field(default_factory=dict)


class ListResult(BaseModel):
    items: list[dict[str, Any]]
    total: int


class CandidatesResult(BaseModel):
    candidates: list[dict[str, Any]]
    total: int


class EvaluateResult(BaseModel):
    entity_count: int
    edge_count: int
    findings: list[dict[str, Any]]
    summary: dict[str, int]
    constraint_summary: dict[str, int] = Field(default_factory=dict)
    quality_summary: dict[str, int] = Field(default_factory=dict)


class LintSummary(BaseModel):
    config_warning_count: int = 0
    compatibility_warning_count: int = 0
    evaluation_finding_count: int = 0
    feedback_report_count: int = 0
    feedback_issue_count: int = 0
    outcome_report_count: int = 0
    outcome_issue_count: int = 0


class SampleResult(BaseModel):
    entities: list[dict[str, Any]]
    entity_type: str
    count: int


class AddRelationshipResult(BaseModel):
    added: int
    updated: int
    receipt_id: str | None = None


class AddEntityResult(BaseModel):
    entities_added: int
    entities_updated: int
    receipt_id: str | None = None


class AddConstraintResult(BaseModel):
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = Field(default_factory=list)


class GetEntityResult(BaseModel):
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GetRelationshipResult(BaseModel):
    found: bool
    from_type: str
    from_id: str
    relationship_type: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class QueryParamHints(BaseModel):
    entry_point: str
    required_params: list[str] = Field(default_factory=list)
    primary_key: str | None = None
    example_ids: list[str] = Field(default_factory=list)


class StatsResult(BaseModel):
    entity_count: int
    edge_count: int
    entity_counts: dict[str, int] = Field(default_factory=dict)
    relationship_counts: dict[str, int] = Field(default_factory=dict)
    head_snapshot_id: str | None = None


class ServerInfoResult(BaseModel):
    agent_mode: bool
    state_dir: str
    version: str
    instance_count: int


class NamedQueryInfoResult(BaseModel):
    name: str
    entry_point: str
    required_params: list[str] = Field(default_factory=list)
    returns: str
    description: str | None = None
    example_ids: list[str] = Field(default_factory=list)


class QueryListResult(BaseModel):
    queries: list[NamedQueryInfoResult] = Field(default_factory=list)


class WikiPageResult(BaseModel):
    path: str
    content: str


class WikiRenderResult(BaseModel):
    pages: list[WikiPageResult] = Field(default_factory=list)
    page_count: int = 0


class InspectNeighborResult(BaseModel):
    direction: Literal["incoming", "outgoing"]
    relationship_type: str
    edge_key: int | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    entity: dict[str, Any]


class InspectEntityResult(BaseModel):
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    neighbors: list[InspectNeighborResult] = Field(default_factory=list)
    total_neighbors: int = 0


class CanonicalViewResult(BaseModel):
    view: str
    payload: dict[str, Any]


class ReloadConfigResult(BaseModel):
    config_path: str
    updated: bool
    warnings: list[str] = Field(default_factory=list)


class FeedbackProfileResult(BaseModel):
    found: bool
    relationship_type: str
    profile: dict[str, Any] = Field(default_factory=dict)


class WorkflowLockResult(BaseModel):
    lock_path: str
    config_digest: str
    providers_locked: int
    artifacts_locked: int


class WorkflowPlanResult(BaseModel):
    plan: dict[str, Any]


class WorkflowExecutionResult(BaseModel):
    workflow: str
    output: Any
    receipt_id: str
    mode: str
    canonical: bool
    apply_digest: str | None = None
    head_snapshot_id: str | None = None
    committed_snapshot_id: str | None = None
    apply_previews: dict[str, Any] = Field(default_factory=dict)
    query_receipt_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowRunResult(WorkflowExecutionResult):
    mode: str = "run"
    canonical: bool = False


class WorkflowApplyResult(WorkflowExecutionResult):
    mode: str = "apply"
    canonical: bool = True


class WorkflowTestCaseResult(BaseModel):
    name: str
    workflow: str
    passed: bool
    output: Any | None = None
    receipt_id: str | None = None
    error: str | None = None


class WorkflowTestResult(BaseModel):
    total: int
    passed: int
    failed: int
    cases: list[WorkflowTestCaseResult] = Field(default_factory=list)


class WorkflowProposeResult(BaseModel):
    workflow: str
    output: Any
    receipt_id: str
    group_id: str | None = None
    group_status: str
    review_priority: str
    suppressed: bool = False
    suppressed_members: list[SuppressedProposalMember] = Field(default_factory=list)
    query_receipt_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    prior_resolution: dict[str, Any] | None = None
    policy_summary: dict[str, int] = Field(default_factory=dict)
    receipt: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = Field(default_factory=list)


class SnapshotMetadata(BaseModel):
    snapshot_id: str
    created_at: str
    label: str | None = None
    config_digest: str
    lock_digest: str | None = None
    graph_digest: str
    parent_snapshot_id: str | None = None
    origin_snapshot_id: str | None = None


class SnapshotCreateResult(BaseModel):
    snapshot: SnapshotMetadata


class SnapshotListResult(BaseModel):
    snapshots: list[SnapshotMetadata] = Field(default_factory=list)


class CloneSnapshotResult(BaseModel):
    instance_id: str
    snapshot: SnapshotMetadata


class PublishedWorldManifest(BaseModel):
    format_version: int
    world_id: str
    release_id: str
    snapshot_id: str
    compatibility: WorldCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    parent_release_id: str | None = None


class UpstreamMetadataResult(BaseModel):
    transport_ref: str
    requested_source_ref: str | None = None
    requested_transport_ref: str | None = None
    world_id: str
    release_id: str
    snapshot_id: str
    compatibility: WorldCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    overlay_config_path: str
    active_config_path: str
    manifest_path: str
    graph_path: str
    config_path: str
    lock_path: str
    manifest_digest: str | None = None
    graph_digest: str | None = None


class WorldPublishResult(BaseModel):
    manifest: PublishedWorldManifest


class WorldOverlayResult(BaseModel):
    instance_id: str
    manifest: PublishedWorldManifest


class WorldStatusResult(BaseModel):
    upstream: UpstreamMetadataResult | None = None


class WorldPullPreviewResult(BaseModel):
    current_release_id: str | None = None
    target_release_id: str
    compatibility: WorldCompatibility
    apply_digest: str
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    lock_changed: bool = False
    upstream_entity_delta: int = 0
    upstream_edge_delta: int = 0


class WorldPullApplyResult(BaseModel):
    release_id: str
    apply_digest: str
    pre_pull_snapshot_id: str


class ProposeGroupToolResult(BaseModel):
    group_id: str | None = None
    signature: str
    status: str
    review_priority: str
    member_count: int
    prior_resolution: dict[str, Any] | None = None
    suppressed: bool = False
    suppressed_members: list[SuppressedProposalMember] = Field(default_factory=list)
    policy_summary: dict[str, int] = Field(default_factory=dict)
    receipt_id: str | None = None


class AddDecisionPolicyResult(BaseModel):
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = Field(default_factory=list)


class FeedbackGroupSummary(BaseModel):
    relationship_type: str
    reason_code: str
    remediation_hint: str
    decision_context: dict[str, Any] = Field(default_factory=dict)
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    feedback_count: int
    feedback_ids: list[str] = Field(default_factory=list)
    sample_reasons: list[str] = Field(default_factory=list)


class UncodedFeedbackExample(BaseModel):
    feedback_id: str
    relationship_type: str
    reason: str
    decision_context: dict[str, Any] = Field(default_factory=dict)
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)


class ConstraintSuggestion(BaseModel):
    name: str
    description: str
    relationship_type: str
    rule: str
    severity: ConstraintSeverity
    support_count: int
    feedback_ids: list[str] = Field(default_factory=list)
    sample_value_pairs: list[dict[str, Any]] = Field(default_factory=list)


class DecisionPolicySuggestion(BaseModel):
    name: str
    description: str
    relationship_type: str
    applies_to: DecisionPolicyAppliesTo
    effect: DecisionPolicyEffect
    rationale: str
    match: dict[str, Any] = Field(default_factory=dict)
    query_name: str | None = None
    workflow_name: str | None = None
    support_count: int
    feedback_ids: list[str] = Field(default_factory=list)


class QualityCheckCandidate(BaseModel):
    relationship_type: str
    reason_code: str
    support_count: int
    description: str
    feedback_ids: list[str] = Field(default_factory=list)


class ProviderFixCandidate(BaseModel):
    relationship_type: str
    reason_code: str
    support_count: int
    description: str
    feedback_ids: list[str] = Field(default_factory=list)


class AnalyzeFeedbackResult(BaseModel):
    relationship_type: str
    feedback_count: int
    action_counts: dict[str, int] = Field(default_factory=dict)
    source_counts: dict[str, int] = Field(default_factory=dict)
    reason_code_counts: dict[str, int] = Field(default_factory=dict)
    coded_groups: list[FeedbackGroupSummary] = Field(default_factory=list)
    uncoded_feedback_count: int = 0
    uncoded_examples: list[UncodedFeedbackExample] = Field(default_factory=list)
    constraint_suggestions: list[ConstraintSuggestion] = Field(default_factory=list)
    decision_policy_suggestions: list[DecisionPolicySuggestion] = Field(default_factory=list)
    quality_check_candidates: list[QualityCheckCandidate] = Field(default_factory=list)
    provider_fix_candidates: list[ProviderFixCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OutcomeGroupSummary(BaseModel):
    anchor_type: OutcomeAnchorType
    outcome_code: str
    remediation_hint: str
    decision_context: dict[str, Any] = Field(default_factory=dict)
    scope_hints: dict[str, Any] = Field(default_factory=dict)
    outcome_count: int = 0
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    outcome_ids: list[str] = Field(default_factory=list)


class UncodedOutcomeExample(BaseModel):
    outcome_id: str
    anchor_type: OutcomeAnchorType
    anchor_id: str
    outcome: OutcomeValue
    detail: dict[str, Any] = Field(default_factory=dict)
    decision_context: dict[str, Any] = Field(default_factory=dict)
    scope_hints: dict[str, Any] = Field(default_factory=dict)


class TrustAdjustmentSuggestion(BaseModel):
    resolution_id: str
    relationship_type: str
    group_signature: str
    current_trust_status: GroupTrustStatus
    suggested_trust_status: GroupTrustStatus
    support_count: int
    rationale: str
    outcome_ids: list[str] = Field(default_factory=list)


class OutcomeDecisionPolicySuggestion(BaseModel):
    name: str
    description: str
    relationship_type: str
    applies_to: DecisionPolicyAppliesTo
    effect: DecisionPolicyEffect
    rationale: str
    match: dict[str, Any] = Field(default_factory=dict)
    query_name: str | None = None
    workflow_name: str | None = None
    support_count: int
    outcome_ids: list[str] = Field(default_factory=list)


class QueryPolicySuggestion(BaseModel):
    surface_name: str
    outcome_code: str
    support_count: int
    description: str
    outcome_ids: list[str] = Field(default_factory=list)


class OutcomeProviderFixCandidate(BaseModel):
    surface_type: str
    surface_name: str
    outcome_code: str
    support_count: int
    description: str
    outcome_ids: list[str] = Field(default_factory=list)


class DebugPackage(BaseModel):
    anchor_id: str
    outcome_count: int
    outcome_breakdown: dict[str, int] = Field(default_factory=dict)
    outcome_code_breakdown: dict[str, int] = Field(default_factory=dict)
    sample_outcome_ids: list[str] = Field(default_factory=list)
    lineage_summary: dict[str, Any] = Field(default_factory=dict)
    common_providers: list[str] = Field(default_factory=list)
    common_trace_patterns: list[str] = Field(default_factory=list)


class AnalyzeOutcomesResult(BaseModel):
    anchor_type: OutcomeAnchorType
    outcome_count: int
    outcome_counts: dict[str, int] = Field(default_factory=dict)
    outcome_code_counts: dict[str, int] = Field(default_factory=dict)
    coded_groups: list[OutcomeGroupSummary] = Field(default_factory=list)
    uncoded_outcome_count: int = 0
    uncoded_examples: list[UncodedOutcomeExample] = Field(default_factory=list)
    trust_adjustment_suggestions: list[TrustAdjustmentSuggestion] = Field(default_factory=list)
    workflow_review_policy_suggestions: list[OutcomeDecisionPolicySuggestion] = Field(
        default_factory=list
    )
    query_policy_suggestions: list[QueryPolicySuggestion] = Field(default_factory=list)
    provider_fix_candidates: list[OutcomeProviderFixCandidate] = Field(default_factory=list)
    debug_packages: list[DebugPackage] = Field(default_factory=list)
    workflow_debug_packages: list[DebugPackage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class LintResult(BaseModel):
    config_name: str
    config_warnings: list[str] = Field(default_factory=list)
    compatibility_warnings: list[str] = Field(default_factory=list)
    evaluation: EvaluateResult
    feedback_reports: list[AnalyzeFeedbackResult] = Field(default_factory=list)
    outcome_reports: list[AnalyzeOutcomesResult] = Field(default_factory=list)
    summary: LintSummary = Field(default_factory=LintSummary)
    has_issues: bool = False


class ResolveGroupToolResult(BaseModel):
    group_id: str
    action: str
    edges_created: int
    edges_skipped: int
    resolution_id: str | None = None
    receipt_id: str | None = None


class UpdateTrustStatusToolResult(BaseModel):
    resolution_id: str
    trust_status: str


class GetGroupToolResult(BaseModel):
    group: dict[str, Any]
    members: list[dict[str, Any]]
    resolution: dict[str, Any] | None = None


class ListGroupsToolResult(BaseModel):
    groups: list[dict[str, Any]]
    total: int


class ListResolutionsToolResult(BaseModel):
    resolutions: list[dict[str, Any]]
    total: int


class GroupStatusHistoryItem(BaseModel):
    resolution_id: str
    action: str
    trust_status: str
    confirmed: bool
    resolved_at: str
    tuple_count: int


class GroupBucketStatusToolResult(BaseModel):
    signature: str
    relationship_type: str
    thesis_text: str
    thesis_facts: dict[str, Any] = Field(default_factory=dict)
    latest_trust_status: str | None = None
    accepted_tuple_count: int
    pending_delta_count: int
    pending_group_id: str | None = None
    pending_version: int | None = None
    latest_approved_resolution_id: str | None = None
    approved_history: list[GroupStatusHistoryItem] = Field(default_factory=list)
