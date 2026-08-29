"""Pydantic wire contracts for the Playbill-only public surface."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.primitives import canonical_json

RuntimeCredentialPermissionMode = Literal[
    "read_only",
    "governed_write",
    "graph_write",
    "admin",
]
PlaybillHostStatus = Literal["created", "already_exists"]
PlaybillAuthoringExampleName = Literal[
    "claim-type",
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
]
PlaybillNextReason: TypeAlias = Literal[
    "claim_conflicted",
    "claim_uncovered",
    "claim_stale_evidence",
    "citation_drifted",
    "citation_source_unobserved",
    "evidence_expiring",
    "floor_missing",
    "floor_stale",
    "floor_invalid",
    "projection_dirty",
    "projection_backing_stale",
    "self_published_source_stale",
    "claim_dependency_stale",
    "claim_attestation_threshold_met",
    "claim_contradicting_evidence_available",
    "claim_new_evidence_supporting",
    "claim_new_evidence_unreviewed",
    "document_modified",
    "claim_cites_retired",
    "retired_claim_source_stale",
    "unregistered_projection_block",
]


class PlaybillHostResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: PlaybillHostStatus


class RuntimeCredentialBootstrapResult(BaseModel):
    credential_id: str
    instance_id: str
    permission_mode: Literal["admin"]
    token: str


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


class ServerInfoResult(BaseModel):
    server_required: bool
    state_dir: str
    version: str
    instance_count: int
    auth_enabled: bool
    auth_required: bool


class ServerRestartResult(BaseModel):
    scheduled: bool
    version: str
    state_dir: str


class PlaybillAcceptedCoordinate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-accepted-coordinate-v1"] = "playbill-accepted-coordinate-v1"
    git_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    semantic_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PlaybillInitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-init-v1"] = "playbill-init-v1"
    instance_id: str
    coordinate: PlaybillAcceptedCoordinate
    trust_root: dict[str, Any]
    recovery_posture: str


class PlaybillCasObjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: str
    present: bool
    byte_length: int | None
    redacted: bool


class PlaybillProposalInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-inspection-v1"] = "playbill-proposal-inspection-v1"
    proposal: dict[str, Any]
    accepted_coordinate: PlaybillAcceptedCoordinate
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PlaybillProposalListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-list-entry-v1"] = "playbill-proposal-list-entry-v1"
    proposal_id: str
    actor_id: str
    target_ref: str
    admitted_at: str
    verdict: Literal["candidate", "refused"]
    candidate_digest: str | None = None
    status: Literal["open", "settled"]
    terminal_reason: Literal["accepted", "refused", "stale"] | None = None


class PlaybillProposalList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-list-v1"] = "playbill-proposal-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    status_filter: Literal["open", "settled"] | None = None
    entries: list[PlaybillProposalListEntry]


class PlaybillProposalReadmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-readmit-result-v1"]
    source_proposal_id: str
    operation_digest: str
    proposal: PlaybillProposalInspection


class PlaybillWhoAmI(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-whoami-v1"] = "playbill-whoami-v1"
    actor_id: str
    credential_label: str
    actor_id_source: Literal["runtime_credential_label", "local_operator"]
    credential_permission_mode: Literal["read_only", "governed_write", "graph_write", "admin"]
    principal_registration_status: Literal["active", "revoked", "absent"]
    active_principal_ids: list[str]
    coordinate: PlaybillAcceptedCoordinate


class PlaybillRefusalInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-refusal-v1"] = "playbill-refusal-v1"
    proposal_id: str
    verdict: Literal["candidate", "refused"]
    diagnostics: list[dict[str, Any]]


class PlaybillProposalReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-review-v1"] = "playbill-proposal-review-v1"
    coordinate_kind: Literal["provisional"] = "provisional"
    proposal_id: str
    candidate: dict[str, Any]
    candidate_digest: str
    parent_semantic_root: str
    settlement_base: PlaybillAcceptedCoordinate
    base_oid: str
    complete_members: list[dict[str, Any]]
    members: list[dict[str, Any]]
    governance: dict[str, Any]
    provenance: dict[str, Any]
    attestation_coverage: dict[str, Any]
    documents: list[dict[str, Any]]
    redactions: list[str]


class PlaybillApprovalChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-challenge-v1"] = "playbill-approval-challenge-v1"
    proposal_id: str
    signer_principal: dict[str, Any]
    signer_key_history_ref: str
    statement: dict[str, Any]
    review: PlaybillProposalReview


class PlaybillApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-receipt-v1"] = "playbill-approval-receipt-v1"
    proposal_id: str
    candidate_digest: str
    signer_id: str
    submitted_by: str
    signing_semantic_root: str
    attestation_digest: str
    key_history_ref: str


class PlaybillActivationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-activation-receipt-v1"] = "playbill-activation-receipt-v1"
    proposal_id: str
    activated_by: str
    status: Literal["accepted", "lost_cas"]
    accepted_coordinate: PlaybillAcceptedCoordinate | None


class PlaybillFloorRefreshResult(BaseModel):
    """Client-owned truth about the optional workspace floor refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-floor-refresh-result-v1"] = "playbill-floor-refresh-result-v1"
    status: Literal["not_configured", "refreshed", "failed"]
    path: str | None = None
    destination: str | None = None
    floor_digest: str | None = None
    message: str | None = None


class PlaybillWorkspaceActivationResult(PlaybillActivationReceipt):
    """Activation receipt plus the independent client-workspace refresh outcome."""

    floor_refresh: PlaybillFloorRefreshResult


class PlaybillDocumentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-document-read-v1"] = "playbill-document-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]


class PlaybillDocumentList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-document-list-v1"] = "playbill-document-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    documents: list[PlaybillDocumentView]


class PlaybillPrincipalList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-principal-list-v1"] = "playbill-principal-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    principals: list[dict[str, Any]]


class PlaybillBodyRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-document-body-v1"] = "playbill-document-body-v1"
    identity: str
    coordinate: PlaybillAcceptedCoordinate
    body_digest: str
    media_type: str
    content_base64: str


class PlaybillDocumentHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-document-history-v1"] = "playbill-document-history-v1"
    identity: str
    entries: list[dict[str, Any]]


class PlaybillExplainResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-explain-v1"] = "playbill-explain-v1"
    subject: dict[str, Any]
    coordinate: PlaybillAcceptedCoordinate
    detail: Literal["summary", "evidence"]
    governance: dict[str, Any]
    provenance: dict[str, Any]
    attestation_coverage: dict[str, Any]
    history: dict[str, Any]
    source_mapping: dict[str, Any] | None
    proof_references: list[dict[str, Any]]
    redactions: list[str]
    supported_details: list[str]


class PlaybillExplainUnsupportedDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-explain-unsupported-detail-v1"] = (
        "playbill-explain-unsupported-detail-v1"
    )
    subject: dict[str, Any]
    coordinate: PlaybillAcceptedCoordinate
    requested_detail: Literal["proof"]
    code: str
    message: str
    supported_details: list[str]


class PlaybillSourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-context-v1"] = "playbill-source-context-v1"
    accepted_coordinate: PlaybillAcceptedCoordinate
    documents: list[dict[str, Any]]


class PlaybillSourceCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-check-v1"] = "playbill-source-check-v1"
    compilation_digest: str
    accepted_coordinate: PlaybillAcceptedCoordinate
    alignments: list[dict[str, Any]]


class PlaybillSubjectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-read-v1"] = "playbill-subject-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]


class PlaybillSubjectList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-list-v1"] = "playbill-subject-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    subjects: list[PlaybillSubjectView]


class PlaybillSubjectHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-history-v1"] = "playbill-subject-history-v1"
    identity: str
    entries: list[dict[str, Any]]


class PlaybillClaimTypeView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-read-v1"] = "playbill-claim-type-read-v1"
    coordinate: PlaybillAcceptedCoordinate
    path: str
    predicate: str
    identity: str
    artifact_digest: str
    envelope: dict[str, Any]


class PlaybillClaimTypeList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-list-v1"] = "playbill-claim-type-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claim_types: list[PlaybillClaimTypeView]


class PlaybillClaimTypeProposalLint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-proposal-lint-v1"] = "playbill-claim-type-proposal-lint-v1"
    warnings: list[dict[str, Any]]


class PlaybillClaimTypeInputProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-input-proposal-result-v1"] = (
        "playbill-claim-type-input-proposal-result-v1"
    )
    proposal: PlaybillProposalInspection
    lint: PlaybillClaimTypeProposalLint


class PlaybillClaimTypeMigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v1"] = (
        "playbill-claim-type-migration-result-v1"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: PlaybillProposalInspection
    warnings: list[dict[str, Any]] = []
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PlaybillClaimTypeMigrationPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-preflight-v1"] = (
        "playbill-claim-type-migration-preflight-v1"
    )
    coordinate: PlaybillAcceptedCoordinate
    successor_artifact_digest: str
    dependents: list[dict[str, Any]]
    warnings: list[dict[str, Any]] = []
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PlaybillClaimTypeMigrationResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v2"] = (
        "playbill-claim-type-migration-result-v2"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: PlaybillProposalInspection
    warnings: list[dict[str, Any]] = []
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PlaybillClaimTypeMigrationResultV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v3"] = (
        "playbill-claim-type-migration-result-v3"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: PlaybillProposalInspection
    warnings: list[dict[str, Any]] = []
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


PlaybillClaimTypeMigrationResponse: TypeAlias = (
    PlaybillClaimTypeMigrationResult
    | PlaybillClaimTypeMigrationPreflight
    | PlaybillClaimTypeMigrationResultV2
    | PlaybillClaimTypeMigrationResultV3
)


class PlaybillClaimView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-read-v1"] = "playbill-claim-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]


class PlaybillCaptureEvidenceKindAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-capture-evidence-kind-admission-v1"]
    evidence_kind: str
    status: Literal["admitted", "not_admitted"]
    rule_id: str | None = None
    admission: Literal["origin_only", "direct", "derivational"] | None = None
    refusal_code: str | None = None
    closest_rule_id: str | None = None


class PlaybillCaptureAdmissionAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-capture-admission-account-v1"]
    citation_id: str
    capture_digest: str
    citation_role: Literal["evidence", "copy", "legacy"]
    citation_origin: Literal["independent", "self_source", "self_published", "legacy"]
    capture_contract_identity: str
    capture_contract_digest: str
    status: Literal["admitted", "not_admitted", "not_evidence"]
    decisions: list[PlaybillCaptureEvidenceKindAdmission]


class PlaybillClaimViewV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-read-v2"]
    coordinate_kind: Literal["canonical"]
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]
    admission_evaluation_time: str
    admission_accounts: list[PlaybillCaptureAdmissionAccount]


class PlaybillClaimList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-list-v1"] = "playbill-claim-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    claims: list[PlaybillClaimView]


class PlaybillClaimHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-history-v1"] = "playbill-claim-history-v1"
    identity: str
    entries: list[dict[str, Any]]


class PlaybillClaimRetirePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-retire-preflight-v1"] = "playbill-claim-retire-preflight-v1"
    operation_digest: str
    coordinate: PlaybillAcceptedCoordinate
    root_identity: dict[str, Any]
    root_predecessor_digest: str
    reason: Literal["was-rescinded", "was-wrong"]
    effective_until: str | None
    required_dependents: list[dict[str, Any]]
    # Advisory, never required: live Claims left citing this Claim's Captures.
    citing_claims: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]]
    submit_ready: bool


class PlaybillClaimRetireResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-retire-result-v1"] = "playbill-claim-retire-result-v1"
    outcome: Literal["preflight", "proposed", "already_retired"]
    operation_digest: str
    coordinate: PlaybillAcceptedCoordinate
    retirements: list[dict[str, Any]]
    proposal: PlaybillProposalInspection | None = None


PlaybillClaimRetireResponse: TypeAlias = PlaybillClaimRetirePreflight | PlaybillClaimRetireResult


class PlaybillClaimExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-explanation-v1"] = "playbill-claim-explanation-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    claim: PlaybillClaimView
    law_evidence: dict[str, Any]
    verdict: dict[str, Any]
    exact_attestations: list[dict[str, Any]]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: list[dict[str, Any]]
    coverage: dict[str, Any]


class PlaybillClaimExplanationV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-explanation-v2"]
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    claim: PlaybillClaimView
    law_evidence: dict[str, Any]
    verdict: dict[str, Any]
    exact_attestations: list[dict[str, Any]]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: list[dict[str, Any]]
    coverage: dict[str, Any]
    admission_evaluation_time: str
    admission_accounts: list[PlaybillCaptureAdmissionAccount]


class PlaybillClaimExplanationV3(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-explanation-v3"]
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    claim: PlaybillClaimView
    law_evidence: dict[str, Any]
    verdict: dict[str, Any]
    exact_attestations: list[dict[str, Any]]
    approval_coverage: Literal["containing_change_set"] = "containing_change_set"
    source_handles: list[dict[str, Any]]
    coverage: dict[str, Any]
    admission_evaluation_time: str
    admission_accounts: list[PlaybillCaptureAdmissionAccount]
    freshness: list[dict[str, Any]]


class PlaybillCandidateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-candidate-status-v1"] = "playbill-candidate-status-v1"
    state: Literal[
        "draft",
        "preflight_refused",
        "ready_to_submit",
        "awaiting_external_approval",
        "approval_invalid",
        "ready_to_activate",
        "conflicted_after_rebase",
        "superseded",
        "accepted",
        "terminal",
    ]
    proposal_id: str | None = None
    candidate_digest: str | None = None
    current_accepted_coordinate: PlaybillAcceptedCoordinate
    path_to_acceptance: list[dict[str, Any]] = Field(default_factory=list)
    accepted_generation: PlaybillAcceptedCoordinate | None = None


class PlaybillAuthoringIntentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-intent-view-v1"] = "playbill-authoring-intent-view-v1"
    intent: dict[str, Any]


class PlaybillAuthoringExampleResult(BaseModel):
    """One model-constructed, executable authoring input example."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-example-result-v1"] = "playbill-authoring-example-result-v1"
    name: PlaybillAuthoringExampleName
    payload: dict[str, Any]


class PlaybillAuthoringIntentList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-intent-list-v1"] = "playbill-authoring-intent-list-v1"
    intents: list[dict[str, Any]] = Field(default_factory=list)


class PlaybillAuthoringPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-preflight-result-v1"] = (
        "playbill-authoring-preflight-result-v1"
    )
    verdict: Literal["passed", "refused"]
    certificate: dict[str, Any]
    frontier: dict[str, Any]
    lint: PlaybillClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class PlaybillAuthoringSubmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: dict[str, Any]
    status: PlaybillCandidateStatus
    # True when this submit amends an existing Claim identity in place.
    identity_stable: bool = False
    claim_revision: int | None = None


class PlaybillInsertionPrepareResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-insertion-prepare-result-v2"]
    outcome: Literal[
        "prepared",
        "already_prepared",
        "bound",
        "expired",
        "claim_currency_changed",
    ]
    intent: dict[str, Any]
    expectation: dict[str, Any]
    preparation: dict[str, Any] | None = None
    warnings: list["PlaybillPublicationPrepareWarning"] = Field(default_factory=list)


class PlaybillPublicationPrepareWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-publication-prepare-warning-v1"]
    code: Literal["playbill.authoring.publication_citation_anchor_collision"]
    source_id: str
    citation_ids: list[str] = Field(min_length=1)

    @field_validator("citation_ids")
    @classmethod
    def _citation_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value), key=lambda item: item.encode("ascii")):
            raise ValueError("publication warning citation IDs must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value


class PlaybillInsertionConfirmResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-insertion-confirm-result-v2"]
    outcome: Literal["bound", "already_bound", "expired", "claim_currency_changed"]
    intent: dict[str, Any]
    expectation: dict[str, Any]


class PlaybillInsertionAbandonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-insertion-abandon-result-v1"] = "playbill-insertion-abandon-result-v1"
    intent: dict[str, Any]
    expectation: dict[str, Any]


class PlaybillQueryDefinitionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-definition-read-v1"] = "playbill-query-definition-read-v1"
    coordinate: PlaybillAcceptedCoordinate
    path: str
    name: str
    identity: str
    artifact_digest: str
    envelope: dict[str, Any]


class PlaybillQueryDefinitionList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-definition-list-v1"] = "playbill-query-definition-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    query_definitions: list[PlaybillQueryDefinitionView]


class PlaybillQueryRun(BaseModel):
    """One executed query: its replayable result beside its execution receipt.

    ``receipt`` carries the whole ``playbill-query-execution-receipt-v1``; its
    ``result_digest`` is the receipt's content identity, and
    ``journal_record_digest`` is present only when the caller owned a journal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-run-v1"] = "playbill-query-run-v1"
    coordinate: PlaybillAcceptedCoordinate
    name: str
    definition_path: str
    definition_digest: str
    result: dict[str, Any]
    receipt: dict[str, Any]
    journal_record_digest: str | None = None


class PlaybillProcedureReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-readiness-result-v1"] = (
        "playbill-procedure-readiness-result-v1"
    )
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    procedure_identity: dict[str, Any]
    procedure_artifact_digest: str
    state: Literal["ready", "binding_required", "unsupported"]
    required_slots: list[str]
    unsupported_nodes: list[dict[str, Any]]
    next_operation: dict[str, Any]


class PlaybillProcedureBindResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-bind-result-v1"] = "playbill-procedure-bind-result-v1"
    proposal: PlaybillProposalInspection
    readiness: PlaybillProcedureReadiness


class PlaybillProcedureRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-run-state-v1"] = "playbill-procedure-run-state-v1"
    run_id: str
    procedure_identity: dict[str, Any]
    procedure_artifact_digest: str
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    status: Literal[
        "binding_required",
        "unsupported",
        "running",
        "succeeded",
        "refused",
        "failed",
        "budget_exhausted",
    ]
    pending_inputs: list[str]
    outcomes: list[dict[str, Any]]
    next_operation: dict[str, Any]
    result: Any = None
    receipt_digest: str | None = None


class PlaybillNextResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-result-v1", "playbill-next-result-v2"] = "playbill-next-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    observed_domains: list[Literal["accepted_state", "workspace_floor", "workspace_sources"]]
    unobserved_domains: list[Literal["accepted_state", "workspace_floor", "workspace_sources"]]
    items: list[dict[str, Any]]
    result_digest: str
    # Set only on a delta: result_digest still names the whole queue, so it is
    # the cursor to echo back, not a description of the rows carried here.
    delta_since: str | None = None
    attestation_head_digest: str | None = None

    @model_validator(mode="after")
    def _attestation_coordinate(self) -> "PlaybillNextResult":
        if (self.tag == "playbill-next-result-v2") != (self.attestation_head_digest is not None):
            raise ValueError("Next v2 alone requires an attestation evidence head")
        if self.attestation_head_digest is not None:
            Sha256Value.from_tagged(self.attestation_head_digest)
        return self


class PlaybillCurationListResult(BaseModel):
    """G9 curation queue plus request-bound observation accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-curation-list-result-v1"] = "playbill-curation-list-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    generation: int = Field(ge=0)
    evaluation_time: str
    operational_head_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    items: list[dict[str, Any]] = Field(default_factory=list)
    detector_coverage: list[dict[str, Any]]
    observation_coverage: dict[str, Any]
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PlaybillCurationActionResult(BaseModel):
    """One attributed append-only curation lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-curation-action-result-v1"] = "playbill-curation-action-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    generation: int = Field(ge=0)
    operational_head_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    item: dict[str, Any]


class PlaybillAuditFactors(BaseModel):
    """Exact integer factors behind one audit row's rank."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    unique_dependent_count: int = Field(ge=0)
    qualifying_consumption_touch_count: int = Field(ge=0)
    stake: int = Field(ge=1)
    single_source: bool
    proposer_observed_only: bool
    zero_corroboration: bool
    near_freshness_horizon: bool
    weakness: int = Field(ge=1, le=5)
    first_accepted_generation: int = Field(ge=0)
    last_independent_verification_generation: int = Field(ge=0)
    never_verified: bool
    staleness: int = Field(ge=1)


class PlaybillAuditEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "accepted_claim",
        "claim_attestation",
        "claim_type",
        "consumption_aggregate",
        "dependent",
        "supporting_capture",
    ]
    identity: str
    artifact_digest: str | None = None
    generation: int | None = Field(default=None, ge=0)
    facts: dict[str, Any] = Field(default_factory=dict)


class PlaybillAuditRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-claim-row-v1"] = "playbill-audit-claim-row-v1"
    claim_path: str
    claim_identity: dict[str, Any]
    claim_artifact_digest: str
    claim_statement_digest: str
    subject_identity: dict[str, Any]
    claim_type_identity: dict[str, Any]
    verdict: str
    currency: str
    factors: PlaybillAuditFactors
    rank_score: int = Field(ge=1)
    evidence_refs: list[PlaybillAuditEvidenceRef]


class PlaybillAuditScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-scope-v1"] = "playbill-audit-scope-v1"
    claim_type_identities: list[str] = Field(default_factory=list)
    subject_kinds: list[str] = Field(default_factory=list)


class PlaybillAuditCoveredClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_identity: dict[str, Any]
    artifact_digest: str


class PlaybillAuditCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-coverage-v1"] = "playbill-audit-coverage-v1"
    access_permitted: bool
    declared_scope: PlaybillAuditScope
    covered_claims: list[PlaybillAuditCoveredClaim]
    candidate_claim_count: int = Field(ge=0)
    returned_claim_count: int = Field(ge=0)
    omitted_claim_count: int = Field(ge=0)
    omission_reasons: list[Literal["byte_budget_exceeded", "row_budget_exceeded"]]


class PlaybillAuditCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-cursor-v1"] = "playbill-audit-cursor-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    operational_input_head_digest: str
    scope_digest: str
    next_offset: int = Field(ge=1)
    cursor_digest: str


class PlaybillAuditResult(BaseModel):
    """Read-only ranked Claim patrol plus completed-run coverage accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-result-v1"] = "playbill-audit-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    generation: int = Field(ge=0)
    evaluation_time: str
    operational_input_head_digest: str
    audited_through_generation: int | None = Field(default=None, ge=0)
    rows: list[PlaybillAuditRow]
    coverage: PlaybillAuditCoverage
    next_cursor: PlaybillAuditCursor | None = None
    result_digest: str


def _since_digest(domain: str, payload: dict[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(canonical_json({"tag": domain, **payload}).encode("utf-8")).hexdigest()
    )


def _validate_since_access_profile(value: dict[str, Any]) -> dict[str, Any]:
    if (
        set(value)
        != {
            "tag",
            "profile_id",
            "permitted_access_classes",
            "disclose_restricted_existence",
        }
        or value.get("tag") != "playbill-coverage-access-profile-v1"
    ):
        raise ValueError("since access_profile is not a CoverageAccessProfileV1")
    classes = value.get("permitted_access_classes")
    if not isinstance(classes, list | tuple) or any(not isinstance(item, str) for item in classes):
        raise ValueError("since access_profile classes must be strings")
    if list(classes) != sorted(set(classes)):
        raise ValueError("since access_profile classes must be sorted and unique")
    if any(item not in {"public", "instance", "restricted"} for item in classes):
        raise ValueError("since access_profile contains an unknown access class")
    profile_id = value.get("profile_id")
    if (
        not isinstance(profile_id, str)
        or re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", profile_id) is None
        or not isinstance(value.get("disclose_restricted_existence"), bool)
    ):
        raise ValueError("since access_profile is malformed")
    return value


class PlaybillSinceCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-cursor-v1"] = "playbill-since-cursor-v1"
    instance_id: str
    lower_generation: int = Field(ge=0)
    head_coordinate: PlaybillAcceptedCoordinate
    access_profile: dict[str, Any]
    max_rows: int = Field(ge=1, le=1000)
    max_bytes: int = Field(ge=1, le=1_048_576)
    last_generation: int = Field(ge=1)
    last_member_path: str
    cursor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    _profile = field_validator("access_profile")(_validate_since_access_profile)

    @model_validator(mode="after")
    def _digest(self) -> "PlaybillSinceCursor":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("cursor_digest")
        if self.cursor_digest != _since_digest("playbill-since-cursor-v1", payload):
            raise ValueError("since cursor digest does not reproduce")
        return self


class PlaybillSinceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-request-v1"] = "playbill-since-request-v1"
    generation: int = Field(ge=0)
    at: PlaybillAcceptedCoordinate | None = None
    access_profile: dict[str, Any]
    max_rows: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    cursor: PlaybillSinceCursor | None = None

    _profile = field_validator("access_profile")(_validate_since_access_profile)


class PlaybillSinceRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-row-v1"] = "playbill-since-row-v1"
    generation: int = Field(ge=1)
    changeset_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    member_path: str
    artifact_kind: str
    disposition: Literal[
        "generated-successor",
        "hand-authored-successor",
        "invalidation",
        "replacement",
        "create",
        "replace",
        "retire",
        "delete",
    ]
    artifact_digest: str | None
    predecessor_artifact_digest: str | None

    @field_validator("artifact_digest", "predecessor_artifact_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 71
            or not value.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in value[7:])
        ):
            raise ValueError("since artifact digest is malformed")
        return value


class PlaybillSinceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-result-v1"] = "playbill-since-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    generation: int = Field(ge=0)
    rows: list[PlaybillSinceRow]
    next_cursor: PlaybillSinceCursor | None = None
    truncated: bool
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digest(self) -> "PlaybillSinceResult":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("result_digest")
        if self.result_digest != _since_digest("playbill-since-result-v1", payload):
            raise ValueError("since result digest does not reproduce")
        return self


class PlaybillDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-discovery-result-v1"] = "playbill-discovery-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    page: dict[str, Any]
    vocabulary_entry_count: int


class PlaybillProviderInterfaceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-provider-interface-entry-v1"]
    identity: str
    artifact_digest: str
    artifact_kind: Literal["Provider"]
    pin_role: Literal["provider"]
    interface_digest: str
    interface_basis: Literal["explicit_interface_pin", "artifact_digest_fallback"]


class PlaybillInterfaceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-interface-inventory-v1"]
    coordinate: PlaybillAcceptedCoordinate
    provider_status: Literal["installed", "not_installed"]
    interfaces: list[PlaybillProviderInterfaceEntry]


class PlaybillSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-search-result-v1"] = "playbill-search-result-v1"
    mode: Literal["search", "list", "orient"]
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    rows: list[dict[str, Any]]
    orientation: dict[str, Any] | None = None
    selection_basis_digest: str
    next_cursor: dict[str, Any] | None = None
    truncated: bool
    result_digest: str


class PlaybillContextCapsule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-context-capsule-v1"] = "playbill-context-capsule-v1"
    address: dict[str, Any]
    at: PlaybillAcceptedCoordinate
    evaluation_time: str
    canonical_summary: Any = None
    governance: Any = None
    provenance: Any = None
    attestation_coverage: Literal[
        "exact_subject",
        "containing_artifact",
        "containing_change_set",
    ]
    claim_context: Any = None
    procedure_context: Any = None
    claim_type_card: Any = None
    subject_profile: Any = None
    source_material: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[Any] = Field(default_factory=list)
    next_reads: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any]
    receipt_digest: str


class PlaybillCoverageResult(BaseModel):
    """One resolved coverage answer: the whole `playbill-coverage-result-v1`.

    ``result`` carries the frozen coverage grammar verbatim -- span results,
    cards, the one batch summary, coverage health, accepted coordinate, scope,
    manifest epoch, and the index/overlay/manifest digests the answer was
    resolved against. Coverage remains reproducible from those three digests;
    a successful outer read may additionally append a local consumption touch,
    which enters neither this answer nor accepted state.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-coverage-result-v1"] = "playbill-coverage-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    result: dict[str, Any]


class PlaybillFloorFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content_base64: str


class PlaybillFloorExport(BaseModel):
    """The deterministic greppable floor as base64 bytes keyed by floor path.

    ``manifest`` is the decoded root ``manifest.json``: it binds every file to
    the accepted coordinate it was projected from. The service is
    filesystem-free, so materializing the directory is the client's act.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-floor-export-v1", "playbill-floor-export-v2"] = (
        "playbill-floor-export-v2"
    )
    coordinate: PlaybillAcceptedCoordinate
    manifest: dict[str, Any]
    files: list[PlaybillFloorFile]


class PlaybillWorkspaceFloorWriteResult(BaseModel):
    """A verified floor export materialized by a client-side adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-floor-write-result-v1"] = (
        "playbill-workspace-floor-write-result-v1"
    )
    status: Literal["written"] = "written"
    path: str
    destination: str
    floor_digest: str
    coordinate: PlaybillAcceptedCoordinate
    file_count: int = Field(ge=1)


class PlaybillWorkspaceFloorStatus(BaseModel):
    """Freshness of the configured local floor against a daemon coordinate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-floor-status-v1"] = "playbill-workspace-floor-status-v1"
    status: Literal["not_configured", "missing", "current", "stale", "invalid"]
    path: str | None = None
    destination: str | None = None
    installed_coordinate: PlaybillAcceptedCoordinate | None = None
    current_coordinate: PlaybillAcceptedCoordinate | None = None
    message: str | None = None
