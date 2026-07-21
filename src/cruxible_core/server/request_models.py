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
    kits: list[str] | None = None
    bare: bool = False
    config_source_manifest: contracts.ConfigSourceManifest | None = None

    @model_validator(mode="after")
    def validate_bare(self) -> InitRequest:
        if self.bare and not any(value.strip() for value in (self.kits or [])):
            raise ValueError("bare requires kit-backed init")
        return self


class ValidateRequest(BaseModel):
    config_path: str | None = None
    config_yaml: str | None = None


class BootstrapClaimRequest(BaseModel):
    bootstrap_secret: str = Field(min_length=1)


class RuntimeCredentialCreateRequest(BaseModel):
    label: str = Field(min_length=1)
    permission_mode: contracts.RuntimeCredentialPermissionMode = "admin"


class HostedInstanceInitRequest(BaseModel):
    instance_id: str | None = None
    source_type: contracts.HostedInstanceSourceType
    kit_refs: list[str] | None = None
    transport_ref: str | None = None
    state_ref: str | None = None
    overlay_kit_ref: str | None = None
    no_overlay_kit: bool = False
    bare: bool = False

    @model_validator(mode="after")
    def validate_source(self) -> HostedInstanceInitRequest:
        normalized_kit_refs = [value.strip() for value in (self.kit_refs or []) if value.strip()]
        if self.source_type == "kit":
            if not normalized_kit_refs:
                raise ValueError("kit_refs is required when source_type=kit")
            if any(
                (value or "").strip()
                for value in (self.transport_ref, self.state_ref, self.overlay_kit_ref)
            ):
                raise ValueError(
                    "transport_ref, state_ref, and overlay_kit_ref require "
                    "source_type=reference_model"
                )
            if self.no_overlay_kit:
                raise ValueError("no_overlay_kit requires source_type=reference_model")
            return self

        has_transport = bool((self.transport_ref or "").strip())
        has_state = bool((self.state_ref or "").strip())
        if has_transport == has_state:
            raise ValueError(
                "Provide exactly one of transport_ref or state_ref when source_type=reference_model"
            )
        if (self.overlay_kit_ref or "").strip() and self.no_overlay_kit:
            raise ValueError("Provide overlay_kit_ref or no_overlay_kit, not both")
        if normalized_kit_refs:
            raise ValueError("kit_refs requires source_type=kit")
        if self.bare:
            raise ValueError("bare requires source_type=kit")
        return self


class QueryRequest(BaseModel):
    query_name: str
    params: dict[str, Any] | None = None
    limit: int | None = None
    offset: int = Field(default=0, ge=0)
    relationship_state: contracts.QueryVisibilityState | None = None
    decision_record_id: str | None = None
    profile: contracts.ReadProfile = "standard"
    layout: contracts.QueryLayout = "rows"


class InlineQueryRequest(BaseModel):
    definition: contracts.InlineQueryDefinition
    params: dict[str, Any] | None = None
    limit: int | None = None
    relationship_state: contracts.QueryVisibilityState | None = None
    decision_record_id: str | None = None
    profile: contracts.ReadProfile = "standard"
    layout: contracts.QueryLayout = "rows"


class GateCheckRequest(BaseModel):
    candidates: list[str] = Field(default_factory=list)
    error_reason: str | None = None


class AddEntitiesRequest(BaseModel):
    entities: list[contracts.EntityInput]
    dry_run: bool = False
    actor_context: contracts.GovernedActorContext | None = None


class AddRelationshipsRequest(BaseModel):
    relationships: list[contracts.RelationshipInput]
    dry_run: bool = False
    actor_context: contracts.GovernedActorContext | None = None


class BatchDirectWriteRequest(BaseModel):
    payload: contracts.BatchDirectWritePayload
    dry_run: bool = False
    actor_context: contracts.GovernedActorContext | None = None


class FeedbackRequest(BaseModel):
    receipt_id: str | None = None
    action: contracts.FeedbackAction
    source: contracts.FeedbackSource
    from_type: str
    from_id: str
    relationship_type: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    reason: str = ""
    reason_code: str | None = None
    scope_hints: dict[str, Any] | None = None
    corrections: dict[str, Any] | None = None
    group_override: bool = False
    actor_context: contracts.GovernedActorContext | None = None


class FeedbackBatchRequest(BaseModel):
    source: contracts.FeedbackSource
    items: list[contracts.FeedbackBatchItemInput]
    actor_context: contracts.GovernedActorContext | None = None


class FeedbackFromQueryRequest(contracts.FeedbackFromQueryInput):
    actor_context: contracts.GovernedActorContext | None = None


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
    actor_context: contracts.GovernedActorContext | None = None


class ProposeGroupRequest(BaseModel):
    relationship_type: str
    members: list[contracts.MemberInput]
    thesis_text: str = ""
    thesis_facts: dict[str, Any] | None = None
    analysis_state: dict[str, Any] | None = None
    signal_sources_used: list[str] | None = None
    proposed_by: contracts.GroupProposedBy = "agent"
    suggested_priority: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class ResolveGroupRequest(BaseModel):
    action: contracts.GroupAction
    rationale: str = ""
    resolved_by: contracts.GroupResolvedBy = "human"
    expected_pending_version: int
    actor_context: contracts.GovernedActorContext | None = None
    # When approving, bless each surviving pre-existing edge (a member tuple
    # already live) with the group's review status + provenance instead of
    # skipping it silently. Default keeps today's skip-but-now-explained behavior.
    stamp_existing: bool = False


class UpdateTrustStatusRequest(BaseModel):
    trust_status: contracts.GroupTrustStatus
    reason: str = ""
    actor_context: contracts.GovernedActorContext | None = None


class RegisterSourceArtifactRequest(BaseModel):
    source_path: str
    source_artifact_id: str | None = None
    source_kind: contracts.SourceKind = "markdown"
    source_retention: contracts.SourceRetention = "manifest_only"
    original_uri: str | None = None
    label: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class DereferenceSourceEvidenceRequest(BaseModel):
    source_artifact_id: str
    chunk_id: str | None = None
    heading_path: list[str] | None = None
    block_selector: str | None = None
    expected_content_hash: str | None = None

    @model_validator(mode="after")
    def _validate_locator(self) -> DereferenceSourceEvidenceRequest:
        if not self.source_artifact_id.strip():
            raise ValueError("source_artifact_id is required")
        if self.chunk_id is not None:
            if not self.chunk_id.strip():
                raise ValueError("chunk_id must be non-empty when provided")
            return self
        if not self.heading_path or self.block_selector is None:
            raise ValueError(
                "source evidence requires chunk_id or heading_path plus block_selector"
            )
        if not self.block_selector.strip():
            raise ValueError("block_selector must be non-empty when provided")
        return self


class EvaluateRequest(BaseModel):
    max_findings: int = 100
    exclude_orphan_types: list[str] | None = None
    severity_filter: list[contracts.FindingSeverity] | None = None
    category_filter: list[contracts.FindingCategory] | None = None


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
    actor_context: contracts.GovernedActorContext | None = None


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
    actor_context: contracts.GovernedActorContext | None = None


class WorkflowInputRequest(BaseModel):
    workflow_name: str
    input: dict[str, Any] | None = None
    decision_record_id: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class WorkflowApplyRequest(BaseModel):
    workflow_name: str
    input: dict[str, Any] | None = None
    expected_apply_digest: str
    expected_head_snapshot_id: str | None = None
    decision_record_id: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class DecisionRecordCreateRequest(BaseModel):
    question: str
    subject_type: str | None = None
    subject_id: str | None = None
    opened_by: Literal["human", "agent", "service"] = "human"
    actor_context: contracts.GovernedActorContext | None = None


class DecisionRecordFinalizeRequest(BaseModel):
    final_decision: str
    decision_class: contracts.DecisionClass
    rationale: str = ""
    actor_context: contracts.GovernedActorContext | None = None


class DecisionRecordAbandonRequest(BaseModel):
    reason: str = ""
    actor_context: contracts.GovernedActorContext | None = None


class WorkflowTestRequest(BaseModel):
    name: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class WorkflowLockRequest(BaseModel):
    force: bool = False


class ReloadConfigRequest(BaseModel):
    config_path: str | None = None
    config_yaml: str | None = None
    allow_orphans: bool = False
    config_source_manifest: contracts.ConfigSourceManifest | None = None


class ConfigStatusRequest(BaseModel):
    current_source_manifest: contracts.ConfigSourceManifest | None = None


class SnapshotCreateRequest(BaseModel):
    label: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class InstanceBackupRequest(BaseModel):
    artifact_path: str
    label: str | None = None
    actor_context: contracts.GovernedActorContext | None = None


class InstanceRestoreRequest(BaseModel):
    artifact_path: str
    root_dir: str | None = None


class InstanceRelocateRequest(BaseModel):
    to_dir: str
    remove_source: bool = False


class CloneSnapshotRequest(BaseModel):
    snapshot_id: str
    root_dir: str


class StatePublishRequest(BaseModel):
    transport_ref: str
    state_id: str
    release_id: str
    compatibility: contracts.StateCompatibility


class StateOverlayRequest(BaseModel):
    transport_ref: str | None = None
    state_ref: str | None = None
    kit: str | None = None
    no_kit: bool = False
    root_dir: str

    @model_validator(mode="after")
    def validate_source(self) -> StateOverlayRequest:
        if bool((self.transport_ref or "").strip()) == bool((self.state_ref or "").strip()):
            raise ValueError("Provide exactly one of transport_ref or state_ref")
        if bool((self.kit or "").strip()) and self.no_kit:
            raise ValueError("Provide kit or no_kit, not both")
        return self


class StatePullApplyRequest(BaseModel):
    expected_apply_digest: str
    actor_context: contracts.GovernedActorContext | None = None
