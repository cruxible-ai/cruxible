"""Input and result types for the service layer."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypeAlias

from cruxible_core.config.schema import (
    CoreConfig,
    FeedbackRemediationHint,
    OutcomeAnchorType,
    OutcomeLabel,
    OutcomeRemediationHint,
    QueryMode,
    SurfaceType,
    WorkflowType,
)
from cruxible_core.decision.types import DecisionEvent, DecisionRecord
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.assertion_state import RelationshipLifecycleState
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.types import (
    CandidateGroup,
    CandidateMember,
    GroupResolution,
    GroupStatus,
    QuerySourceEvidence,
    ResolutionAction,
    ReviewPriority,
    TrustStatus,
)
from cruxible_core.instance_protocol import InstanceProtocol
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.query.enums import QueryDedupe, QueryResultShape, QueryVisibilityState
from cruxible_core.query.evaluate import EvaluationReport
from cruxible_core.query.types import QueryRow
from cruxible_core.receipt.types import Receipt
from cruxible_core.snapshot.types import (
    InstanceBackupManifest,
    PublishedStateManifest,
    StateCompatibility,
    StateSnapshot,
    UpstreamMetadata,
)
from cruxible_core.source_artifacts.types import SourceEvidenceInput
from cruxible_core.workflow.types import CompiledPlan
from cruxible_core.workflow_execution_types import WorkflowResultMode


@dataclass(frozen=True)
class OperationContext:
    """Optional audit context for recording an operation against a decision.

    Supplying ``decision_record_id`` opts the operation into decision recording
    mode. Read operations may still append decision-event audit metadata; this
    does not imply graph or state mutation.
    """

    decision_record_id: str | None = None
    request_id: str | None = None
    surface: Literal["cli", "mcp", "http", "local"] | None = None
    actor_context: GovernedActorContext | None = None


@dataclass
class DecisionRecordServiceResult:
    record: DecisionRecord
    events: list[DecisionEvent] = field(default_factory=list)


@dataclass
class DecisionRecordListResult:
    items: list[DecisionRecord]
    total: int = 0


@dataclass
class DecisionEventListResult:
    items: list[DecisionEvent]
    total: int = 0


NeighborDirection = Literal["incoming", "outgoing"]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class EntityWriteInput:
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationshipWriteInput:
    from_type: str
    from_id: str
    relationship_type: str
    to_type: str
    to_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    pending: bool = False
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = field(default_factory=list)
    source_evidence: Sequence[SourceEvidenceInput | Mapping[str, Any]] = field(default_factory=list)
    evidence_rationale: str | None = None
    # Typed, review-SAFE lifecycle write. Sets ONLY ``assertion.lifecycle`` on the
    # edge; structurally cannot touch ``assertion.review`` or ``group_override``
    # because :class:`RelationshipLifecycleState` carries neither field.
    lifecycle: RelationshipLifecycleState | None = None


@dataclass
class SharedEvidenceInput:
    evidence_refs: Sequence[EvidenceRef | Mapping[str, Any]] = field(default_factory=list)
    source_evidence: Sequence[SourceEvidenceInput | Mapping[str, Any]] = field(default_factory=list)


@dataclass
class BatchRelationshipWriteInput(RelationshipWriteInput):
    shared_evidence_keys: Sequence[str] = field(default_factory=list)


@dataclass
class BatchDirectWriteInput:
    entities: Sequence[EntityWriteInput] = field(default_factory=list)
    relationships: Sequence[BatchRelationshipWriteInput] = field(default_factory=list)
    shared_evidence: Mapping[str, SharedEvidenceInput] = field(default_factory=dict)


@dataclass
class RelationshipTargetInput:
    from_type: str
    from_id: str
    relationship_type: str
    to_type: str
    to_id: str
    edge_key: int | None = None


@dataclass
class FeedbackItemInput:
    action: Literal["approve", "reject", "correct", "flag"]
    target: RelationshipTargetInput
    receipt_id: str | None = None
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] | None = None
    corrections: dict[str, Any] | None = None
    group_override: bool = False


@dataclass
class AddEntityResult:
    added: int
    updated: int
    receipt_id: str | None = None


@dataclass
class DirectWriteGroupInteraction:
    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    group_id: str
    group_status: str | None = None
    group_signature: str | None = None
    source_workflow_name: str | None = None
    edge_key: int | None = None


@dataclass
class AddRelationshipResult:
    added: int
    updated: int
    pending_conflicts: list[DirectWriteGroupInteraction] = field(default_factory=list)
    updated_group_backed_edges: list[DirectWriteGroupInteraction] = field(default_factory=list)
    receipt_id: str | None = None


@dataclass
class BatchDirectWriteResult:
    dry_run: bool
    valid: bool
    entities_added: int = 0
    entities_updated: int = 0
    relationships_added: int = 0
    relationships_updated: int = 0
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    evidence_sources_used: list[str] = field(default_factory=list)
    pending_conflicts: list[DirectWriteGroupInteraction] = field(default_factory=list)
    updated_group_backed_edges: list[DirectWriteGroupInteraction] = field(default_factory=list)
    receipt_id: str | None = None


@dataclass
class ValidateServiceResult:
    config: CoreConfig
    warnings: list[str]


@dataclass
class QueryParamHints:
    entry_point: str | None
    required_params: list[str] = field(default_factory=list)
    primary_key: str | None = None
    example_ids: list[str] = field(default_factory=list)


@dataclass
class QueryServiceResult:
    items: list[QueryRow]
    receipt_id: str | None
    receipt: Receipt | None
    total: int
    limit: int | None
    truncated: bool
    steps_executed: int
    offset: int = 0
    limit_truncated: bool = False
    path_truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    total_path_count: int | None = None
    retained_path_count: int | None = None
    result_shape: QueryResultShape = "path"
    dedupe: QueryDedupe = "path"
    relationship_state: QueryVisibilityState = "live"
    param_hints: QueryParamHints | None = None
    policy_summary: dict[str, int] = field(default_factory=dict)


QuerySurfaceServiceResult: TypeAlias = QueryServiceResult


@dataclass
class StatsServiceResult:
    entity_count: int
    edge_count: int
    entity_counts: dict[str, int] = field(default_factory=dict)
    relationship_counts: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    head_snapshot_id: str | None = None


@dataclass
class ServerInfoServiceResult:
    server_required: bool
    state_dir: str
    version: str
    instance_count: int
    auth_enabled: bool
    auth_required: bool


@dataclass
class QueryDefinitionServiceResult:
    name: str
    mode: QueryMode
    entry_point: str | None
    required_params: list[str] = field(default_factory=list)
    returns: str = ""
    result_shape: QueryResultShape = "path"
    dedupe: QueryDedupe = "path"
    relationship_state: QueryVisibilityState = "live"
    allow_relationship_state_override: bool = False
    select: dict[str, Any] | None = None
    order_by: list[dict[str, Any]] = field(default_factory=list)
    include: dict[str, dict[str, Any]] = field(default_factory=dict)
    limit: int | None = None
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    description: str | None = None
    example_ids: list[str] = field(default_factory=list)


@dataclass
class InspectNeighborResult:
    direction: NeighborDirection
    relationship_type: str
    edge_key: int | None
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    entity: EntityInstance | None = None


@dataclass
class InspectEntityResult:
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    neighbors: list[InspectNeighborResult] = field(default_factory=list)
    total_neighbors: int = 0


@dataclass
class PropertyChangeItem:
    property: str
    from_value: Any | None = None
    to_value: Any | None = None


@dataclass
class EntityChangeHistoryItem:
    entity_type: str
    entity_id: str
    change_kind: Literal["created", "updated"]
    property_changes: list[PropertyChangeItem]
    changed_at: datetime
    receipt_id: str
    operation_type: str
    actor_context: dict[str, Any] | None = None


@dataclass
class EntityChangeHistoryResult:
    entity_type: str
    entity_id: str | None = None
    items: list[EntityChangeHistoryItem] = field(default_factory=list)
    total: int = 0
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    legacy_entity_write_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class CanonicalViewResult:
    view: str
    payload: dict[str, Any]


@dataclass
class ReceiptExplanationResult:
    receipt_id: str
    format: Literal["json", "markdown", "mermaid"]
    content: str


@dataclass
class TraceListResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0


@dataclass
class ExportEdgesResult:
    fieldnames: list[str]
    rows: list[dict[str, Any]]
    count: int


@dataclass
class ReloadConfigResult:
    config_path: str
    updated: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class AddConstraintServiceResult:
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class AddDecisionPolicyServiceResult:
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class FeedbackServiceResult:
    feedback_id: str
    applied: bool
    receipt_id: str | None = None


@dataclass
class FeedbackBatchServiceResult:
    feedback_ids: list[str] = field(default_factory=list)
    applied_count: int = 0
    total: int = 0
    receipt_id: str | None = None


@dataclass
class FeedbackGroupSummary:
    relationship_type: str
    reason_code: str
    remediation_hint: FeedbackRemediationHint
    decision_context: dict[str, Any] = field(default_factory=dict)
    scope_hints: dict[str, Any] = field(default_factory=dict)
    feedback_count: int = 0
    feedback_ids: list[str] = field(default_factory=list)
    sample_reasons: list[str] = field(default_factory=list)


@dataclass
class UncodedFeedbackExample:
    feedback_id: str
    relationship_type: str
    reason: str
    target: RelationshipInstance
    decision_context: dict[str, Any] = field(default_factory=dict)
    scope_hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConstraintSuggestion:
    name: str
    description: str
    relationship_type: str
    rule: str
    severity: Literal["warning", "error"]
    support_count: int
    feedback_ids: list[str] = field(default_factory=list)
    sample_value_pairs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DecisionPolicySuggestion:
    name: str
    description: str
    relationship_type: str
    applies_to: Literal["query", "workflow"]
    effect: Literal["suppress", "require_review"]
    rationale: str
    match: dict[str, Any] = field(default_factory=dict)
    query_name: str | None = None
    workflow_name: str | None = None
    support_count: int = 0
    feedback_ids: list[str] = field(default_factory=list)


@dataclass
class OutcomeDecisionPolicySuggestion:
    name: str
    description: str
    relationship_type: str
    applies_to: Literal["query", "workflow"]
    effect: Literal["suppress", "require_review"]
    rationale: str
    match: dict[str, Any] = field(default_factory=dict)
    query_name: str | None = None
    workflow_name: str | None = None
    support_count: int = 0
    outcome_ids: list[str] = field(default_factory=list)


@dataclass
class QualityCheckCandidate:
    relationship_type: str
    reason_code: str
    support_count: int
    description: str
    feedback_ids: list[str] = field(default_factory=list)


@dataclass
class ProviderFixCandidate:
    relationship_type: str
    reason_code: str
    support_count: int
    description: str
    feedback_ids: list[str] = field(default_factory=list)


@dataclass
class AnalyzeFeedbackResult:
    relationship_type: str
    feedback_count: int
    action_counts: dict[str, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    reason_code_counts: dict[str, int] = field(default_factory=dict)
    coded_groups: list[FeedbackGroupSummary] = field(default_factory=list)
    uncoded_feedback_count: int = 0
    uncoded_examples: list[UncodedFeedbackExample] = field(default_factory=list)
    constraint_suggestions: list[ConstraintSuggestion] = field(default_factory=list)
    decision_policy_suggestions: list[DecisionPolicySuggestion] = field(default_factory=list)
    quality_check_candidates: list[QualityCheckCandidate] = field(default_factory=list)
    provider_fix_candidates: list[ProviderFixCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OutcomeGroupSummary:
    anchor_type: OutcomeAnchorType
    outcome_code: str
    remediation_hint: OutcomeRemediationHint
    decision_context: dict[str, Any] = field(default_factory=dict)
    scope_hints: dict[str, Any] = field(default_factory=dict)
    outcome_count: int = 0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    outcome_ids: list[str] = field(default_factory=list)


@dataclass
class UncodedOutcomeExample:
    outcome_id: str
    anchor_type: OutcomeAnchorType
    anchor_id: str
    outcome: OutcomeLabel
    detail: dict[str, Any] = field(default_factory=dict)
    decision_context: dict[str, Any] = field(default_factory=dict)
    scope_hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrustAdjustmentSuggestion:
    resolution_id: str
    relationship_type: str
    group_signature: str
    current_trust_status: TrustStatus
    suggested_trust_status: TrustStatus
    support_count: int
    rationale: str
    outcome_ids: list[str] = field(default_factory=list)


@dataclass
class QueryPolicySuggestion:
    surface_name: str
    outcome_code: str
    support_count: int
    description: str
    outcome_ids: list[str] = field(default_factory=list)


@dataclass
class OutcomeProviderFixCandidate:
    surface_type: SurfaceType
    surface_name: str
    outcome_code: str
    support_count: int
    description: str
    outcome_ids: list[str] = field(default_factory=list)


@dataclass
class DebugPackage:
    anchor_id: str
    outcome_count: int
    outcome_breakdown: dict[str, int] = field(default_factory=dict)
    outcome_code_breakdown: dict[str, int] = field(default_factory=dict)
    sample_outcome_ids: list[str] = field(default_factory=list)
    lineage_summary: dict[str, Any] = field(default_factory=dict)
    common_providers: list[str] = field(default_factory=list)
    common_trace_patterns: list[str] = field(default_factory=list)


@dataclass
class AnalyzeOutcomesResult:
    anchor_type: OutcomeAnchorType
    outcome_count: int
    outcome_counts: dict[str, int] = field(default_factory=dict)
    outcome_code_counts: dict[str, int] = field(default_factory=dict)
    coded_groups: list[OutcomeGroupSummary] = field(default_factory=list)
    uncoded_outcome_count: int = 0
    uncoded_examples: list[UncodedOutcomeExample] = field(default_factory=list)
    trust_adjustment_suggestions: list[TrustAdjustmentSuggestion] = field(default_factory=list)
    workflow_review_policy_suggestions: list[OutcomeDecisionPolicySuggestion] = field(
        default_factory=list
    )
    query_policy_suggestions: list[QueryPolicySuggestion] = field(default_factory=list)
    provider_fix_candidates: list[OutcomeProviderFixCandidate] = field(default_factory=list)
    debug_packages: list[DebugPackage] = field(default_factory=list)
    workflow_debug_packages: list[DebugPackage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LintSummary:
    config_warning_count: int = 0
    compatibility_warning_count: int = 0
    evaluation_finding_count: int = 0
    feedback_report_count: int = 0
    feedback_issue_count: int = 0
    outcome_report_count: int = 0
    outcome_issue_count: int = 0


@dataclass
class LintServiceResult:
    config_name: str = ""
    config_warnings: list[str] = field(default_factory=list)
    compatibility_warnings: list[str] = field(default_factory=list)
    evaluation: EvaluationReport = field(
        default_factory=lambda: EvaluationReport(
            entity_count=0, edge_count=0, findings=[], summary={}
        )
    )
    feedback_reports: list[AnalyzeFeedbackResult] = field(default_factory=list)
    outcome_reports: list[AnalyzeOutcomesResult] = field(default_factory=list)
    summary: LintSummary = field(default_factory=LintSummary)
    has_issues: bool = False


@dataclass
class OutcomeServiceResult:
    outcome_id: str


@dataclass
class InitResult:
    instance: InstanceProtocol
    warnings: list[str]


@dataclass
class ListResult:
    items: list[Any]
    total: int


@dataclass
class LockServiceResult:
    lock_path: str
    config_digest: str
    providers_locked: int
    artifacts_locked: int


@dataclass
class PlanServiceResult:
    plan: CompiledPlan


@dataclass
class WorkflowExecutionServiceResult:
    workflow: str
    output: Any
    receipt_id: str
    mode: WorkflowResultMode
    workflow_type: WorkflowType
    apply_digest: str | None = None
    head_snapshot_id: str | None = None
    committed_snapshot_id: str | None = None
    apply_previews: dict[str, Any] = field(default_factory=dict)
    query_receipt_ids: list[str] = field(default_factory=list)
    read_metadata: dict[str, Any] = field(default_factory=dict)
    trace_ids: list[str] = field(default_factory=list)
    receipt: Receipt | None = None
    traces: list[ExecutionTrace] = field(default_factory=list)

    @property
    def canonical(self) -> bool:
        """Whether this result came from a canonical workflow."""
        return self.workflow_type == "canonical"


@dataclass(frozen=True)
class ApplyPreviewReference:
    workflow: str
    input_payload: dict[str, Any]
    apply_digest: str
    head_snapshot_id: str | None
    receipt_id: str
    created_at: datetime
    apply_previews: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunServiceResult(WorkflowExecutionServiceResult):
    mode: WorkflowResultMode = "run"
    workflow_type: WorkflowType = "utility"


@dataclass
class ApplyWorkflowResult(WorkflowExecutionServiceResult):
    mode: WorkflowResultMode = "apply"
    workflow_type: WorkflowType = "canonical"


@dataclass
class WorkflowTestCaseServiceResult:
    name: str
    workflow: str
    passed: bool
    output: Any | None = None
    receipt_id: str | None = None
    error: str | None = None


@dataclass
class TestServiceResult:
    total: int
    passed: int
    failed: int
    cases: list[WorkflowTestCaseServiceResult] = field(default_factory=list)


@dataclass
class SuppressedProposalMember:
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


@dataclass
class ProposeWorkflowResult:
    workflow: str
    output: Any
    receipt_id: str
    group_id: str | None
    group_status: GroupStatus | Literal["suppressed", "no_candidates"]
    review_priority: ReviewPriority
    mode: WorkflowResultMode = "proposal"
    workflow_type: WorkflowType = "proposal"
    suppressed: bool = False
    suppressed_members: list[SuppressedProposalMember] = field(default_factory=list)
    query_receipt_ids: list[str] = field(default_factory=list)
    read_metadata: dict[str, Any] = field(default_factory=dict)
    trace_ids: list[str] = field(default_factory=list)
    prior_resolution: GroupResolution | None = None
    policy_summary: dict[str, int] = field(default_factory=dict)
    receipt: Receipt | None = None
    traces: list[ExecutionTrace] = field(default_factory=list)

    @property
    def canonical(self) -> bool:
        """Whether this result came from a canonical workflow."""
        return self.workflow_type == "canonical"


@dataclass
class SnapshotCreateResult:
    snapshot: StateSnapshot


@dataclass
class SnapshotListResult:
    items: list[StateSnapshot] = field(default_factory=list)
    total: int = 0


@dataclass
class CloneSnapshotResult:
    instance: InstanceProtocol
    snapshot: StateSnapshot


@dataclass
class InstanceBackupResult:
    instance_id: str
    artifact_path: str
    manifest: InstanceBackupManifest


@dataclass
class InstanceRestoreResult:
    instance: InstanceProtocol
    instance_id: str
    root_dir: str
    manifest: InstanceBackupManifest
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered"


@dataclass
class InstanceRelocateResult:
    instance: InstanceProtocol
    instance_id: str
    from_dir: str
    to_dir: str
    manifest: InstanceBackupManifest
    source_removed: bool = False
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered"


@dataclass
class StatePublishResult:
    manifest: PublishedStateManifest


@dataclass
class StateOverlayResult:
    instance: InstanceProtocol
    manifest: PublishedStateManifest


@dataclass
class StateStatusResult:
    upstream: UpstreamMetadata | None


@dataclass
class StatePullPreviewResult:
    current_release_id: str | None
    target_release_id: str
    compatibility: StateCompatibility
    apply_digest: str
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    lock_changed: bool = False
    upstream_entity_delta: int = 0
    upstream_edge_delta: int = 0


@dataclass
class StatePullApplyResult:
    release_id: str
    apply_digest: str
    pre_pull_snapshot_id: str


# ---------------------------------------------------------------------------
# Group result types
# ---------------------------------------------------------------------------


@dataclass
class ProposeGroupResult:
    group_id: str | None
    signature: str
    status: GroupStatus | Literal["suppressed", "no_candidates"]
    review_priority: ReviewPriority
    member_count: int
    prior_resolution: GroupResolution | None
    suppressed: bool = False
    suppressed_members: list[SuppressedProposalMember] = field(default_factory=list)
    policy_summary: dict[str, int] = field(default_factory=dict)
    receipt_id: str | None = None


@dataclass
class GroupSignalInput:
    signal_source: str
    signal: Literal["support", "contradict", "unsure"]
    evidence: str = ""
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    source_evidence: list[SourceEvidenceInput | dict[str, Any]] = field(default_factory=list)
    basis: dict[str, Any] | None = None


@dataclass
class GroupMemberInput:
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    relationship_type: str
    signals: list[GroupSignalInput] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    source_query_evidence: list[QuerySourceEvidence | dict[str, Any]] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    source_evidence: list[SourceEvidenceInput | dict[str, Any]] = field(default_factory=list)
    evidence_rationale: str | None = None


@dataclass
class ResolveGroupResult:
    group_id: str
    action: ResolutionAction
    edges_created: int
    edges_skipped: int
    resolution_id: str | None = None
    receipt_id: str | None = None
    # Per-member explanations for every skipped member (existing-edge or
    # validation-failure), each carrying the member identity, a ``skip_kind``
    # and a human-readable ``reason`` so a bare count is never the only signal.
    skipped_members: list[dict[str, str]] = field(default_factory=list)
    # Count of pre-existing edges blessed with the group's review status and
    # provenance when ``stamp_existing`` was requested (0 otherwise).
    edges_stamped: int = 0


@dataclass
class PropertyDeltaResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass
class GroupMemberReviewResult:
    proposed_tuple: dict[str, str]
    proposed_properties: dict[str, Any]
    current_edge_count: int
    current_edge_key: int | None = None
    current_properties: dict[str, Any] | None = None
    current_review_status: str | None = None
    property_delta: PropertyDeltaResult = field(default_factory=PropertyDeltaResult)


@dataclass
class GetGroupResult:
    group: CandidateGroup
    members: list[CandidateMember]
    resolution: GroupResolution | None = None
    bucket_status: "GroupStatusResult | None" = None
    member_review: list[GroupMemberReviewResult] = field(default_factory=list)


@dataclass
class RelationshipLineageResult:
    found: bool
    relationship: RelationshipInstance | None = None
    provenance: dict[str, Any] | None = None
    group: CandidateGroup | None = None
    resolution: GroupResolution | None = None
    source_workflow_receipt_id: str | None = None
    source_trace_ids: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ListGroupsResult:
    items: list[CandidateGroup]
    total: int


@dataclass
class ListResolutionsResult:
    items: list[GroupResolution]
    total: int


@dataclass
class UpdateTrustStatusResult:
    resolution_id: str
    trust_status: TrustStatus
    receipt_id: str | None = None


@dataclass
class GroupStatusHistoryItem:
    resolution_id: str
    action: ResolutionAction
    trust_status: TrustStatus
    confirmed: bool
    resolved_at: str
    tuple_count: int


@dataclass
class GroupStatusResult:
    signature: str
    relationship_type: str
    thesis_text: str
    thesis_facts: dict[str, Any]
    latest_trust_status: TrustStatus | None
    accepted_tuple_count: int
    pending_delta_count: int
    pending_group_id: str | None
    pending_version: int | None
    latest_approved_resolution_id: str | None
    approved_history: list[GroupStatusHistoryItem] = field(default_factory=list)


# ── State health (read-only deterministic maintenance signals) ────────
#
# Parallel to ``service_evaluate``: aggregates DETERMINISTIC raw maintenance
# metrics (counts, ages, timestamps) and binary deterministic facts only. No
# scoring, ranking, severity, or threshold-derived statuses — agents interpret;
# core reports defensible facts. Empty/missing signals default to 0 or None.


@dataclass
class StateHealthGroupsSection:
    """Candidate-group lifecycle counts plus the unresolved-backlog age span.

    Age is scoped to unresolved (pending_review + applying) groups: resolved
    groups only accumulate age and are not an actionable maintenance signal.
    """

    pending_review_count: int = 0
    applying_count: int = 0
    auto_resolved_count: int = 0
    resolved_count: int = 0
    total_count: int = 0
    oldest_unresolved_age_seconds: float | None = None
    newest_unresolved_age_seconds: float | None = None


@dataclass
class StateHealthSignalsSection:
    """Signal evidence counts for unresolved candidate proposals."""

    unevidenced_support_by_source: dict[str, int] = field(default_factory=dict)


@dataclass
class StateHealthProvenanceSection:
    """Edge provenance tally by source_ref class."""

    direct_write_edge_count: int = 0
    group_backed_edge_count: int = 0
    other_source_edge_count: int = 0
    total_edge_count: int = 0


@dataclass
class StateHealthFreshnessSection:
    """Source-artifact / provider-trace recency plus config compatibility facts."""

    source_artifact_count: int = 0
    oldest_source_artifact_age_seconds: float | None = None
    provider_trace_count: int = 0
    oldest_provider_trace_age_seconds: float | None = None
    config_compatible: bool = True
    config_warnings: list[str] = field(default_factory=list)


@dataclass
class StateHealthIntegritySection:
    """Graph-integrity counts reused from the deterministic evaluate findings."""

    orphan_entity_count: int = 0
    unused_entity_types: list[str] = field(default_factory=list)
    unused_relationship_types: list[str] = field(default_factory=list)
    configuration_locked: bool | None = None


@dataclass
class StateHealthResult:
    """Aggregated read-only state-health report (five deterministic sections)."""

    captured_at: str
    head_snapshot_id: str | None
    groups: StateHealthGroupsSection
    signals: StateHealthSignalsSection
    provenance: StateHealthProvenanceSection
    freshness: StateHealthFreshnessSection
    integrity: StateHealthIntegritySection
