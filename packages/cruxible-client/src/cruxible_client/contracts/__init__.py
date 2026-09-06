"""Pydantic wire contracts for the Playbill-only public surface."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.approval_policy import ApprovalPolicyMode
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncItemV1 as PlaybillBlockSyncItemV1,
)
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadRequestV1 as PlaybillBlockSyncReadRequestV1,
)
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncReadResultV1 as PlaybillBlockSyncReadResultV1,
)
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncResultV1 as PlaybillBlockSyncResultV1,
)
from cruxible_client.contracts.authoring.models import (
    PlaybillBlockSyncSuccessorCandidateV1 as PlaybillBlockSyncSuccessorCandidateV1,
)
from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.claims import ClaimStatementCardV1 as ClaimStatementCardV1
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV1 as ObservationSettlementEvidenceV1,
)
from cruxible_client.contracts.predictions import (
    PlaybillPredictionDeclarationV1 as PlaybillPredictionDeclarationV1,
)
from cruxible_client.contracts.predictions import (
    PlaybillPredictRequestV1 as PlaybillPredictRequestV1,
)
from cruxible_client.contracts.predictions import PlaybillPredictResultV1 as PlaybillPredictResultV1
from cruxible_client.contracts.predictions import PlaybillSettleRequestV1 as PlaybillSettleRequestV1
from cruxible_client.contracts.predictions import PlaybillSettleResultV1 as PlaybillSettleResultV1
from cruxible_client.contracts.predictions import (
    PredictionEqualityRuleV1 as PredictionEqualityRuleV1,
)
from cruxible_client.contracts.predictions import (
    PredictionObservationSelectorV1 as PredictionObservationSelectorV1,
)
from cruxible_client.contracts.predictions import (
    PredictionPresenceRuleV1 as PredictionPresenceRuleV1,
)
from cruxible_client.contracts.predictions import (
    PredictionThresholdRuleV1 as PredictionThresholdRuleV1,
)
from cruxible_client.contracts.predictions import (
    TerminalSettlementEvidenceV1 as TerminalSettlementEvidenceV1,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedures.results import (
    ProcedurePendingSuccessorV1,
    ProcedureRunAttributionV1,
    ProcedureRunReceiptV2,
    ProcedureRunReceiptV3,
    ProcedureRunReceiptV4,
    ProcedureRunReceiptV5,
    ProcedureRunReceiptV6,
    ProcedureTerminalV1,
)
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    PlaybillWorkspaceAdvertisement,
)
from cruxible_client.contracts.workspace_file import (
    SourceReadReceiptV1 as SourceReadReceiptV1,
)
from cruxible_client.contracts.workspace_file import (
    WorkspaceFileSourceRequestV1 as WorkspaceFileSourceRequestV1,
)

RuntimeCredentialPermissionMode = Literal[
    "read_only",
    "governed_write",
    "graph_write",
    "admin",
]
PlaybillHostStatus = Literal["created", "already_exists"]
PlaybillHostWorkspaceRegistrationStatus = Literal["registered", "not_registered"]
PlaybillAuthoringExampleName = Literal[
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "claim-subject-relation",
    "claim-exact-content",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
    "query-claims-by-type",
    "subject",
    "approval-policy",
    "procedure-runtime-policy",
    "procedure-mandate",
    "change-set",
    "claim-type-succession",
]
PlaybillPolicyKind: TypeAlias = Literal[
    "approval_policy",
    "procedure_runtime_policy",
    "source_acquisition_policy",
    "claim_evidence_admission_policy",
    "claim_admission_policy",
    "claim_resolution_policy",
    "claim_evidence_freshness_policy",
    "claim_attestation_consequence_policy",
    "capture_retention_erasure_policy",
    "query_evaluation_policy",
    "document_activation_policy",
    "procedure_activation_policy",
    "line_trigger_policy",
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
    "projection_candidates_changed",
    "claim_dependency_stale",
    "claim_attestation_threshold_met",
    "claim_contradicting_evidence_available",
    "claim_new_evidence_supporting",
    "claim_new_evidence_unreviewed",
    "document_modified",
    "claim_cites_retired",
    "retired_claim_source_stale",
    "unregistered_projection_block",
    "projection_marker_invalid",
    "provider_lane_unavailable",
    "procedure_projection_missing",
    "instance_decommissioned",
    "ledger_mirror_behind",
]
PlaybillHandEditNextReason: TypeAlias = Literal[
    "procedure_projection_missing",
    "provider_lane_unavailable",
    "instance_decommissioned",
    # The daemon has already retried by construction: it pushes after every
    # write, so a mirror that is still behind is behind for a reason no verb
    # can clear -- a remote that moved, a credential that expired, a network
    # that is down. What repairs it is off this host.
    "ledger_mirror_behind",
]
PLAYBILL_HAND_EDIT_NEXT_REASONS: frozenset[PlaybillHandEditNextReason] = frozenset(
    {
        "procedure_projection_missing",
        "provider_lane_unavailable",
        "instance_decommissioned",
        "ledger_mirror_behind",
    }
)

ProviderLaneUnavailableCodeV1: TypeAlias = Literal[
    "provider_process_lease_invalid",
    "provider_process_lease_missing",
    "provider_process_lease_echo_failed",
    "provider_process_lease_echo_mismatch",
    "provider_process_group_survived_recovery",
    "provider_runtime_recovery_failed",
]


class GitWorkspaceNoteV1(BaseModel):
    """Client-side advisory when CWD wins over inherited Git selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["inherited_git_workspace_ignored"]
    cwd_workspace_root: str
    inherited_workspace_root: str


class PlaybillHostResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: PlaybillHostStatus
    git_workspace_note: GitWorkspaceNoteV1 | None = None


class PlaybillHostWorkspaceRegistrationV1(BaseModel):
    """Whether one daemon host has a daemon-local workspace registration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-host-workspace-registration-v1"] = (
        "playbill-host-workspace-registration-v1"
    )
    instance_id: str
    status: PlaybillHostWorkspaceRegistrationStatus
    workspace_path: str | None = None


PlaybillHostCompatibilityV1: TypeAlias = Literal["uninitialized", "writable", "reseed_required"]
PlaybillHostCompatibilityReasonCodeV1: TypeAlias = Literal[
    "legacy_layout_requires_reseed",
    "host_state_incomplete",
    "host_state_malformed",
    "compiler_lineage_not_writable",
]


class PlaybillHostCompatibilityReasonV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PlaybillHostCompatibilityReasonCodeV1
    detail: str
    repair_commands: tuple[str, ...]


class PlaybillHostInspectionV1(BaseModel):
    """Credential-safe compatibility view of one governed daemon host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-host-inspection-v1"] = "playbill-host-inspection-v1"
    instance_id: str
    managed_root: str | None
    workspace_root: str | None
    compiler_coordinate: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_revision: str | None = None
    compatibility: PlaybillHostCompatibilityV1
    writable: bool
    reason: PlaybillHostCompatibilityReasonV1 | None = None

    @model_validator(mode="after")
    def _compatibility_fields_agree(self) -> PlaybillHostInspectionV1:
        if self.writable != (self.compatibility == "writable"):
            raise ValueError("writable must agree with compatibility")
        if self.compatibility == "uninitialized" and (
            self.compiler_coordinate is not None
            or self.compiler_revision is not None
            or self.reason is not None
        ):
            raise ValueError("uninitialized host cannot carry compiler or reason")
        if self.compatibility == "reseed_required" and self.reason is None:
            raise ValueError("reseed_required host must carry a typed reason")
        return self


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


class ProviderLaneStatusV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-provider-lane-status-v1"] = "cruxible-provider-lane-status-v1"
    # `not_applicable` is a third answer, not a softer `unavailable`: the lane
    # is not broken, it is not part of this deployment's surface. A hosted
    # profile that cannot run Provider code at all reported `available` and then
    # refused every run, which is the one thing a health field must not do; and
    # `unavailable` would have demanded a refusal code, and every code in that
    # vocabulary describes a lane that broke.
    state: Literal["available", "unavailable", "not_applicable"]
    code: ProviderLaneUnavailableCodeV1 | None
    detail: str | None
    # Backend ids of the isolated executors this daemon registered at start, from
    # the `cruxible.isolated_executors` entry-point group. Empty is the ordinary
    # answer and the honest one: core ships no executor, so a hosted profile
    # that names a backend nothing registered can be seen to be naming nothing.
    isolated_executors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _state_matches_reason(self) -> ProviderLaneStatusV1:
        if self.state != "unavailable" and self.code is not None:
            raise ValueError("only an unavailable Provider lane carries a refusal code")
        if self.state == "unavailable" and (self.code is None or self.detail is None):
            raise ValueError("unavailable Provider lane requires a typed code and detail")
        if self.state == "not_applicable" and self.detail is None:
            raise ValueError("an inapplicable Provider lane must say why it does not apply")
        if self.isolated_executors != tuple(
            sorted(set(self.isolated_executors), key=lambda item: item.encode("utf-8"))
        ):
            raise ValueError("Provider lane isolated executor ids must be sorted and unique")
        return self


class ServerInfoResult(BaseModel):
    server_required: bool
    state_root: str
    version: str
    instance_count: int
    auth_enabled: bool
    auth_required: bool
    provider_lane: ProviderLaneStatusV1
    compiler_coordinate: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_revision: str | None = None
    hosts: tuple[PlaybillHostInspectionV1, ...] = ()


class ServerRestartResult(BaseModel):
    scheduled: bool
    version: str
    state_root: str


class ServerStopResult(BaseModel):
    """Acknowledgement that this daemon will exit and release its state root."""

    scheduled: bool
    version: str
    state_root: str
    pid: int


class IsolatedExecutorRegistrationV1(BaseModel):
    """What a runtime must publish to be a REGISTERED isolated executor.

    A shared hosted profile executes Provider code only through an executor
    that is registered in the running build. Core registers none, so the record
    exists here as the seam an out-of-tree executor registers through, and so
    the thing being claimed is a pinned artifact rather than an environment
    string: the backend id selects it, the implementation digest says exactly
    which bytes are isolating, and the capabilities say what that isolation
    covers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["isolated-executor-registration-v1"] = "isolated-executor-registration-v1"
    backend_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    implementation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capabilities: tuple[str, ...] = ()


class PlaybillAcceptedCoordinate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-accepted-coordinate-v1"] = "playbill-accepted-coordinate-v1"
    git_oid: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    semantic_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generation_root: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


#: The one repair an ``unseeded`` Provider seed row names: configure the daemon-local
#: ``seed_materializations`` entry, then run the ordinary ``playbill provider seed`` verb.
ProviderSeedRepairV1 = Literal["configure_seed_materializations_then_playbill_provider_seed"]


class PlaybillProviderSeedResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-provider-seed-result-v1"] = "playbill-provider-seed-result-v1"
    provider_id: str
    materialization_source: Literal["local", "registry"]
    status: Literal[
        "already_current",
        "pending",
        "proposed",
        "activated",
        "lost_cas",
        "unseeded",
    ]
    changed_paths: tuple[str, ...]
    proposal_id: str | None = Field(default=None, exclude_if=lambda value: value is None)
    candidate_digest: str | None = Field(default=None, exclude_if=lambda value: value is None)
    approval_required: bool
    repair: ProviderSeedRepairV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    accepted_coordinate: PlaybillAcceptedCoordinate

    @model_validator(mode="after")
    def _only_an_unseeded_row_names_a_repair(self) -> PlaybillProviderSeedResultV1:
        if (self.status == "unseeded") != (self.repair is not None):
            raise ValueError("exactly an unseeded Provider seed row carries its repair")
        if self.status == "unseeded" and (
            self.changed_paths
            or self.proposal_id is not None
            or self.candidate_digest is not None
            or self.approval_required
        ):
            raise ValueError("an unseeded Provider seed row carries no proposal and no change")
        return self


class PlaybillInitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-init-v1"] = "playbill-init-v1"
    instance_id: str
    coordinate: PlaybillAcceptedCoordinate
    trust_root: dict[str, Any]
    recovery_posture: str
    approval_policy_mode: ApprovalPolicyMode
    workspace_advertisement: PlaybillWorkspaceAdvertisement
    git_workspace_note: GitWorkspaceNoteV1 | None = None
    provider_seed: PlaybillProviderSeedResultV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


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
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
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
    terminal_reason: Literal["accepted", "refused", "stale", "withdrawn"] | None = None


class PlaybillProposalList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-list-v1"] = "playbill-proposal-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    status_filter: Literal["open", "settled"] | None = None
    entries: list[PlaybillProposalListEntry]


class PlaybillProposalSelectorResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-selector-result-v1"] = "playbill-proposal-selector-result-v1"
    selector: str
    proposal_id: str


class PlaybillProposalReadmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-readmit-result-v1"]
    source_proposal_id: str
    operation_digest: str
    proposal: PlaybillProposalInspection


class PlaybillProposalWithdrawResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-withdraw-result-v1"] = "playbill-proposal-withdraw-result-v1"
    proposal_id: str
    actor_id: str
    reason: str
    withdrawn_at: str
    already_withdrawn: bool = False


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


class PlaybillSemanticFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["absent", "present"]
    value: Any

    @model_validator(mode="after")
    def _absent_has_no_value(self) -> "PlaybillSemanticFieldValue":
        if self.state == "absent" and self.value is not None:
            raise ValueError("an absent semantic field value must carry JSON null")
        return self


class PlaybillSemanticFieldDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-semantic-field-delta-v1"] = "playbill-semantic-field-delta-v1"
    field_path: str
    before: PlaybillSemanticFieldValue
    after: PlaybillSemanticFieldValue


class PlaybillReviewedMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    artifact_kind: str
    disposition: str
    closure_role: Literal["authored", "generated_successor", "invalidation"]
    predecessor_artifact_digest: str | None
    candidate_artifact_digest: str | None
    base_semantic_artifact: dict[str, Any] | None
    candidate_semantic_artifact: dict[str, Any] | None
    semantic_delta: list[PlaybillSemanticFieldDelta]
    law_identifier: str
    law_digest: str
    law_evidence: dict[str, Any]
    dependency_proof_refs: list[dict[str, Any]]


class PlaybillProjectionAdvisory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-projection-advisory-v1"] = "playbill-projection-advisory-v1"
    unprojected_count: int = Field(ge=1)
    artifact_identities: list[str]
    message: str

    @field_validator("artifact_identities")
    @classmethod
    def _identities(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value), key=lambda item: item.encode("utf-8")):
            raise ValueError("projection advisory identities must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _count(self) -> "PlaybillProjectionAdvisory":
        if self.unprojected_count != len(self.artifact_identities):
            raise ValueError("projection advisory count must match its identities")
        return self


class PlaybillProjectionEvidence(BaseModel):
    """Whether one bounded workspace projection observation informed review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-projection-evidence-v1"] = "playbill-projection-evidence-v1"
    status: Literal["used", "rejected"]
    coordinate: PlaybillAcceptedCoordinate | None = None
    reason: (
        Literal[
            "observation_invalid",
            "presentation_policy_invalid",
            "coverage_missing",
            "coordinate_not_accepted",
            "coordinate_before_settlement_base",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _shape(self) -> "PlaybillProjectionEvidence":
        if self.status == "used" and (self.coordinate is None or self.reason is not None):
            raise ValueError("used projection evidence requires a coordinate and no reason")
        if self.status == "rejected" and self.reason is None:
            raise ValueError("rejected projection evidence requires a reason")
        return self


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
    members: list[PlaybillReviewedMember]
    governance: dict[str, Any]
    provenance: dict[str, Any]
    attestation_coverage: dict[str, Any]
    documents: list[dict[str, Any]]
    redactions: list[str]
    projection_advisory: PlaybillProjectionAdvisory | None = None
    projection_evidence: PlaybillProjectionEvidence | None = None


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
    git_workspace_note: GitWorkspaceNoteV1 | None = None


class PlaybillActivationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-activation-receipt-v1"] = "playbill-activation-receipt-v1"
    proposal_id: str
    activated_by: str
    status: Literal["accepted", "lost_cas"]
    accepted_coordinate: PlaybillAcceptedCoordinate | None
    workspace_advertisement: PlaybillWorkspaceAdvertisement


class PlaybillFloorRefreshResult(BaseModel):
    """Client-owned truth about the optional workspace floor refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-floor-refresh-result-v1"] = "playbill-floor-refresh-result-v1"
    status: Literal["not_configured", "refreshed", "failed"]
    path: str | None = None
    destination: str | None = None
    floor_digest: str | None = None
    coordinate: PlaybillAcceptedCoordinate | None = None
    message: str | None = None


class PlaybillWorkspaceActivationResult(PlaybillActivationReceipt):
    """Activation receipt plus the independent client-workspace refresh outcome."""

    floor_refresh: PlaybillFloorRefreshResult
    block_sync: PlaybillBlockSyncResultV1 | None = None


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


class PlaybillInstanceDecommissionResultV1(BaseModel):
    """Receipt for the terminal lifecycle state of one governed instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-instance-decommission-result-v1"] = (
        "playbill-instance-decommission-result-v1"
    )
    instance_id: str
    reason: str
    decommissioned_at: str
    decommissioned_by: str
    coordinate: PlaybillAcceptedCoordinate


class PlaybillLedgerMirrorV1(BaseModel):
    """Where one instance publishes its ledger, and whether that copy is current.

    One model for both doors: `ledger set-mirror` binds a remote and publishes
    to it at once, and `ledger clone-url` reads back what a reviewer clones. The
    URL carries no credential -- one that could is refused before it is stored --
    so this model is safe to print, log and hand to anyone who may read the
    instance at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-ledger-mirror-v1"] = "playbill-ledger-mirror-v1"
    instance_id: str
    mirror_url: str
    status: Literal["current", "behind"]
    attempted_at: str | None = None
    detail: str | None = None


class PlaybillSubjectIncomingClaimV1(BaseModel):
    """One live Claim whose subject-valued object is the profiled Subject."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-incoming-claim-v1"] = "playbill-subject-incoming-claim-v1"
    claim_identity: str
    subject_identity: str


class PlaybillSubjectIncomingGroupV1(BaseModel):
    """Every incoming edge that arrives on one governed predicate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-incoming-group-v1"] = "playbill-subject-incoming-group-v1"
    predicate: str
    claims: list[PlaybillSubjectIncomingClaimV1]


class PlaybillSubjectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-subject-read-v1"] = "playbill-subject-read-v1"
    coordinate_kind: Literal["canonical"] = "canonical"
    coordinate: PlaybillAcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]
    incoming: list[PlaybillSubjectIncomingGroupV1] = []


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
    semantic_delta: list[PlaybillSemanticFieldDelta]
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
    semantic_delta: list[PlaybillSemanticFieldDelta]
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
    semantic_delta: list[PlaybillSemanticFieldDelta]
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
    semantic_delta: list[PlaybillSemanticFieldDelta]
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
    citation_origin: Literal["independent", "self_source", "legacy"]
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
    statement: ClaimStatementCardV1


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
    reason: Literal["was-rescinded", "was-wrong", "superseded"]
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
    payload: AuthoringInputV1


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
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
    # True when this submit amends an existing Claim identity in place.
    identity_stable: bool = False
    claim_revision: int | None = None
    # One row per submitted member, so a changeset answers the same two
    # questions once per member instead of once for the whole submission.
    members: tuple[dict[str, Any], ...] = ()


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
    inserted_block_base64: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
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


class PlaybillBlockDeclareResultV1(BaseModel):
    """One projection block registered with the instance that governs its page.

    A block declared with `block repin` was known only to the bytes in the page:
    `next` could ask whether a marker was sanctioned for one declaration road
    and answered it by a string prefix, and `workspace detach` could not refuse
    on a block it had never heard of. The declaration is protocol state, not
    accepted state -- it records that this instance stands behind this marker,
    not what the marker says.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-block-declare-result-v1"] = "playbill-block-declare-result-v1"
    source_id: str
    block_id: str
    outcome: Literal["declared", "redeclared"]
    declared_generation: int = Field(ge=0)
    coordinate: PlaybillAcceptedCoordinate


class PlaybillBlockDepublishResultV1(BaseModel):
    """One published block released from the registration that demanded it.

    A publication registration was terminal at `bound`: publish once, and that
    page carried that block, with that id, forever. `next` demanded the frame
    back for a block a later ruling had deleted, and the repair it named was to
    restore it. This is the transition out, addressed the way the page names it
    -- a source and a block -- rather than by the intent id nobody keeps.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-block-depublish-result-v1"] = "playbill-block-depublish-result-v1"
    source_id: str
    block_id: str
    # A block declared with `block repin` has no intent, no expectation and no
    # publishing Claim -- it is prose held to a list. Those three fields name a
    # publication and are absent for a declaration, which `origin` says.
    origin: Literal["publication", "declaration"] = "publication"
    intent_id: str | None = None
    expectation_id: str | None = None
    outcome: Literal["depublished", "already_depublished"]
    claim_identity: str | None = None
    coordinate: PlaybillAcceptedCoordinate

    @model_validator(mode="after")
    def _origin_shape(self) -> "PlaybillBlockDepublishResultV1":
        publication = (self.intent_id, self.expectation_id, self.claim_identity)
        if self.origin == "publication":
            if any(value is None for value in publication):
                raise ValueError("a released publication names its intent, expectation and Claim")
        elif any(value is not None for value in publication):
            raise ValueError("a released declaration names no intent, expectation or Claim")
        return self


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
    definition_digest: str
    state: Literal["ready", "binding_required", "unsupported"]
    required_slots: list[str]
    unsupported_nodes: list[dict[str, Any]]
    next_operation: dict[str, Any]


class PlaybillPolicyInForce(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-policy-in-force-v1"] = "playbill-policy-in-force-v1"
    placement: Literal["embedded", "standalone"]
    policy_kind: PlaybillPolicyKind
    declaring_artifact_identity: str
    declaring_artifact_kind: str
    declaring_artifact_digest: str
    path: str
    field_path: str
    policy: dict[str, Any]


class PlaybillPolicyInForceList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-policy-in-force-list-v1"] = "playbill-policy-in-force-list-v1"
    coordinate: PlaybillAcceptedCoordinate
    policies: list[PlaybillPolicyInForce]


class PlaybillProcedureBindResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-bind-result-v2"] = "playbill-procedure-bind-result-v2"
    accepted_digest: str
    accepted_readiness: PlaybillProcedureReadiness
    pending: "ProcedurePendingSuccessorV1 | None" = None
    workspace_advertisement: PlaybillWorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT


class PlaybillProcedureRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-run-state-v2"] = "playbill-procedure-run-state-v2"
    run_id: str | None
    procedure_identity: dict[str, Any]
    procedure_artifact_digest: str
    bound_coordinate: PlaybillAcceptedCoordinate
    head_at_admission: PlaybillAcceptedCoordinate
    lane: Literal["current", "replay"]
    evaluation_time: str
    status: Literal[
        "running",
        "succeeded",
        "admission_refused",
        "node_refused",
        "operational_failed",
        "internal_failed",
        "halted",
    ]
    pending_inputs: list[str]
    outcomes: list[dict[str, Any]]
    next_operation: dict[str, Any]
    result: Any = None
    attribution: ProcedureRunAttributionV1 | None = None
    semantic_replay_key_digest: str | None = None
    semantic_result_digest: str | None = None
    receipt: (
        ProcedureRunReceiptV2
        | ProcedureRunReceiptV3
        | ProcedureRunReceiptV4
        | ProcedureRunReceiptV5
        | ProcedureRunReceiptV6
        | None
    ) = None
    receipt_digest: str | None = None
    terminal: ProcedureTerminalV1 | None = None

    @property
    def coordinate(self) -> PlaybillAcceptedCoordinate:
        return self.bound_coordinate


class PlaybillNextResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-result-v1", "playbill-next-result-v2"] = "playbill-next-result-v1"
    coordinate: PlaybillAcceptedCoordinate
    evaluation_time: str
    observed_domains: list[
        Literal[
            "accepted_state",
            "workspace_floor",
            "workspace_sources",
            "workspace_projections",
        ]
    ]
    unobserved_domains: list[
        Literal[
            "accepted_state",
            "workspace_floor",
            "workspace_sources",
            "workspace_projections",
        ]
    ]
    items: list[dict[str, Any]]
    result_digest: str
    # Set only on a delta. Items are the changed rows while result_digest names
    # the complete current queue, so callers may echo it as the next cursor.
    delta_since: str | None = None
    attestation_head_digest: str | None = None
    removed_item_ids: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )

    @field_validator("removed_item_ids")
    @classmethod
    def _removed_item_ids(cls, value: list[str]) -> list[str]:
        for item_id in value:
            Sha256Value.from_tagged(item_id)
        if value != sorted(set(value), key=lambda item: item.encode("ascii")):
            raise ValueError("removed next item IDs must be ASCII byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _attestation_coordinate(self) -> "PlaybillNextResult":
        if (self.tag == "playbill-next-result-v2") != (self.attestation_head_digest is not None):
            raise ValueError("Next v2 alone requires an attestation evidence head")
        if self.attestation_head_digest is not None:
            Sha256Value.from_tagged(self.attestation_head_digest)
        if self.removed_item_ids and (
            self.tag != "playbill-next-result-v2" or not self.delta_since
        ):
            raise ValueError("removed next item IDs are valid only on a v2 delta")
        carried_ids = {
            item.get("item_id") for item in self.items if isinstance(item.get("item_id"), str)
        }
        if not set(self.removed_item_ids).issubset(carried_ids):
            raise ValueError("removed next item IDs must name carried delta rows")
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
    artifact_kind: Literal["ProviderInterface"]
    pin_role: Literal["provider-interface"]
    interface_digest: str
    vocabulary_digest: str
    classifier_digest: str
    effect_class: Literal["none", "external_read", "external_mutation"]
    classifier_status: Literal["installed", "not_installed"]
    interface_basis: Literal["accepted_registration"]


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
    git_workspace_note: GitWorkspaceNoteV1 | None = None


class PlaybillWorkspaceAttachResultV1(BaseModel):
    """Client-owned result of binding local config to an existing daemon host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-attach-result-v1"] = "playbill-workspace-attach-result-v1"
    instance_id: str
    workspace_root: str
    config_path: str
    transport: str
    git_workspace_note: GitWorkspaceNoteV1 | None = None


class PlaybillWorkspaceDetachResultV1(BaseModel):
    """One daemon host released from the Git worktree it was attached to.

    The registry exclusivity is a UNIQUE index on (backend, workspace_root), so
    a worktree can only ever be one host's. Moving one between hosts had no
    verb: the refusal named "archive/rebuild that host", which is not a verb
    either, and the rollback that does exactly this was reachable only from an
    initialization failure.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-detach-result-v1"] = "playbill-workspace-detach-result-v1"
    instance_id: str
    status: Literal["detached", "not_registered"]
    workspace_root: str | None = None


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
