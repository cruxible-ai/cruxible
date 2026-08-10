"""Shared Pydantic contracts for MCP tools.

Single source of truth for tool return shapes and constrained input types.
Both handlers.py and tools.py import from here.
FastMCP auto-generates outputSchema from the BaseModel return annotations.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QueryVisibilityState = Literal["live", "accepted", "all", "not-live", "pending", "reviewable"]
LifecycleStatus = Literal["active", "inactive", "superseded", "retracted", "live", "retired"]
# Output profile for entity-shaped read payloads: `standard` is the unchanged
# full shape (HTTP default), `compact` is the bounded identity card that keeps
# governance markers (lifecycle / review status) but drops actor_context and
# provenance blobs, `full` is reserved as a superset of standard (today equal).
ReadProfile = Literal["compact", "standard", "full"]
# Transport representation for query output: `rows` is the unchanged per-row
# layout (default), `graph` is the normalized nodes/edges representation where
# each unique entity and relationship is serialized once and rows become
# ordered references. Orthogonal to `result_shape` (the semantic unit) and
# `profile` (the detail level).
QueryLayout = Literal["rows", "graph"]
QueryMode = Literal["collection", "traversal"]
QueryResultShape = Literal["entity", "path", "relationship"]
QueryDedupe = Literal["entity", "path", "none"]
FindingSeverity = Literal["error", "warning", "info"]
FindingCategory = Literal[
    "orphan_entity",
    "coverage_gap",
    "constraint_violation",
    "governed_support_relationship",
    "unreviewed_co_member",
    "quality_check_failed",
]
ReceiptExplanationFormat = Literal["json", "markdown", "mermaid"]
GateEvaluationVerdict = Literal["satisfied", "unsatisfied", "error"]

# ── Constrained input types ───────────────────────────────────────────

ConstraintSeverity = Literal["warning", "error"]
FeedbackAction = Literal["accept", "reject", "correct"]
FeedbackInputAction = Literal["accept", "reject", "correct", "approve", "flag"]
"""Compatibility input vocabulary.

``approve`` delegates to ``accept`` with a structured deprecation warning.
``flag`` is accepted only so the server can return its structured deprecation
refusal; it is not a live feedback action and is never persisted.
"""
OutcomeValue = Literal["correct", "incorrect", "partial", "unknown"]
OutcomeAnchorType = Literal["resolution", "receipt"]
ResourceType = Literal["entities", "edges", "receipts", "feedback", "outcomes"]
GroupAction = Literal["approve", "reject"]
# Deprecated: ``auto_resolved`` is read-only legacy — never written since 0.3,
# but shipped 0.2.x instances persisted rows carrying it, so a client that drops
# it from the vocabulary cannot parse those rows at all.
GroupStatus = Literal["pending_review", "applying", "resolved", "withdrawn", "auto_resolved"]
GroupTrustStatus = Literal["trusted", "watch", "invalidated"]
DecisionPolicyAppliesTo = Literal["query", "workflow"]
DecisionPolicyEffect = Literal["suppress", "require_review"]
DecisionClass = Literal["recommended", "rejected", "deferred", "escalated"]
StateCompatibility = Literal["data_only", "additive_schema", "breaking"]
WorkflowType = Literal["utility", "canonical", "decision_support", "proposal"]
WorkflowMode = Literal["run", "preview", "apply", "proposal"]
RuntimeCredentialPermissionMode = Literal[
    "read_only",
    "governed_write",
    "graph_write",
    "admin",
]
HostedInstanceSourceType = Literal["kit", "reference_model"]
HostedInstanceInitStatus = Literal["initialized", "already_initialized"]
GovernedActorType = Literal["human_user", "service_account", "system"]

# Per-kind lifecycle status vocabularies. Deliberately distinct: entities and
# relationships do NOT share a status enum (only the surrounding structure).
#
# These enumerate the FULL vocabulary a status can hold once stored -- NOT the
# subset a free-form add/update may write. The terminal statuses (entity
# ``superseded``/``retired``, relationship ``superseded``/``retracted``) are
# owned by the dedicated receipted lifecycle verbs and are refused on this
# channel; see ``*LifecycleInput.status`` below. The Literals stay WIDE on
# purpose: narrowing them would replace the teaching refusal
# (``TerminalLifecycleWriteRefusedError``, which names the verb to use) with a
# bare enum-validation error, and they are also the read-side shape for a stored
# lifecycle that legitimately holds a terminal status.
EntityLifecycleStatus = Literal["live", "superseded", "retired"]
RelationshipLifecycleStatus = Literal["active", "inactive", "superseded", "retracted"]


# ── Structured input types ───────────────────────────────────────────


class EntityLifecycleInput(BaseModel):
    """Typed, review-SAFE lifecycle write for an entity.

    Carries ONLY the entity lifecycle axis. Entities have no review axis, so there
    is nothing else this could touch. The server validates ``status`` against the
    entity lifecycle vocabulary and stores it as the typed entity lifecycle state.
    """

    model_config = ConfigDict(extra="forbid")

    status: EntityLifecycleStatus = Field(
        description=(
            "Entity lifecycle status writable here: live. The terminal statuses "
            "'superseded' and 'retired' are REFUSED on this channel -- they are "
            "governed judgements owned by the receipted lifecycle verbs "
            "('cruxible entity supersede' / 'cruxible entity retire'), which "
            "carry a required reason, actor attribution, and a mutation receipt."
        )
    )
    reason: str | None = Field(
        default=None, description="Optional human-readable reason for the lifecycle change."
    )


class RelationshipLifecycleInput(BaseModel):
    """Typed, review-SAFE lifecycle write for a relationship edge.

    Carries ONLY the lifecycle axis (``status`` + ``reason``). It deliberately has
    NO ``review`` or ``group_override`` field: a lifecycle write through this
    channel is structurally incapable of approving/rejecting an edge or flipping
    the group override -- those stay exclusive to the governed feedback / group
    paths. The server sets only ``assertion.lifecycle`` from this input.
    """

    model_config = ConfigDict(extra="forbid")

    status: RelationshipLifecycleStatus = Field(
        description=(
            "Relationship lifecycle status writable here: active, inactive. The "
            "terminal statuses 'superseded' and 'retracted' are REFUSED on this "
            "channel -- they are governed judgements owned by the receipted "
            "lifecycle verbs ('cruxible relationship supersede' / 'cruxible "
            "relationship retract'), which carry a required reason, actor "
            "attribution, and a mutation receipt."
        )
    )
    reason: str | None = Field(
        default=None, description="Optional human-readable reason for the lifecycle change."
    )


class RelationshipInput(BaseModel):
    from_type: str = Field(description="Entity type of the source endpoint.")
    from_id: str = Field(description="Entity id of the source endpoint; must already exist.")
    relationship_type: str = Field(description="Edge type as declared in the config schema.")
    to_type: str = Field(description="Entity type of the target endpoint.")
    to_id: str = Field(description="Entity id of the target endpoint; must already exist.")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Edge properties; keys must be declared by the relationship schema.",
    )
    pending: bool = Field(
        default=False,
        description="If true, stage the edge as pending review instead of live state.",
    )
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description="External provenance references attached to this edge.",
    )
    source_evidence: list[SourceEvidenceInput] = Field(
        default_factory=list,
        description="Locators into registered source artifacts backing this edge.",
    )
    citation_handles: list[str] = Field(
        default_factory=list,
        description=("Server-minted, revision-pinned source-evidence handles backing this edge."),
    )
    evidence_rationale: str | None = Field(
        default=None,
        description="Free-text explanation of why the attached evidence supports the edge.",
    )
    lifecycle: RelationshipLifecycleInput | None = Field(
        default=None,
        description=(
            "Typed, review-safe lifecycle write. Sets only the edge's lifecycle "
            "status/reason; cannot touch its review or group-override state."
        ),
    )


class SharedEvidenceInput(BaseModel):
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=list,
        description="External provenance references shared by multiple relationships.",
    )
    source_evidence: list[SourceEvidenceInput] = Field(
        default_factory=list,
        description="Source-artifact locators shared by multiple relationships.",
    )
    citation_handles: list[str] = Field(
        default_factory=list,
        description="Server-minted source-evidence handles shared by multiple relationships.",
    )


class BatchRelationshipInput(RelationshipInput):
    shared_evidence_keys: list[str] = Field(
        default_factory=list,
        description="Keys into the payload's top-level shared_evidence map to attach here.",
    )


class EntityInput(BaseModel):
    entity_type: str = Field(description="Entity type as declared in the config schema.")
    entity_id: str = Field(description="Unique id for the entity; re-using an id upserts it.")
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Entity properties; keys must be declared by the entity schema.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Free-form non-schema metadata stored alongside the entity. It is carried "
            "verbatim as free-form data (nested under the entity's typed metadata "
            "envelope); it cannot set the entity's lifecycle. Set lifecycle via the "
            "typed `lifecycle` field, which is the only channel for it."
        ),
    )
    lifecycle: EntityLifecycleInput | None = Field(
        default=None,
        description=(
            "Typed entity lifecycle write. Sets the entity's lifecycle "
            "status/reason (the canonical soft-delete / supersession axis), "
            "validated and stored as typed lifecycle state. This is the ONLY "
            "channel for entity lifecycle; free-form `metadata` cannot touch it."
        ),
    )


class BatchDirectWritePayload(BaseModel):
    entities: list[EntityInput] = Field(
        default_factory=list,
        description="Entities to add or upsert in this batch.",
    )
    relationships: list[BatchRelationshipInput] = Field(
        default_factory=list,
        description="Relationships to add or upsert; endpoint entities must exist.",
    )
    shared_evidence: dict[str, SharedEvidenceInput] = Field(
        default_factory=dict,
        description="Named evidence bundles referenced by relationships via shared_evidence_keys.",
    )


class GovernedActorContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_type: GovernedActorType = Field(
        description="Whether the acting principal is a human_user, service_account, or system."
    )
    actor_id: str = Field(min_length=1, description="Stable id of the acting principal.")
    org_id: str = Field(min_length=1, description="Org/tenant the operation runs under.")
    operation_id: str = Field(
        min_length=1, description="Unique id for this operation, stamped into provenance."
    )
    timestamp: str = Field(description="ISO-8601 timestamp of when the operation was issued.")
    request_id: str | None = Field(
        default=None, description="Optional client request id for correlation."
    )

    @model_validator(mode="after")
    def _validate_nonblank_fields(self) -> GovernedActorContext:
        for field_name in ("actor_id", "org_id", "operation_id", "timestamp"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.request_id is not None and not self.request_id.strip():
            raise ValueError("request_id must not be blank when provided")
        return self


class ConfigSourceDigest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    digest: str


class ConfigSourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_path: str | None = None
    layers: list[ConfigSourceDigest] = Field(default_factory=list)
    composed_digest: str


class ConfigProvenance(ConfigSourceManifest):
    active_config_digest: str
    materialized_digest: str
    recorded_at: str


class SignalBucketBasis(BaseModel):
    mode: Literal["score", "enum"] = Field(
        description="Whether the signal was bucketed by numeric score or enum match."
    )
    path: str = Field(description="Dotted path to the field the basis was read from.")
    value: str | int | float = Field(description="Raw value observed at the path.")
    matched: str = Field(description="Bucket/category the value was matched to.")


SourceKind = Literal["markdown"]
SourceRetention = Literal["manifest_only", "archive"]
# ``revision_bytes_not_retained``: a pinned read of a superseded revision whose
# bytes were never archived. NOT drift — the local path holds a newer revision.
DereferenceStatus = Literal["available", "drifted", "unavailable", "revision_bytes_not_retained"]
DereferenceBodyOrigin = Literal["archive", "local_path"]


class EvidenceRef(BaseModel):
    source: str = Field(description="Origin system or dataset of the referenced record.")
    source_record_id: str = Field(description="Identifier of the record within that source.")
    artifact_id: str | None = Field(
        default=None, description="Optional registered source-artifact id."
    )
    artifact_revision_id: str | None = Field(
        default=None,
        description=(
            "Physical revision ('{source_artifact_id}@{revision}') this citation was "
            "made against. Absent refs dereference against the current revision and "
            "report revision_unpinned."
        ),
    )
    table: str | None = Field(
        default=None, description="Optional table name when the source is tabular."
    )
    row_index: int | None = Field(
        default=None, description="Optional zero-based row index within the table."
    )
    label: str | None = Field(
        default=None, description="Optional human-readable label for this reference."
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Extra key/values; unknown top-level keys are folded in here.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _collect_extra_metadata(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        known = set(cls.model_fields)
        payload = dict(value)
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ValueError("EvidenceRef metadata must be an object")
        extra = {str(key): payload.pop(key) for key in list(payload) if key not in known}
        payload["metadata"] = {**dict(metadata), **extra}
        return payload

    @field_validator("source", "source_record_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("EvidenceRef source and source_record_id must be non-empty")
        return value


AttestationStance = Literal["support", "contradict", "unsure"]
AttestationVerdict = Literal["upheld", "corrected", "invalidated"]


class AttestationRecordResult(BaseModel):
    """Recorded immutable observation plus D2 routing outcome."""

    attestation: dict[str, Any]
    created_claim: bool = False
    idempotent_replay: bool = False
    warnings: list[str] = Field(default_factory=list)
    receipt_id: str | None = None


class AttestationDispositionResult(BaseModel):
    """Appended reviewer disposition and receipt."""

    disposition: dict[str, Any]
    receipt_id: str | None = None


ResolutionVerdict = Literal["satisfied", "contradicted", "indeterminate"]
ResolutionDispositionVerdict = Literal["upheld", "overturned"]
ContractStatus = Literal["prepared", "open", "resolved"]
ContractQueue = Literal["due", "overdue", "contradicted"]


class OutcomeContractResult(BaseModel):
    """Opened resolution contract plus its idempotent-replay marker."""

    contract: dict[str, Any]
    idempotent_replay: bool = False
    receipt_id: str | None = None


class OutcomeResolutionResult(BaseModel):
    """Recorded resolution and receipt."""

    resolution: dict[str, Any]
    receipt_id: str | None = None


class OutcomeDispositionResult(BaseModel):
    """Appended reviewer disposition on one resolution, and receipt."""

    disposition: dict[str, Any]
    receipt_id: str | None = None


class SourceEvidenceInput(BaseModel):
    source_artifact_id: str = Field(
        description="Id of the registered source artifact this evidence points into."
    )
    artifact_revision_id: str | None = Field(
        default=None,
        description=(
            "Optional pin to one immutable revision ('{source_artifact_id}@{revision}'). "
            "Absent resolves against the current revision."
        ),
    )
    chunk_id: str | None = Field(
        default=None,
        description="Chunk id within the artifact; provide this or heading_path+block_selector.",
    )
    heading_path: list[str] | None = Field(
        default=None,
        description="Heading breadcrumb locating the block when no chunk_id is given.",
    )
    block_selector: str | None = Field(
        default=None,
        description="Block selector (e.g. 'paragraph:1') used with heading_path.",
    )
    label: str | None = Field(
        default=None, description="Optional human-readable label for this locator."
    )
    expected_content_hash: str | None = Field(
        default=None,
        description="Expected content hash to detect drift when dereferenced later.",
    )

    @model_validator(mode="after")
    def _validate_locator(self) -> SourceEvidenceInput:
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


class SourceArtifactChunk(BaseModel):
    chunk_id: str
    heading_path: list[str] = Field(default_factory=list)
    block_selector: str
    block_type: str
    content_hash: str
    line_start: int
    line_end: int
    preview: str | None = None
    label: str | None = None
    citation_handle: str | None = Field(
        default=None,
        description="Revision-pinned handle that cites exactly this registered chunk.",
    )


class RegisterSourceArtifactResult(BaseModel):
    source_artifact_id: str
    artifact_revision_id: str = Field(
        description="Physical id of the immutable artifact revision this registration resolved to."
    )
    revision_handle: str | None = Field(
        default=None,
        description="Stable handle for the whole immutable revision (all of its chunks).",
    )
    revision: int = Field(
        default=1,
        description="1-based registration count for this logical source_artifact_id.",
    )
    source_kind: SourceKind
    source_retention: SourceRetention
    original_uri: str | None = None
    label: str | None = None
    content_hash: str
    byte_count: int
    parser_version: str
    archived: bool = False
    archive_content_hash: str | None = None
    chunks: list[SourceArtifactChunk] = Field(default_factory=list)
    supersedes: str | None = Field(
        default=None,
        description="Revision id this registration superseded, if it replaced an earlier one.",
    )
    already_registered: bool = Field(
        default=False,
        description="True when identical content was already the current revision (no write).",
    )
    receipt_id: str | None = None


class DereferenceSourceEvidenceResult(BaseModel):
    status: DereferenceStatus
    source_artifact_id: str
    chunk_id: str
    content_hash: str
    expected_artifact_hash: str
    current_artifact_hash: str | None = None
    body_origin: DereferenceBodyOrigin | None = None
    body: str | None = None
    reason: str | None = None
    chunk: SourceArtifactChunk | None = None
    artifact_revision_id: str | None = None
    revision_unpinned: bool = False


class SourceArtifactListItem(BaseModel):
    source_artifact_id: str
    artifact_revision_id: str | None = None
    revision: int = 1
    revision_handle: str | None = None
    kind: SourceKind
    retention: SourceRetention
    original_uri: str | None = None
    label: str | None = None
    content_hash: str
    registered_at: str
    chunk_count: int
    byte_count: int


class SourceArtifactReadChunk(BaseModel):
    chunk_id: str
    heading_path: list[str] = Field(default_factory=list)
    block_selector: str
    block_type: str
    line_start: int
    line_end: int
    content_hash: str
    citation_handle: str | None = None
    text: str | None = None


class SourceArtifactReadResult(SourceArtifactListItem):
    artifact_revision_id: str
    revision: int = 1
    parser_version: str
    archived: bool = False
    archive_content_hash: str | None = None
    content_available: bool
    content_unavailable_reason: str | None = None
    body_origin: DereferenceBodyOrigin | None = None
    current_artifact_hash: str | None = None
    drift_observed_hash: str | None = Field(
        default=None,
        description="Last observed local content hash that did not match the manifest.",
    )
    drift_observed_at: str | None = Field(
        default=None, description="When the recorded content drift was last observed."
    )
    first_drift_observed_hash: str | None = Field(
        default=None,
        description=(
            "Local content hash of the FIRST drift ever observed for this revision. "
            "Sticky: unlike the pair above it is never cleared, so restoring the "
            "original bytes does not erase that the source was altered."
        ),
    )
    first_drift_observed_at: str | None = Field(
        default=None, description="When the first content drift was observed."
    )
    chunks: list[SourceArtifactReadChunk] = Field(default_factory=list)


class SignalInput(BaseModel):
    signal_source: str = Field(
        description="Name of the declared signal source producing this signal."
    )
    signal: Literal["support", "contradict", "unsure"] = Field(
        description="Tri-state stance of the source toward the proposed edge."
    )
    evidence: str = Field(default="", description="Free-text evidence or rationale for the signal.")
    evidence_refs: list[EvidenceRef | dict[str, Any]] = Field(
        default_factory=list,
        description="External provenance references backing the signal.",
    )
    source_evidence: list[SourceEvidenceInput] = Field(
        default_factory=list,
        description="Registered source-artifact locators backing the signal.",
    )
    citation_handles: list[str] = Field(
        default_factory=list,
        description="Server-minted, revision-pinned source-evidence handles backing the signal.",
    )
    basis: SignalBucketBasis | None = Field(
        default=None,
        description="Optional structured basis explaining how the signal was bucketed.",
    )


class EdgeTargetInput(BaseModel):
    from_type: str = Field(description="Entity type of the edge's source endpoint.")
    from_id: str = Field(description="Entity id of the edge's source endpoint.")
    relationship_type: str = Field(description="Edge type identifying the relationship.")
    to_type: str = Field(description="Entity type of the edge's target endpoint.")
    to_id: str = Field(description="Entity id of the edge's target endpoint.")
    edge_key: int | None = Field(
        default=None,
        description="Disambiguator when multiple edges share the same endpoints.",
    )
    claim_id: str | None = Field(
        default=None,
        description=(
            "Stable claim identity. Preferred disambiguator when multiple edges "
            "share the same endpoints; takes precedence over edge_key. Supplying "
            "both with disagreeing values is refused, never silently resolved."
        ),
    )


class MemberInput(BaseModel):
    from_type: str = Field(description="Entity type of the member edge's source endpoint.")
    from_id: str = Field(description="Entity id of the member edge's source endpoint.")
    to_type: str = Field(description="Entity type of the member edge's target endpoint.")
    to_id: str = Field(description="Entity id of the member edge's target endpoint.")
    relationship_type: str = Field(description="Edge type proposed for this member.")
    signals: list[SignalInput] = Field(
        default_factory=list,
        description="Tri-state signals from declared sources supporting or contradicting the edge.",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        description="Edge properties to set when the group is approved.",
    )
    evidence_refs: list[EvidenceRef | dict[str, Any]] = Field(
        default_factory=list,
        description="External provenance references for this member edge.",
    )
    source_evidence: list[SourceEvidenceInput] = Field(
        default_factory=list,
        description="Registered source-artifact locators for this member edge.",
    )
    citation_handles: list[str] = Field(
        default_factory=list,
        description="Server-minted, revision-pinned source-evidence handles for this member.",
    )
    evidence_rationale: str | None = Field(
        default=None,
        description="Free-text explanation of why the evidence supports this member.",
    )


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
    from_property: str = Field(description="Source-endpoint property to compare.")
    to_property: str = Field(description="Target-endpoint property to compare against.")


class FeedbackBatchItemInput(BaseModel):
    receipt_id: str = Field(description="Receipt id the feedback is anchored to.")
    action: FeedbackInputAction = Field(
        description=(
            "Adjudication: accept, reject, or correct the edge. Deprecated `approve` "
            "delegates to `accept`; deprecated `flag` is accepted only to return its "
            "removal warning. To record a "
            "doubt WITHOUT adjudicating, use `cruxible attest record --stance "
            "contradict` -- it stores the observation, its evidence, and its "
            "actor instead of silently un-approving the edge."
        )
    )
    target: EdgeTargetInput = Field(description="Coordinates of the edge being adjudicated.")
    reason: str = Field(default="", description="Free-text reason for the feedback.")
    reason_code: str | None = Field(
        default=None, description="Optional coded reason for analytics/remediation."
    )
    scope_hints: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional hints scoping how broadly the feedback should generalize.",
    )
    corrections: dict[str, Any] | None = Field(
        default=None,
        description="Corrected property values, used with action='correct'.",
    )
    group_override: bool = Field(
        default=False,
        description=(
            "Deprecated compatibility write; use force_review. If true, mark "
            "the edge assertion as a group-resolve override."
        ),
    )


class FeedbackFromQueryInput(BaseModel):
    receipt_id: str = Field(description="Query receipt id whose row is being adjudicated.")
    result_index: int = Field(description="Zero-based index of the result row in the receipt.")
    action: FeedbackInputAction = Field(
        description=(
            "Adjudication: accept, reject, or correct the edge. Deprecated `approve` "
            "delegates to `accept`; deprecated `flag` is accepted only to return its "
            "removal warning. To record a "
            "doubt WITHOUT adjudicating, use `cruxible attest record --stance "
            "contradict` -- it stores the observation, its evidence, and its "
            "actor instead of silently un-approving the edge."
        )
    )
    reason: str = Field(default="", description="Free-text reason for the feedback.")
    reason_code: str | None = Field(
        default=None, description="Optional coded reason for analytics/remediation."
    )
    scope_hints: dict[str, Any] | None = Field(
        default=None,
        description="Optional hints scoping how broadly the feedback should generalize.",
    )
    corrections: dict[str, Any] | None = Field(
        default=None,
        description="Corrected property values, used with action='correct'.",
    )
    group_override: bool = Field(
        default=False,
        description=(
            "Deprecated compatibility write; use force_review. If true, mark "
            "the edge assertion as a group-resolve override."
        ),
    )
    path_index: int | None = Field(
        default=None,
        description="For path rows, which path to select within the result.",
    )
    path_alias: str | None = Field(
        default=None,
        description="For path rows, the segment alias identifying the edge to adjudicate.",
    )


class DecisionPolicyMatchInput(BaseModel):
    from_match: dict[str, Any] = Field(
        default_factory=dict,
        alias="from",
        description="Property matchers applied to the source endpoint.",
    )
    to: dict[str, Any] = Field(
        default_factory=dict,
        description="Property matchers applied to the target endpoint.",
    )
    edge: dict[str, Any] = Field(
        default_factory=dict,
        description="Property matchers applied to the edge itself.",
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Property matchers applied to the surrounding decision context.",
    )

    model_config = {"populate_by_name": True}


# ── Tool return contracts ─────────────────────────────────────────────


class InitResult(BaseModel):
    instance_id: str
    status: str
    warnings: list[str] = Field(default_factory=list)
    base_kit_id: str | None = None


class RuntimeCredentialBootstrapResult(BaseModel):
    credential_id: str
    instance_id: str
    permission_mode: Literal["admin"]
    token: str


class HostedInstanceInitResult(BaseModel):
    instance_id: str
    status: HostedInstanceInitStatus
    source_type: HostedInstanceSourceType
    source_ref: str
    resolved_source_ref: str | None = None
    overlay_kit_ref: str | None = None
    base_kit_id: str | None = None
    manifest: "PublishedStateManifest | None" = None
    warnings: list[str] = Field(default_factory=list)


class RuntimeCredentialMetadata(BaseModel):
    credential_id: str
    instance_id: str
    label: str
    permission_mode: RuntimeCredentialPermissionMode
    created_at: str
    created_by: str | None = None
    revoked_at: str | None = None


class RuntimeCredentialResult(BaseModel):
    credential: RuntimeCredentialMetadata
    token: str | None = None


class RuntimeCredentialListResult(BaseModel):
    credentials: list[RuntimeCredentialMetadata] = Field(default_factory=list)


class ValidateResult(BaseModel):
    valid: bool
    name: str
    entity_types: list[str]
    relationships: list[str]
    named_queries: list[str]
    warnings: list[str]


class ListEnvelopeFields(BaseModel):
    """Standard list envelope carried by every top-level list result.

    ``read_revision`` is the instance's monotonic state revision at read time —
    the freshness marker for pagination and caching. Receipts prove a
    computation happened; they never prove its inputs are still current, so
    freshness checks must compare ``read_revision``, not receipt IDs.
    """

    total: int
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    read_revision: int | None = None


class SourceArtifactListResult(ListEnvelopeFields):
    items: list[SourceArtifactListItem] = Field(default_factory=list)


class SlotBindingItem(BaseModel):
    """One compute-slot binding: which provider this install resolved a slot to.

    ``contract_in``, ``contract_out``, ``allowed_billing_modes`` and
    ``requires_third_party_consent`` are the SLOT INTERFACE the binding was
    pinned to at bind time — what the pinned procedures expect, not the
    provider's restatement of it. They never change: a rebind is checked against
    them and moves the provider only. ``revision`` counts every governed change
    to this binding, so a caller that records a binding id AND revision can
    state exactly which binding it used, even after a later rebind.
    """

    binding_id: str
    install_id: str
    slot_name: str
    provider_name: str
    contract_in: str
    contract_out: str
    allowed_billing_modes: list[str] | None = None
    requires_third_party_consent: bool = False
    billing_mode: str
    third_party_consent: bool = False
    consent_actor_id: str | None = None
    consent_org_id: str | None = None
    consent_at: str | None = None
    revision: int = 1
    status: str
    bound_at: str
    updated_at: str
    retired_at: str | None = None
    actor_context: dict[str, Any] | None = None
    receipt_id: str | None = None


class SlotBindingListResult(ListEnvelopeFields):
    items: list[SlotBindingItem] = Field(default_factory=list)


class SlotBindingRevisionItem(BaseModel):
    """One revision in a binding's history, as it stood at that revision."""

    binding_id: str
    revision: int
    change_kind: str
    install_id: str
    slot_name: str
    provider_name: str
    contract_in: str
    contract_out: str
    billing_mode: str
    third_party_consent: bool = False
    consent_actor_id: str | None = None
    consent_org_id: str | None = None
    consent_at: str | None = None
    status: str
    note: str | None = None
    recorded_at: str
    actor_context: dict[str, Any] | None = None
    receipt_id: str | None = None


class SlotBindingHistoryResult(ListEnvelopeFields):
    binding_id: str
    items: list[SlotBindingRevisionItem] = Field(default_factory=list)


class QueryEntityItem(BaseModel):
    """Entity-shaped row returned by entity result queries."""

    entity_type: str
    entity_id: str
    properties: dict[str, Any]
    metadata: dict[str, Any]


class QueryPathSegmentItem(BaseModel):
    """One relationship segment in a path-shaped query row."""

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    alias: str | None = None


class QueryIncludeItem(BaseModel):
    """One included one-hop side-context relationship."""

    edge: QueryPathSegmentItem
    source: QueryEntityItem
    target: QueryEntityItem


class QueryIncludeResult(BaseModel):
    """Side-context attached to a primary query row."""

    alias: str
    many: bool = False
    exists: bool = False
    count: int = 0
    limit: int | None = None
    truncated: bool = False
    items: list[QueryIncludeItem] = Field(default_factory=list)


class QueryPathItem(BaseModel):
    """Path-shaped row returned by traversal queries."""

    entry: QueryEntityItem
    result: QueryEntityItem
    entities: list[QueryEntityItem]
    path: list[QueryPathSegmentItem]
    includes: dict[str, QueryIncludeResult] = Field(default_factory=dict)


class QueryRelationshipItem(BaseModel):
    """Relationship-shaped row returned by relationship result queries."""

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entry: QueryEntityItem
    from_entity: QueryEntityItem | None = None
    to_entity: QueryEntityItem | None = None
    includes: dict[str, QueryIncludeResult] = Field(default_factory=dict)


QueryBaseItem: TypeAlias = QueryEntityItem | QueryPathItem | QueryRelationshipItem


class QueryProjectedItem(BaseModel):
    """Projected query row with selected values and optional source evidence."""

    values: dict[str, Any]
    source: QueryBaseItem | None = None


QueryItem: TypeAlias = QueryBaseItem | QueryProjectedItem


class QueryToolResult(BaseModel):
    items: list[QueryItem]
    receipt_id: str | None
    receipt: dict[str, Any] | None
    total: int
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    limit_truncated: bool = False
    path_truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    total_path_count: int | None = None
    retained_path_count: int | None = None
    steps_executed: int
    result_shape: Literal["entity", "path", "relationship"] = "path"
    dedupe: Literal["entity", "path", "none"] = "path"
    relationship_state: QueryVisibilityState = "live"
    lifecycle_status: LifecycleStatus | None = None
    param_hints: "QueryParamHints | None" = None
    policy_summary: dict[str, int] = Field(default_factory=dict)
    # Monotonic state revision at read time; receipts prove computation,
    # never freshness — compare read_revision to detect staleness.
    read_revision: int | None = None


class QueryGraphEdgeItem(BaseModel):
    """One PHYSICAL relationship card in the shared graph-layout `edges` array.

    Exactly the serialized edge payload minus the per-occurrence traversal
    `alias`: aliases are reference-level metadata (path step refs, include
    item refs), so one physical edge is always one card even when visited
    under several step aliases.
    """

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class QueryGraphPathStepRef(BaseModel):
    """One traversal step of a path: an edge reference plus its step alias.

    Reconstructing the rows-layout segment for a step is exactly
    `{**edges[edge], "alias": alias}`.
    """

    model_config = ConfigDict(extra="forbid")

    edge: int
    alias: str | None = None


class QueryGraphIncludeItemRef(BaseModel):
    """One include item as references into the shared nodes/edges arrays.

    `alias` is the per-occurrence traversal alias of the referenced edge —
    aliases live on references, never on the shared `edges` cards.
    """

    model_config = ConfigDict(extra="forbid")

    edge: int
    alias: str | None = None
    source: int
    target: int


class QueryGraphIncludeResult(BaseModel):
    """Include side-context for one result under graph layout.

    Envelope fields (`many`/`exists`/`count`/`limit`/`truncated`) are verbatim
    row-layout passthrough; `items` becomes node/edge references.
    """

    alias: str
    many: bool = False
    exists: bool = False
    count: int = 0
    limit: int | None = None
    truncated: bool = False
    items: list[QueryGraphIncludeItemRef] = Field(default_factory=list)


class QueryGraphCompactIncludeResult(BaseModel):
    """Sparse include envelope stored in compact graph `include_sets`.

    The enclosing map key supplies the alias. `exists` is absent when it is
    derivable from `count`/`items`; false truncation and null limits are absent.
    """

    model_config = ConfigDict(extra="forbid")

    many: bool
    count: int
    exists: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    limit: int | None = Field(default=None, exclude_if=lambda value: value is None)
    truncated: bool = Field(default=False, exclude_if=lambda value: value is False)
    items: list[QueryGraphIncludeItemRef] = Field(default_factory=list)


QueryGraphIncludesRef: TypeAlias = dict[str, QueryGraphIncludeResult] | int


class QueryGraphEntityRef(BaseModel):
    """Entity-shaped result row as a node reference."""

    model_config = ConfigDict(extra="forbid")

    result: int


class QueryGraphPathRef(BaseModel):
    """Path-shaped result row as node references plus path-index references.

    The per-row `entities` array of the rows layout is not materialized: the
    visited-entity sequence is recoverable by walking `paths[path]` from
    `entry` (each segment connects the current node to its other endpoint).
    """

    model_config = ConfigDict(extra="forbid")

    entry: int
    result: int
    paths: list[int] = Field(default_factory=list)
    includes: QueryGraphIncludesRef = Field(default_factory=dict)


class QueryGraphRelationshipRef(BaseModel):
    """Relationship-shaped result row as an edge reference with endpoints."""

    model_config = ConfigDict(extra="forbid")

    entry: int
    edge: int
    from_entity: int | None = None
    to_entity: int | None = None
    includes: QueryGraphIncludesRef = Field(default_factory=dict)


QueryGraphBaseRef: TypeAlias = QueryGraphEntityRef | QueryGraphPathRef | QueryGraphRelationshipRef


class QueryGraphProjectedRef(BaseModel):
    """Projected result row: selected values plus optional source-row references."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]
    source: QueryGraphBaseRef | None = None


QueryGraphResultRef: TypeAlias = QueryGraphBaseRef | QueryGraphProjectedRef


class QueryGraphToolResult(BaseModel):
    """Normalized graph transport for query output (`layout="graph"`).

    Carries the same information as the rows layout of
    :class:`QueryToolResult`: `nodes` holds each unique entity once,
    `edges` each unique PHYSICAL relationship once (edge identity =
    relationship type + endpoints + `edge_key`; traversal-step aliases are
    carried per occurrence on path step refs and include item refs, never
    on the card), `results` preserves today's row order as index
    references, and `paths` holds step-ref sequences (edge index + alias)
    for path-shaped results so `dedupe=path` semantics stay distinct. Under
    `profile="compact"`, repeated non-empty include maps are interned in
    `include_sets` and a result/source `includes` integer indexes that table;
    standard/full retain their inline include maps.
    Envelope, truncation, policy-summary, and receipt fields are verbatim
    :class:`QueryToolResult` passthrough — normalization happens strictly
    after filtering, ordering, and pagination.
    """

    layout: Literal["graph"] = "graph"
    nodes: list[QueryEntityItem] = Field(default_factory=list)
    edges: list[QueryGraphEdgeItem] = Field(default_factory=list)
    results: list[QueryGraphResultRef] = Field(default_factory=list)
    paths: list[list[QueryGraphPathStepRef]] = Field(default_factory=list)
    include_sets: list[dict[str, QueryGraphCompactIncludeResult]] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    receipt_id: str | None
    receipt: dict[str, Any] | None
    total: int
    limit: int | None = None
    offset: int = 0
    truncated: bool = False
    limit_truncated: bool = False
    path_truncated: bool = False
    truncation_reasons: list[str] = Field(default_factory=list)
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    total_path_count: int | None = None
    retained_path_count: int | None = None
    steps_executed: int
    result_shape: Literal["entity", "path", "relationship"] = "path"
    dedupe: Literal["entity", "path", "none"] = "path"
    relationship_state: QueryVisibilityState = "live"
    lifecycle_status: LifecycleStatus | None = None
    param_hints: "QueryParamHints | None" = None
    policy_summary: dict[str, int] = Field(default_factory=dict)
    # Monotonic state revision at read time; receipts prove computation,
    # never freshness — compare read_revision to detect staleness.
    read_revision: int | None = None


class InlineQueryDefinition(BaseModel):
    name: str = Field(description="Name for this one-off query; must be non-empty.")
    mode: QueryMode = Field(
        description="'collection' to scan one entity type, 'traversal' to walk relationships."
    )
    description: str | None = Field(
        default=None, description="Optional human-readable description of the query."
    )
    entry_point: str | None = Field(
        default=None,
        description="Entity type to start from; forbidden in collection mode.",
    )
    traversal: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered relationship steps to walk in traversal mode.",
    )
    returns: str = Field(description="Alias of the entity/relationship the query returns.")
    result_shape: QueryResultShape = Field(
        default="path",
        description="Row shape to return: entity, path, or relationship.",
    )
    dedupe: QueryDedupe | None = Field(
        default=None,
        description="Deduplicate rows by entity, path, or none.",
    )
    relationship_state: QueryVisibilityState = Field(
        default="live",
        description=(
            "Default read-visibility state for this query: live, accepted, all, "
            "not-live, pending, or reviewable."
        ),
    )
    allow_relationship_state_override: bool = Field(
        default=False,
        description="Permit callers to override the visibility state at run time.",
    )
    where: dict[str, Any] | None = Field(
        default=None, description="Filter predicates applied to matched rows."
    )
    select: dict[str, Any] | None = Field(
        default=None, description="Projection of fields to return per row."
    )
    order_by: list[dict[str, Any]] = Field(
        default_factory=list, description="Ordering keys for deterministic paging."
    )
    include: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="One-hop side-context relationships to attach per row.",
    )
    limit: int | None = Field(default=None, ge=0, description="Maximum rows to return.")
    max_paths: int | None = Field(
        default=None, gt=0, description="Cap on total traversal paths explored."
    )
    max_paths_per_result: int | None = Field(
        default=None, gt=0, description="Cap on retained paths per result entity."
    )

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("inline query name must be non-empty")
        return value


class DecisionRecordResult(BaseModel):
    record: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)
    # The mutation receipt for open/finalize/abandon. The service result has
    # carried it since decision records became receipted; without it on the
    # contract the receipt exists but is unreachable from the call that made it,
    # so a caller cannot cite the proof of its own governed act. Null on reads.
    receipt_id: str | None = None


class DecisionRecordListResult(ListEnvelopeFields):
    items: list[dict[str, Any]] = Field(default_factory=list)


class DecisionEventListResult(ListEnvelopeFields):
    items: list[dict[str, Any]] = Field(default_factory=list)


class InstallDetailResult(BaseModel):
    """One install ledger record with its owned objects and phase history.

    The three payloads stay open dicts on purpose: the install ledger's write
    surface is service-internal until the installer ships, so pinning its field
    shapes into the frozen client contract now would freeze a record whose
    consumers do not exist yet.
    """

    install: dict[str, Any]
    owned_objects: list[dict[str, Any]] = Field(default_factory=list)
    phase_history: list[dict[str, Any]] = Field(default_factory=list)


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


class ListResult(ListEnvelopeFields):
    items: list[dict[str, Any]]
    # Present iff truncated and resumable: opaque cursor for the next page,
    # bound to this instance/config/read_revision/filter set. Replay after a
    # mutation raises a typed 409 StaleContinuationError — restart the read.
    continuation_token: str | None = None


class GateCandidateOutcome(BaseModel):
    candidate: str
    satisfied: bool
    satisfying_entity_ids: list[str] = Field(default_factory=list)


class GateEvaluationResult(BaseModel):
    gate_name: str
    kind: str | None = None
    candidates: list[str] = Field(default_factory=list)
    candidate_outcomes: list[GateCandidateOutcome] = Field(default_factory=list)
    verdict: GateEvaluationVerdict
    reason: str | None = None
    instance_id: str
    read_revision: int
    receipt_id: str


class TraceListResult(ListEnvelopeFields):
    items: list[dict[str, Any]] = Field(default_factory=list)


class ReceiptExplanationResult(BaseModel):
    receipt_id: str
    format: ReceiptExplanationFormat
    content: str


class EvaluateResult(BaseModel):
    entity_count: int
    edge_count: int
    findings: list[dict[str, Any]]
    summary: dict[str, int]
    constraint_summary: dict[str, int] = Field(default_factory=dict)
    quality_summary: dict[str, int] = Field(default_factory=dict)


class StateHealthGroupsSection(BaseModel):
    """Candidate-group lifecycle counts plus the unresolved-backlog age span.

    Age is scoped to unresolved (pending_review + applying) groups; resolved
    groups only accumulate age and are not an actionable maintenance signal.
    """

    pending_review_count: int = 0
    applying_count: int = 0
    resolved_count: int = 0
    withdrawn_count: int = 0
    total_count: int = 0
    oldest_unresolved_age_seconds: float | None = None
    newest_unresolved_age_seconds: float | None = None
    # Deprecated: always 0. ``auto_resolved`` is no longer a status any code path
    # writes, so this bucket cannot grow; it is re-emitted only so a 0.2.x reader
    # that requires the key keeps parsing. An honest zero, not a suppressed
    # count: legacy ``auto_resolved`` rows are terminal dead-ends and are counted
    # nowhere else either. Removal follows 0.3; read ``withdrawn_count``.
    auto_resolved_count: int = 0


class StateHealthSignalsSection(BaseModel):
    """Support-signal counts pending review under the evidence guard."""

    unevidenced_support_by_source: dict[str, int] = Field(default_factory=dict)


class StateHealthProvenanceSection(BaseModel):
    """Edge provenance tally by source_ref class."""

    direct_write_edge_count: int = 0
    group_backed_edge_count: int = 0
    other_source_edge_count: int = 0
    total_edge_count: int = 0


class StateHealthFreshnessSection(BaseModel):
    """Source-artifact / provider-trace recency plus config-compatibility facts."""

    source_artifact_count: int = 0
    oldest_source_artifact_age_seconds: float | None = None
    provider_trace_count: int = 0
    oldest_provider_trace_age_seconds: float | None = None
    config_compatible: bool = True
    config_warnings: list[str] = Field(default_factory=list)


class StateHealthIntegritySection(BaseModel):
    """Graph-integrity counts reused from the deterministic evaluate findings."""

    orphan_entity_count: int = 0
    unused_entity_types: list[str] = Field(default_factory=list)
    unused_relationship_types: list[str] = Field(default_factory=list)
    configuration_locked: bool | None = None


class StateHealthResult(BaseModel):
    """Read-only deterministic state-health report.

    Aggregates raw maintenance metrics (counts, ages, timestamps) and binary
    deterministic facts only. Carries NO scoring, ranking, severity, or
    threshold-derived statuses — agents interpret; core reports defensible facts.
    """

    captured_at: str
    head_snapshot_id: str | None = None
    groups: StateHealthGroupsSection = Field(default_factory=StateHealthGroupsSection)
    signals: StateHealthSignalsSection = Field(default_factory=StateHealthSignalsSection)
    provenance: StateHealthProvenanceSection = Field(default_factory=StateHealthProvenanceSection)
    freshness: StateHealthFreshnessSection = Field(default_factory=StateHealthFreshnessSection)
    integrity: StateHealthIntegritySection = Field(default_factory=StateHealthIntegritySection)


class LintSummary(BaseModel):
    config_warning_count: int = 0
    compatibility_warning_count: int = 0
    evaluation_finding_count: int = 0
    feedback_report_count: int = 0
    feedback_issue_count: int = 0
    outcome_report_count: int = 0
    outcome_issue_count: int = 0


class SampleResult(ListEnvelopeFields):
    items: list[dict[str, Any]]
    entity_type: str


class DirectWriteGroupInteraction(BaseModel):
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
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""


class AddRelationshipResult(BaseModel):
    added: int
    updated: int
    pending_conflicts: list[DirectWriteGroupInteraction] = Field(default_factory=list)
    updated_group_backed_edges: list[DirectWriteGroupInteraction] = Field(default_factory=list)
    receipt_id: str | None = None


class SimilarExistingEntity(BaseModel):
    entity_id: str
    matched_properties: list[str]


class EntityIdentityWarning(BaseModel):
    entity_type: str
    entity_id: str
    similar_existing_entity: SimilarExistingEntity


class AddEntityResult(BaseModel):
    entities_added: int
    entities_updated: int
    identity_warnings: list[EntityIdentityWarning] = Field(default_factory=list)
    receipt_id: str | None = None


class ClaimLifecycleResult(BaseModel):
    action: Literal["supersede", "retract"]
    claim: dict[str, Any]
    reason: str
    successor: dict[str, Any] | None = None
    receipt_id: str | None = None


class EntityLifecycleResult(BaseModel):
    action: Literal["supersede", "retire"]
    entity: dict[str, Any]
    reason: str
    successor: dict[str, Any] | None = None
    stranded_live_edge_count: int = 0
    receipt_id: str | None = None


class BatchDirectWriteResult(BaseModel):
    dry_run: bool
    valid: bool
    entities_added: int = 0
    entities_updated: int = 0
    relationships_added: int = 0
    relationships_updated: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)
    identity_warnings: list[EntityIdentityWarning] = Field(default_factory=list)
    evidence_sources_used: list[str] = Field(default_factory=list)
    pending_conflicts: list[DirectWriteGroupInteraction] = Field(default_factory=list)
    updated_group_backed_edges: list[DirectWriteGroupInteraction] = Field(default_factory=list)
    receipt_id: str | None = None


class AddConstraintResult(BaseModel):
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = Field(default_factory=list)
    receipt_id: str | None = None


class GetEntityResult(BaseModel):
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    read_revision: int | None = None


class GetRelationshipResult(BaseModel):
    found: bool
    from_type: str
    from_id: str
    relationship_type: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RelationshipLineageResult(BaseModel):
    found: bool
    relationship: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    group: dict[str, Any] | None = None
    resolution: dict[str, Any] | None = None
    source_workflow_receipt_id: str | None = None
    source_trace_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class QueryParamHints(BaseModel):
    entry_point: str | None
    required_params: list[str] = Field(default_factory=list)
    primary_key: str | None = None
    example_ids: list[str] = Field(default_factory=list)


class StatsResult(BaseModel):
    entity_count: int
    edge_count: int
    entity_counts: dict[str, int] = Field(default_factory=dict)
    relationship_counts: dict[str, int] = Field(default_factory=dict)
    status_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    head_snapshot_id: str | None = None
    read_revision: int | None = None


class BoundaryTelemetryCounter(BaseModel):
    surface_name: str
    call_count: int
    error_count: int
    total_response_bytes: int
    total_duration_ms: float
    max_duration_ms: float


class BoundaryTelemetrySummaryResult(BaseModel):
    earliest_recorded_at: datetime | None = None
    counters: list[BoundaryTelemetryCounter] = Field(default_factory=list)
    # Completeness of the counters above. Capture is best-effort by design, so a
    # non-zero drop total is the difference between "quiet" and "undercounted".
    dropped_observations: int = 0
    dropped_events: int = 0


class ServerInfoResult(BaseModel):
    server_required: bool
    state_dir: str
    version: str
    instance_count: int
    auth_enabled: bool
    auth_required: bool


class ServerRestartResult(BaseModel):
    """Acknowledgement that an in-place daemon re-exec has been scheduled."""

    scheduled: bool
    version: str
    state_dir: str


class NamedQueryInfoResult(BaseModel):
    name: str
    mode: Literal["collection", "traversal"]
    entry_point: str | None
    required_params: list[str] = Field(default_factory=list)
    returns: str
    result_shape: Literal["entity", "path", "relationship"] = "path"
    dedupe: Literal["entity", "path", "none"] = "path"
    relationship_state: QueryVisibilityState = "live"
    allow_relationship_state_override: bool = False
    select: dict[str, Any] | None = None
    order_by: list[dict[str, Any]] = Field(default_factory=list)
    include: dict[str, dict[str, Any]] = Field(default_factory=dict)
    limit: int | None = None
    max_paths: int | None = None
    max_paths_per_result: int | None = None
    description: str | None = None
    example_ids: list[str] = Field(default_factory=list)


QueryListDetail = Literal["summary", "full"]


class QueryDefinitionSummary(BaseModel):
    """Bounded discovery card for one named query.

    Exactly the fields needed to pick a query and invoke it; no select,
    order_by, include, or budget internals — describe_query is the
    canonical full-definition read.
    """

    name: str
    description: str | None = None
    mode: Literal["collection", "traversal"]
    entry_point: str | None
    returns: str
    result_shape: Literal["entity", "path", "relationship"] = "path"
    required_params: list[str] = Field(default_factory=list)
    allow_relationship_state_override: bool = False


class QueryListResult(ListEnvelopeFields):
    items: list[QueryDefinitionSummary] = Field(default_factory=list)
    continuation_token: str | None = None


class QueryListDetailResult(ListEnvelopeFields):
    items: list[NamedQueryInfoResult] = Field(default_factory=list)
    continuation_token: str | None = None


class InspectNeighborResult(BaseModel):
    direction: Literal["incoming", "outgoing"]
    relationship_type: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    entity: dict[str, Any]


class InspectEntityResult(BaseModel):
    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    neighbors: list[InspectNeighborResult] = Field(default_factory=list)
    total_neighbors: int = 0
    read_revision: int | None = None


NeighborhoodTruncationReason = Literal["node_budget", "edge_budget", "depth"]


class NeighborhoodNodeResult(BaseModel):
    """One returned non-root entity of a bounded neighborhood read."""

    entity_type: str
    entity_id: str
    depth: int
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NeighborhoodEdgeResult(BaseModel):
    """One returned edge of a bounded neighborhood read.

    ``metadata`` keeps the assertion review/lifecycle markers so
    pending/live/rejected/superseded edges never flatten together.
    """

    relationship_type: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    edge_key: int | None = None
    claim_id: str | None = None
    """Stable minted claim identity; survives pulls, unlike `edge_key`."""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    corroboration: dict[str, Any] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class InspectNeighborhoodResult(BaseModel):
    """Expanded bounded-neighborhood inspect result.

    Opt-in shape: returned by the inspect-entity surface when any
    neighborhood parameter (``depth``, ``relationship_types``,
    ``target_types``, ``state``, ``projection``, ``max_nodes``,
    ``max_edges``) is provided. Calls without them keep the legacy
    :class:`InspectEntityResult` single-hop shape bit-for-bit. The root
    entity card is the top-level ``properties``/``metadata``; ``nodes``
    holds non-root entities with their BFS depth.
    """

    found: bool
    entity_type: str
    entity_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    depth: int = 1
    state: QueryVisibilityState = "all"
    nodes: list[NeighborhoodNodeResult] = Field(default_factory=list)
    edges: list[NeighborhoodEdgeResult] = Field(default_factory=list)
    truncated: bool = False
    truncation_reasons: list[NeighborhoodTruncationReason] = Field(default_factory=list)
    nodes_returned: int = 0
    edges_returned: int = 0
    # Edges excluded solely by an explicit state filter: they passed the
    # direction/relationship/target filters and were hidden by state alone,
    # counted at the frontier the BFS actually explored (hidden edges consume
    # no budget and are never traversed). Always 0 when state is "all" (the
    # default — nothing is hidden); always present so absence is unambiguous.
    edges_hidden_by_state: int = 0
    # Monotonic state revision at read time; receipts prove computation,
    # never freshness — compare read_revision to detect staleness.
    read_revision: int | None = None
    # Present iff truncated on a budget (node_budget/edge_budget) — resume the
    # BFS with `continuation=...`; depth-horizon truncation is not resumable.
    continuation_token: str | None = None


class PropertyChangeItem(BaseModel):
    property: str
    from_value: Any | None = None
    to_value: Any | None = None


class EntityChangeHistoryItem(BaseModel):
    entity_type: str
    entity_id: str
    change_kind: Literal["created", "updated"]
    property_changes: list[PropertyChangeItem] = Field(default_factory=list)
    changed_at: datetime
    receipt_id: str
    operation_type: str
    actor_context: dict[str, Any] | None = None


class EntityChangeHistoryResult(ListEnvelopeFields):
    entity_type: str
    entity_id: str | None = None
    items: list[EntityChangeHistoryItem] = Field(default_factory=list)
    total: int = 0
    legacy_entity_write_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class CanonicalViewResult(BaseModel):
    view: str
    payload: dict[str, Any]


class ConfigTypeDelta(BaseModel):
    entity_types_added: list[str] = Field(default_factory=list)
    entity_types_removed: list[str] = Field(default_factory=list)
    relationship_types_added: list[str] = Field(default_factory=list)
    relationship_types_removed: list[str] = Field(default_factory=list)


class ConfigStrandingReport(BaseModel):
    entity_types: dict[str, int] = Field(default_factory=dict)
    relationship_types: dict[str, int] = Field(default_factory=dict)


class ReloadConfigResult(BaseModel):
    config_path: str
    updated: bool
    warnings: list[str] = Field(default_factory=list)
    type_delta: ConfigTypeDelta = Field(default_factory=ConfigTypeDelta)
    strandings: ConfigStrandingReport = Field(default_factory=ConfigStrandingReport)


class ConfigStatusResult(BaseModel):
    status: Literal[
        "untracked",
        "materialized_modified",
        "source_changed",
        "source_unchecked",
        "in_sync",
    ]
    config_path: str
    materialized_matches: bool | None
    sources_checked: bool
    composed_matches: bool | None
    changed_sources: list[str] = Field(default_factory=list)
    provenance: ConfigProvenance | None = None


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
    mode: WorkflowMode
    workflow_type: WorkflowType
    canonical: bool
    apply_digest: str | None = None
    head_snapshot_id: str | None = None
    committed_snapshot_id: str | None = None
    apply_previews: dict[str, Any] = Field(default_factory=dict)
    query_receipt_ids: list[str] = Field(default_factory=list)
    read_metadata: dict[str, Any] = Field(default_factory=dict)
    trace_ids: list[str] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    traces: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowRunResult(WorkflowExecutionResult):
    mode: WorkflowMode = "run"
    workflow_type: WorkflowType = "utility"
    canonical: bool = False


class WorkflowApplyResult(WorkflowExecutionResult):
    mode: WorkflowMode = "apply"
    workflow_type: WorkflowType = "canonical"
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
    mode: WorkflowMode = "proposal"
    workflow_type: WorkflowType = "proposal"
    canonical: bool = False
    group_id: str | None = None
    group_status: str
    review_priority: str
    suppressed: bool = False
    suppressed_members: list[SuppressedProposalMember] = Field(default_factory=list)
    query_receipt_ids: list[str] = Field(default_factory=list)
    read_metadata: dict[str, Any] = Field(default_factory=dict)
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
    receipt_id: str | None = None


class SnapshotListResult(ListEnvelopeFields):
    items: list[SnapshotMetadata] = Field(default_factory=list)


class CloneSnapshotResult(BaseModel):
    instance_id: str
    snapshot: SnapshotMetadata
    # One-time initial ADMIN credential for the cloned instance, present only on
    # auth-enabled daemons. Mirrors the claim-bootstrap contract: the plaintext
    # token is returned exactly once here and only its hash is stored.
    admin_credential: RuntimeCredentialBootstrapResult | None = None


class InstanceBackupManifest(BaseModel):
    format_version: int = 1
    instance_id: str
    created_at: str
    cruxible_version: str
    label: str | None = None
    original_config_path: str
    restored_config_path: str = "config.yaml"
    instance_mode: str
    artifacts: dict[str, str] = Field(default_factory=dict)


class InstanceBackupResult(BaseModel):
    instance_id: str
    artifact_path: str
    manifest: InstanceBackupManifest


class InstanceRestoreResult(BaseModel):
    instance_id: str
    root_dir: str
    manifest: InstanceBackupManifest
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered"


class InstanceRelocateResult(BaseModel):
    instance_id: str
    from_dir: str
    to_dir: str
    manifest: InstanceBackupManifest
    source_removed: bool = False
    registry_status: Literal["registered", "repaired", "unchanged"] = "registered"


class PublishedStateManifest(BaseModel):
    format_version: int
    state_id: str
    release_id: str
    snapshot_id: str
    compatibility: StateCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    parent_release_id: str | None = None
    bundle_format_version: int | None = None
    members_digest: str | None = None


class UpstreamMetadataResult(BaseModel):
    transport_ref: str
    requested_source_ref: str | None = None
    requested_transport_ref: str | None = None
    state_id: str
    release_id: str
    snapshot_id: str
    compatibility: StateCompatibility
    owned_entity_types: list[str] = Field(default_factory=list)
    owned_relationship_types: list[str] = Field(default_factory=list)
    overlay_config_path: str
    manifest_path: str
    graph_path: str
    upstream_config_path: str
    lock_path: str
    manifest_digest: str | None = None
    graph_digest: str | None = None
    upstream_config_digest: str | None = None
    upstream_lock_digest: str | None = None
    bundle_format_version: int | None = None
    members_digest: str | None = None


class StatePublishResult(BaseModel):
    manifest: PublishedStateManifest


class StateOverlayResult(BaseModel):
    instance_id: str
    manifest: PublishedStateManifest
    warnings: list[str] = Field(default_factory=list)


class StateStatusResult(BaseModel):
    upstream: UpstreamMetadataResult | None = None


class StatePullPreviewResult(BaseModel):
    current_release_id: str | None = None
    target_release_id: str
    compatibility: StateCompatibility
    apply_digest: str
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    lock_changed: bool = False
    upstream_entity_delta: int = 0
    upstream_edge_delta: int = 0


class StatePullApplyResult(BaseModel):
    release_id: str
    apply_digest: str
    pre_pull_snapshot_id: str
    receipt_id: str | None = None


class StateDiffArtifactRef(BaseModel):
    """Where the complete canonical diff artifact was persisted."""

    path: str
    diff_digest: str
    byte_count: int


class StateDiffResult(BaseModel):
    """A structured difference between two state coordinates.

    ``sections`` is the BOUNDED view (per-bucket caps, oversized values elided)
    and ``view_digest`` covers exactly those bytes. ``diff_digest`` always
    covers the complete unelided body persisted at ``artifact_ref``; only an
    ``artifact_complete: true`` result may be treated as a reviewed plan.
    """

    diff_digest: str
    view_digest: str
    artifact_complete: bool
    artifact_ref: StateDiffArtifactRef
    diff_engine_version: str
    artifact_schema_version: int
    artifact_trust: str
    normalizations: list[str] = Field(default_factory=list)
    liveness: str = "not_evaluated"
    selector: dict[str, Any] = Field(default_factory=dict)
    from_coordinate: dict[str, Any] = Field(default_factory=dict)
    to_coordinate: dict[str, Any] = Field(default_factory=dict)
    omitted_sections: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, int] = Field(default_factory=dict)
    view: dict[str, Any] = Field(default_factory=dict)
    default_basis: str | None = None
    receipt_id: str | None = None


class StateDiffArtifactResult(BaseModel):
    """One persisted diff artifact, fetched by its content address.

    ``content_bytes`` is the file's exact UTF-8 text: hashing it reproduces
    ``diff_digest`` with no serializer of the caller's own involved.
    ``content`` is the same body parsed, for reading rather than verifying --
    re-serializing it reproduces the digest only under Cruxible's exact
    canonical form.
    """

    diff_digest: str
    path: str
    byte_count: int
    content_bytes: str = ""
    content: dict[str, Any] = Field(default_factory=dict)


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
    resolution_id: str | None = None
    auto_resolve_deferred_reason: str | None = None


class AddDecisionPolicyResult(BaseModel):
    name: str
    added: bool
    config_updated: bool
    warnings: list[str] = Field(default_factory=list)
    receipt_id: str | None = None


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
    feedback_population_count: int = 0
    truncated: bool = False
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
    outcome_population_count: int = 0
    truncated: bool = False
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
    # Per-member explanation for every skipped member: identity fields plus a
    # ``skip_kind`` ("existing_edge"/"validation_failed"), a human-readable
    # ``reason``, and ``stamped`` ("true"/"false" — whether stamp-existing
    # blessed the surviving edge). Empty when nothing was skipped.
    skipped_members: list[dict[str, str]] = Field(default_factory=list)
    # Count of pre-existing edges blessed with the group's review/provenance
    # when ``stamp_existing`` was requested.
    edges_stamped: int = 0


class UpdateTrustStatusToolResult(BaseModel):
    resolution_id: str
    trust_status: str
    receipt_id: str | None = None


class GetGroupToolResult(BaseModel):
    group: dict[str, Any]
    members: list[dict[str, Any]]
    resolution: dict[str, Any] | None = None
    bucket_status: dict[str, Any] | None = None
    member_review: list[dict[str, Any]] = Field(default_factory=list)


class ListGroupsToolResult(ListEnvelopeFields):
    items: list[dict[str, Any]]


class ListResolutionsToolResult(ListEnvelopeFields):
    items: list[dict[str, Any]]


class GroupStatusHistoryItem(BaseModel):
    resolution_id: str
    action: str
    trust_status: str
    confirmed: bool
    resolved_at: str
    tuple_count: int
    rationale: str = ""
    resolved_by: str = ""
    resolution_source: str = "review"
    resolved_actor: dict[str, Any] | None = None


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
