"""Pydantic wire contracts for the Playbill-only public surface."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RuntimeCredentialPermissionMode = Literal[
    "read_only",
    "governed_write",
    "graph_write",
    "admin",
]
PlaybillHostStatus = Literal["created", "already_exists"]


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
    status: Literal["accepted", "lost_cas"]
    accepted_coordinate: PlaybillAcceptedCoordinate | None


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


class PlaybillClaimProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-direct-claim-proposal-v1"] = "playbill-direct-claim-proposal-v1"
    proposal: PlaybillProposalInspection
    claim_identity: str
    claim_path: str
    statement_digest: str
    artifact_digest: str
    capture_digest: str
    capture_digests: list[str]
    observed_at: str
    existing_statements: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]


class PlaybillAuthoredClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authored-claim-v1"] = "playbill-authored-claim-v1"
    claim_identity: str
    claim_path: str
    statement_digest: str
    artifact_digest: str
    capture_digest: str
    capture_digests: list[str]
    observed_at: str
    existing_statements: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]


class PlaybillClaimBatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-direct-claim-batch-proposal-v1"] = (
        "playbill-direct-claim-batch-proposal-v1"
    )
    proposal: PlaybillProposalInspection
    claims: list[PlaybillAuthoredClaim]


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


class PlaybillAuthoringSubmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: dict[str, Any]
    status: PlaybillCandidateStatus


class PlaybillInsertionConfirmResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-insertion-confirm-result-v1"] = "playbill-insertion-confirm-result-v1"
    outcome: Literal[
        "bound",
        "already_bound",
        "backing_candidate_pending",
        "backing_candidate_refused",
        "ambiguous",
        "stale_target",
        "expired",
        "claim_currency_changed",
    ]
    intent: dict[str, Any]
    expectation: dict[str, Any]
    successor_status: PlaybillCandidateStatus | None = None


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


class PlaybillDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-discovery-result-v1"] = "playbill-discovery-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    page: dict[str, Any]
    vocabulary_entry_count: int


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
    resolved against. Resolving coverage appends no receipt: it changes no
    accepted state, and those three digests are what make it reproducible.
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
