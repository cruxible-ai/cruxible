"""Pydantic wire contracts for the Playbill-only public surface."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.approval_policy import ApprovalPolicyMode
from cruxible_client.contracts.authoring.inputs import AuthoringInput
from cruxible_client.contracts.authoring.models import (
    BlockSyncItem as BlockSyncItem,
)
from cruxible_client.contracts.authoring.models import (
    BlockSyncReadRequest as BlockSyncReadRequest,
)
from cruxible_client.contracts.authoring.models import (
    BlockSyncReadResult as BlockSyncReadResult,
)
from cruxible_client.contracts.authoring.models import (
    BlockSyncResult as BlockSyncResult,
)
from cruxible_client.contracts.authoring.models import (
    BlockSyncSuccessorCandidate as BlockSyncSuccessorCandidate,
)
from cruxible_client.contracts.authoring.models import (
    ProjectionCheckRequest as ProjectionCheckRequest,
)
from cruxible_client.contracts.authoring.models import (
    ProjectionCheckResult as ProjectionCheckResult,
)
from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.contracts.claims import ClaimStatementCard as ClaimStatementCard
from cruxible_client.contracts.compact_query import (
    QUERY_DEFAULT_LIMIT as QUERY_DEFAULT_LIMIT,
)
from cruxible_client.contracts.compact_query import (
    QUERY_MAX_LIMIT as QUERY_MAX_LIMIT,
)
from cruxible_client.contracts.compact_query import QueryColumn as QueryColumn
from cruxible_client.contracts.compact_query import QueryFilterContains as QueryFilterContains
from cruxible_client.contracts.compact_query import QueryFilterEq as QueryFilterEq
from cruxible_client.contracts.compact_query import QueryFilterExists as QueryFilterExists
from cruxible_client.contracts.compact_query import QueryFilterGt as QueryFilterGt
from cruxible_client.contracts.compact_query import QueryFilterGte as QueryFilterGte
from cruxible_client.contracts.compact_query import QueryFilterIn as QueryFilterIn
from cruxible_client.contracts.compact_query import QueryFilterLt as QueryFilterLt
from cruxible_client.contracts.compact_query import QueryFilterLte as QueryFilterLte
from cruxible_client.contracts.compact_query import QueryFilterNe as QueryFilterNe
from cruxible_client.contracts.compact_query import QueryFollow as QueryFollow
from cruxible_client.contracts.compact_query import QueryReceipt as QueryReceipt
from cruxible_client.contracts.compact_query import QueryRequest as QueryRequest
from cruxible_client.contracts.compact_query import QueryResultRecord as QueryResultRecord
from cruxible_client.contracts.floor import FloorDelta
from cruxible_client.contracts.line_dispatch import (
    LineArm as LineArm,
)
from cruxible_client.contracts.line_dispatch import (
    LineArmOutcome as LineArmOutcome,
)
from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipal as LineArmPrincipal,
)
from cruxible_client.contracts.line_dispatch import (
    LineArmStopReason as LineArmStopReason,
)
from cruxible_client.contracts.line_dispatch import (
    LineDispatchItem as LineDispatchItem,
)
from cruxible_client.contracts.line_dispatch import (
    LineDispatchRequest as LineDispatchRequest,
)
from cruxible_client.contracts.line_dispatch import (
    LineDispatchResult as LineDispatchResult,
)
from cruxible_client.contracts.line_dispatch import (
    LineEvaluateRequest as LineEvaluateRequest,
)
from cruxible_client.contracts.line_dispatch import (
    LineTriggerCheckRequest as LineTriggerCheckRequest,
)
from cruxible_client.contracts.line_dispatch import (
    LineTriggerCheckResult as LineTriggerCheckResult,
)
from cruxible_client.contracts.line_dispatch import (
    LineTriggerOccurrence as LineTriggerOccurrence,
)
from cruxible_client.contracts.line_dispatch import (
    LineTriggerVersion as LineTriggerVersion,
)
from cruxible_client.contracts.orient import (
    ORIENT_DEFAULT_LIMIT as ORIENT_DEFAULT_LIMIT,
)
from cruxible_client.contracts.orient import (
    ORIENT_MAX_LIMIT as ORIENT_MAX_LIMIT,
)
from cruxible_client.contracts.orient import (
    Head as Head,
)
from cruxible_client.contracts.orient import (
    OrientFloor as OrientFloor,
)
from cruxible_client.contracts.orient import (
    OrientResult as OrientResult,
)
from cruxible_client.contracts.orient import (
    OrientSection as OrientSection,
)
from cruxible_client.contracts.orient import (
    OrientSurface as OrientSurface,
)
from cruxible_client.contracts.policy_rows import PolicyInForce as PolicyInForce
from cruxible_client.contracts.policy_rows import PolicyKind as PolicyKind
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidence as ObservationSettlementEvidence,
)
from cruxible_client.contracts.predictions import (
    PredictionEqualityRule as PredictionEqualityRule,
)
from cruxible_client.contracts.predictions import (
    PredictionObservationSelector as PredictionObservationSelector,
)
from cruxible_client.contracts.predictions import (
    PredictionPresenceRule as PredictionPresenceRule,
)
from cruxible_client.contracts.predictions import (
    PredictionThresholdRule as PredictionThresholdRule,
)
from cruxible_client.contracts.predictions import (
    PredictRequest as PredictRequest,
)
from cruxible_client.contracts.predictions import PredictResult as PredictResult
from cruxible_client.contracts.predictions import (
    ResolutionContractInput as ResolutionContractInput,
)
from cruxible_client.contracts.predictions import SettleRequest as SettleRequest
from cruxible_client.contracts.predictions import SettleResult as SettleResult
from cruxible_client.contracts.predictions import (
    TerminalSettlementEvidence as TerminalSettlementEvidence,
)
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.principals import AuthoringRefusal
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifactAny as _ProcedureArtifactAny,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasurementContractStatus as ProcedureMeasurementContractStatus,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasurementEligibility as ProcedureMeasurementEligibility,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasurementRefusalCode as ProcedureMeasurementRefusalCode,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasurementResolutionSummary as ProcedureMeasurementResolutionSummary,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasurementRow as ProcedureMeasurementRow,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasureRequest as ProcedureMeasureRequest,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureMeasureResult as ProcedureMeasureResult,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureReadingsRequest as ProcedureReadingsRequest,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureReadingsResult as ProcedureReadingsResult,
)
from cruxible_client.contracts.procedures.readings import (
    ProcedureReadingSummary as ProcedureReadingSummary,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureChildInvocation,
    ProcedurePendingSuccessor,
    ProcedureRunAttribution,
    ProcedureRunAttributionWithheld,
    ProcedureRunReceipt,
    ProcedureRunReceiptV2,
    ProcedureRunReceiptV3,
    ProcedureRunReceiptV4,
    ProcedureRunReceiptV5,
    ProcedureRunReceiptWithheld,
    ProcedureSourceObservation,
    ProcedureTerminal,
    ProcedureTerminalEgress,
)
from cruxible_client.contracts.procedures.windows import (
    LineTriggerBinding,
)
from cruxible_client.contracts.procedures.windows import (
    TriggerEventReference as TriggerEventReference,
)
from cruxible_client.contracts.projection import AcceptedCoordinate as AcceptedCoordinate
from cruxible_client.contracts.provider_contracts import (
    ProviderOperationContract as _ProviderOperationContractV1,
)
from cruxible_client.contracts.provider_installation import (
    ProviderCatalog as ProviderCatalog,
)
from cruxible_client.contracts.provider_installation import (
    ProviderInstallRequest as ProviderInstallRequest,
)
from cruxible_client.contracts.provider_installation import (
    ProviderInstallResult as ProviderInstallResult,
)
from cruxible_client.contracts.provider_installation import (
    ProviderOperationReadiness as ProviderOperationReadiness,
)
from cruxible_client.contracts.provider_installation import (
    ProviderPackageSummary as ProviderPackageSummary,
)
from cruxible_client.contracts.provider_installation import (
    ProviderWheelObject as ProviderWheelObject,
)
from cruxible_client.contracts.query.results import (
    ClaimQueryResult as _ClaimQueryResultV1,
)
from cruxible_client.contracts.query.results import (
    QueryArtifactDefinition as _QueryArtifactDefinitionV2,
)
from cruxible_client.contracts.query.results import (
    QueryExecutionReceipt as _QueryExecutionReceiptV1,
)
from cruxible_client.contracts.resolution_contracts import (
    ClaimVersionReference as ClaimVersionReference,
)
from cruxible_client.contracts.resolution_contracts import (
    InvestigationBinding,
)
from cruxible_client.contracts.resolution_contracts import (
    ResolutionContract as ResolutionContract,
)
from cruxible_client.contracts.resolution_contracts import (
    ResolutionContractReference as ResolutionContractReference,
)
from cruxible_client.contracts.resolution_contracts import (
    ResolutionContractsRequest as ResolutionContractsRequest,
)
from cruxible_client.contracts.resolution_contracts import (
    ResolutionContractsResult as ResolutionContractsResult,
)
from cruxible_client.contracts.runtime_credentials import (
    RuntimeCredentialPermissionMode as RuntimeCredentialPermissionMode,
)
from cruxible_client.contracts.triggers import (
    CadenceSchedule as CadenceSchedule,
)
from cruxible_client.contracts.triggers import (
    CaptureLandingSchedule as CaptureLandingSchedule,
)
from cruxible_client.contracts.triggers import (
    CronSchedule as CronSchedule,
)
from cruxible_client.contracts.triggers import (
    Trigger as Trigger,
)
from cruxible_client.contracts.triggers import (
    WindowCloseSchedule as WindowCloseSchedule,
)
from cruxible_client.contracts.workspace_advertisement import (
    NOT_ATTACHED_ADVERTISEMENT,
    WorkspaceAdvertisement,
)
from cruxible_client.contracts.workspace_file import (
    SourceReadReceipt as SourceReadReceipt,
)
from cruxible_client.contracts.workspace_file import (
    WorkspaceFileSourceRequest as WorkspaceFileSourceRequest,
)

HostStatus = Literal["created", "already_exists", "would_create"]
HostWorkspaceRegistrationStatus = Literal["registered", "not_registered"]
AuthoringExampleName = Literal[
    "claim-existing-capture",
    "claim-flow-a",
    "claim-self-source",
    "claim-subject-relation",
    "claim-exact-content",
    "claim-revision",
    "procedure",
    "claim-adjudicate-contradicting-evidence",
    "claim-cite-supporting-evidence",
    "claim-adjudicate-unreviewed-evidence",
    "query-claims-by-type",
    "query-ontology",
    "query-procedures",
    "subject",
    "approval-policy",
    "procedure-runtime-policy",
    "procedure-mandate",
    "line",
    "trigger",
    "acquisition-policy",
    "change-set",
    "claim-type-succession",
]
NextReason: TypeAlias = Literal[
    "claim_conflicted",
    "claim_uncovered",
    "claim_stale_evidence",
    "citation_drifted",
    "citation_source_unobserved",
    "evidence_expiring",
    "floor_invalid",
    "projection_dirty",
    "projection_backing_stale",
    "claim_dependency_stale",
    "claim_attestation_threshold_met",
    "claim_contradicting_evidence_available",
    "claim_new_evidence_supporting",
    "claim_new_evidence_unreviewed",
    "document_modified",
    "workspace_binding_missing",
    "unregistered_projection_block",
    "projection_marker_invalid",
    "proposal_stale",
    "proposal_awaiting_approval",
    "mandate_expiring",
    "consumer_stalled",
    "evidence_unavailable",
    "prediction_settleable",
    "prediction_window_unbindable",
]
NextSeverity: TypeAlias = Literal["blocking", "repair", "warning"]
NextRepairOperation: TypeAlias = Literal[
    "playbill.authoring.create",
    "playbill.authoring.bind",
    "playbill.claim.retire",
    "playbill.set",
    "playbill.write",
    "playbill.floor.export",
    "playbill.block.depublish",
    "playbill.block.repin",
    "playbill.block.sync",
    "playbill.document.propose",
    "playbill.proposal.readmit",
    "playbill.proposal.approve",
    "playbill.compiler.upgrade",
    "playbill.line.arm",
    "playbill.line.dispatch",
    "playbill.settle",
    "hand_edit",
]
# The next queue's own refusals that carry a declared repair. A page cursor
# names the whole queue it continues; once that queue moves, re-reading page
# one is the repair.
NextRefusalCode: TypeAlias = Literal["playbill.next.cursor_mismatch"]
#: Rows per next page when the request names none, and the most one page carries.
NEXT_DEFAULT_LIMIT = 100
NEXT_MAX_LIMIT = 1000
#: Rows per page of the proposal, policies-in-force and curation lists when the
#: request names none, and the most one page carries. A cut page says
#: `truncated` and carries the `next_cursor` that continues it.
PROPOSAL_LIST_DEFAULT_LIMIT = 50
PROPOSAL_LIST_MAX_LIMIT = 500
CURATION_LIST_DEFAULT_LIMIT = 25
CURATION_LIST_MAX_LIMIT = 200
ProviderLaneUnavailableCode: TypeAlias = Literal[
    "provider_process_lease_invalid",
    "provider_process_lease_missing",
    "provider_process_lease_echo_failed",
    "provider_process_lease_echo_mismatch",
    "provider_process_group_survived_recovery",
    "provider_runtime_recovery_failed",
]


class GitWorkspaceNote(BaseModel):
    """Client-side advisory when CWD wins over inherited Git selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["inherited_git_workspace_ignored"]
    cwd_workspace_root: str
    inherited_workspace_root: str


class HostResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str
    status: HostStatus
    git_workspace_note: GitWorkspaceNote | None = None
    #: The host's registry row the allocation was checked against (absent, for
    #: a new host), read where it is written; commit a preview with ``at``.
    coordinate: StateCoordinate | None = None


class HostWorkspaceRegistration(BaseModel):
    """Whether one daemon host has a daemon-local workspace registration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-host-workspace-registration-v1"] = (
        "playbill-host-workspace-registration-v1"
    )
    instance_id: str
    status: HostWorkspaceRegistrationStatus
    workspace_path: str | None = None
    floor_delivery: bool = False


HostCompatibility: TypeAlias = Literal[
    "uninitialized", "writable", "reseed_required", "decommissioned", "refused"
]
HostCompatibilityReasonCode: TypeAlias = Literal[
    "legacy_layout_requires_reseed",
    "host_state_incomplete",
    "host_state_malformed",
    "compiler_lineage_not_writable",
    "instance_decommissioned",
    "location_outside_state_root",
]


class HostCompatibilityReason(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: HostCompatibilityReasonCode
    detail: str
    repair_commands: tuple[str, ...]


class HostInspection(BaseModel):
    """Credential-safe compatibility view of one governed daemon host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-host-inspection-v1"] = "playbill-host-inspection-v1"
    instance_id: str
    managed_root: str | None
    workspace_root: str | None
    floor_delivery: bool = False
    compiler_coordinate: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_revision: str | None = None
    compatibility: HostCompatibility
    writable: bool
    reason: HostCompatibilityReason | None = None

    @model_validator(mode="after")
    def _compatibility_fields_agree(self) -> HostInspection:
        if self.writable != (self.compatibility == "writable"):
            raise ValueError("writable must agree with compatibility")
        if self.compatibility == "uninitialized" and (
            self.compiler_coordinate is not None
            or self.compiler_revision is not None
            or self.reason is not None
        ):
            raise ValueError("uninitialized host cannot carry compiler or reason")
        if self.compatibility in {"reseed_required", "decommissioned", "refused"} and (
            self.reason is None
        ):
            raise ValueError(f"{self.compatibility} host must carry a typed reason")
        return self


class RuntimeCredentialBootstrapResult(BaseModel):
    #: ``would_claim`` answers a preview: the secret checked out and nothing was
    #: claimed, so no token is issued.
    status: Literal["claimed", "would_claim"]
    credential_id: str
    instance_id: str
    permission_mode: Literal["admin"]
    token: str | None = None
    #: The host's credentials the claim was checked against (none, for a
    #: claimable host), read where the claim is written.
    coordinate: StateCoordinate | None = None


class RuntimeCredentialMetadata(BaseModel):
    credential_id: str
    instance_id: str
    # The principal this credential acts as. None only for the unbound
    # operator credentials (the bootstrap claim, local recovery, and any
    # credential minted before credentials named a principal): they carry
    # transport authority but can never author.
    principal_id: str | None = None
    # A description only; it never decides who acts.
    label: str
    permission_mode: RuntimeCredentialPermissionMode
    created_at: str
    created_by: str | None = None
    revoked_at: str | None = None


class RuntimeCredentialResult(BaseModel):
    #: ``would_*`` answers a preview: nothing was minted, revoked, rotated or
    #: recovered, and no token is issued.
    status: Literal[
        "minted",
        "revoked",
        "rotated",
        "recovered",
        "would_mint",
        "would_revoke",
        "would_rotate",
        "would_recover",
    ]
    credential: RuntimeCredentialMetadata
    token: str | None = None
    #: The credential state the change was checked against (the credential
    #: itself, or for a mint or recovery the credentials it adds to), read where
    #: it writes. A commit of a revoke or rotate (which cannot be undone)
    #: passes its digest as ``at``; it exists before Cruxible is initialized.
    coordinate: StateCoordinate | None = None


class RuntimeCredentialListResult(BaseModel):
    credentials: list[RuntimeCredentialMetadata] = Field(default_factory=list)


class ProviderLaneStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-provider-lane-status-v1"] = "cruxible-provider-lane-status-v1"
    # `not_applicable` is a third answer, not a softer `unavailable`: the lane
    # is not broken, it is not part of this deployment's surface. A hosted
    # profile that cannot run Provider code at all reported `available` and then
    # refused every run, which is the one thing a health field must not do; and
    # `unavailable` would have demanded a refusal code, and every code in that
    # vocabulary describes a lane that broke.
    state: Literal["available", "unavailable", "not_applicable"]
    code: ProviderLaneUnavailableCode | None
    detail: str | None
    # Backend ids of the isolated executors this daemon registered at start, from
    # the `cruxible.isolated_executors` entry-point group. Empty is the ordinary
    # answer and the honest one: core ships no executor, so a hosted profile
    # that names a backend nothing registered can be seen to be naming nothing.
    isolated_executors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _state_matches_reason(self) -> ProviderLaneStatus:
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


class ConsumerStatus(BaseModel):
    """One daemon consumer on one instance: an armed Line, or a built-in worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-consumer-status-v1"] = "playbill-consumer-status-v1"
    instance_id: str
    kind: str
    consumer_id: str
    state: Literal["running", "lagging", "stopped", "stalled", "disabled"]
    detail: dict[str, Any] = Field(default_factory=dict)


class ServerInfoResult(BaseModel):
    server_required: bool
    state_root: str
    version: str
    instance_count: int
    auth_enabled: bool
    auth_required: bool
    provider_lane: ProviderLaneStatus
    compiler_coordinate: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_revision: str | None = None
    hosts: tuple[HostInspection, ...] = ()
    consumers: tuple[ConsumerStatus, ...] = ()


class ServerRestartResult(BaseModel):
    scheduled: bool
    version: str
    state_root: str
    # The process image that acknowledged the restart; a waiting client knows
    # the new image answers once the probe reports a different boot id.
    boot_id: str | None = None


class ServerStopResult(BaseModel):
    """Acknowledgement that this daemon will exit and release its state root."""

    scheduled: bool
    version: str
    state_root: str
    pid: int


class IsolatedExecutorRegistration(BaseModel):
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


class InitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-init-v1"] = "playbill-init-v1"
    instance_id: str
    coordinate: AcceptedCoordinate
    trust_root: dict[str, Any]
    recovery_posture: str
    approval_policy_mode: ApprovalPolicyMode
    workspace_advertisement: WorkspaceAdvertisement
    git_workspace_note: GitWorkspaceNote | None = None


class CasObjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest: str
    present: bool
    byte_length: int | None
    redacted: bool


class ProposalInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-inspection-v1"] = "playbill-proposal-inspection-v1"
    #: ``admitted``: ``proposal`` is the admitted proposal (its own evaluation
    #: says whether it passed). ``would_propose``/``would_block``: a preview that
    #: admitted nothing; ``proposal`` holds its evaluation and candidate (R12).
    status: Literal["admitted", "would_propose", "would_block"] = "admitted"
    proposal: dict[str, Any]
    #: After the call; a preview's is the head it evaluated at, which a commit
    #: passes back as ``at`` (its git oid).
    accepted_coordinate: AcceptedCoordinate
    workspace_advertisement: WorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ProposalListEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-list-entry-v1"] = "playbill-proposal-list-entry-v1"
    proposal_id: str
    actor_id: str | None
    target_ref: str | None
    admitted_at: str | None
    verdict: Literal["candidate", "refused"] | None
    candidate_digest: str | None = None
    status: Literal["open", "settled", "incomplete"]
    terminal_reason: Literal["accepted", "refused", "stale", "withdrawn"] | None = None
    incomplete_reasons: tuple[
        Literal["missing_admission", "missing_evaluation", "missing_candidate"], ...
    ] = ()
    withdrawal_present: bool = False


class ProposalList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-list-v1"] = "playbill-proposal-list-v1"
    coordinate: AcceptedCoordinate
    status_filter: Literal["open", "settled", "incomplete"] | None = None
    entries: list[ProposalListEntry]
    truncated: bool = False
    next_cursor: str | None = None


class ProposalSelectorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-selector-result-v1"] = "playbill-proposal-selector-result-v1"
    selector: str
    proposal_id: str


class ProposalReadmitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-readmit-result-v1"]
    source_proposal_id: str
    operation_digest: str
    proposal: ProposalInspection


class ProposalWithdrawResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-withdraw-result-v1"] = "playbill-proposal-withdraw-result-v1"
    #: ``would_withdraw`` answers a preview, which recorded nothing.
    status: Literal["withdrawn", "would_withdraw"] = "withdrawn"
    proposal_id: str
    actor_id: str
    reason: str
    withdrawn_at: str
    already_withdrawn: bool = False
    #: The accepted coordinate the withdrawal was checked at; ``at`` pins a commit.
    coordinate: AcceptedCoordinate | None = None


class WhoAmI(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-whoami-v1"] = "playbill-whoami-v1"
    # None only for an unbound credential, which acts as no principal.
    actor_id: str | None
    # The credential's description; never the source of the actor ID.
    credential_label: str | None
    actor_id_source: Literal[
        "runtime_credential", "unbound_credential", "principal_claim", "local_operator"
    ]
    # False when no bearer credential backs the identity: an auth-off daemon
    # trusts every process of its OS user equally, so the actor ID is a claim.
    authenticated: bool
    credential_permission_mode: Literal["read_only", "governed_write", "graph_write", "admin"]
    # None when the request names no principal (an unbound credential).
    principal_registration_status: Literal["active", "revoked", "absent"] | None
    active_principal_ids: list[str]
    coordinate: AcceptedCoordinate
    # Whether authoring create would accept this actor, and the refusal it
    # would return otherwise: the same code, detail and repair.
    can_author: bool
    authoring_refusal: AuthoringRefusal | None


class RefusalInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-refusal-v1"] = "playbill-refusal-v1"
    proposal_id: str
    verdict: Literal["candidate", "refused"]
    diagnostics: list[dict[str, Any]]


class SemanticFieldValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["absent", "present"]
    value: Any

    @model_validator(mode="after")
    def _absent_has_no_value(self) -> "SemanticFieldValue":
        if self.state == "absent" and self.value is not None:
            raise ValueError("an absent semantic field value must carry JSON null")
        return self


class SemanticFieldDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-semantic-field-delta-v1"] = "playbill-semantic-field-delta-v1"
    field_path: str
    before: SemanticFieldValue
    after: SemanticFieldValue


class ReviewedMember(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    artifact_kind: str
    disposition: str
    closure_role: Literal["authored", "generated_successor", "invalidation"]
    predecessor_artifact_digest: str | None
    candidate_artifact_digest: str | None
    base_semantic_artifact: dict[str, Any] | None
    candidate_semantic_artifact: dict[str, Any] | None
    semantic_delta: list[SemanticFieldDelta]
    law_identifier: str
    law_digest: str
    law_evidence: dict[str, Any]
    dependency_proof_refs: list[dict[str, Any]]


class ProjectionAdvisory(BaseModel):
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
    def _count(self) -> "ProjectionAdvisory":
        if self.unprojected_count != len(self.artifact_identities):
            raise ValueError("projection advisory count must match its identities")
        return self


class ProjectionEvidence(BaseModel):
    """Whether one bounded workspace projection observation informed review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-projection-evidence-v1"] = "playbill-projection-evidence-v1"
    status: Literal["used", "rejected"]
    coordinate: AcceptedCoordinate | None = None
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
    def _shape(self) -> "ProjectionEvidence":
        if self.status == "used" and (self.coordinate is None or self.reason is not None):
            raise ValueError("used projection evidence requires a coordinate and no reason")
        if self.status == "rejected" and self.reason is None:
            raise ValueError("rejected projection evidence requires a reason")
        return self


class ProposalReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-proposal-review-v1"] = "playbill-proposal-review-v1"
    coordinate_kind: Literal["provisional"] = "provisional"
    proposal_id: str
    candidate: dict[str, Any]
    candidate_digest: str
    parent_semantic_root: str
    settlement_base: AcceptedCoordinate
    base_oid: str
    complete_members: list[dict[str, Any]]
    members: list[ReviewedMember]
    governance: dict[str, Any]
    provenance: dict[str, Any]
    attestation_coverage: dict[str, Any]
    documents: list[dict[str, Any]]
    redactions: list[str]
    projection_advisory: ProjectionAdvisory | None = None
    projection_evidence: ProjectionEvidence | None = None


class ApprovalChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-challenge-v1"] = "playbill-approval-challenge-v1"
    proposal_id: str
    signer_principal: dict[str, Any]
    signer_key_history_ref: str
    statement: dict[str, Any]
    review: ProposalReview


class ApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-approval-receipt-v1"] = "playbill-approval-receipt-v1"
    proposal_id: str
    candidate_digest: str
    signer_id: str
    submitted_by: str
    signing_semantic_root: str
    attestation_digest: str
    key_history_ref: str
    git_workspace_note: GitWorkspaceNote | None = None


class ActivationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-activation-receipt-v1"] = "playbill-activation-receipt-v1"
    proposal_id: str
    activated_by: str
    status: Literal["accepted", "lost_cas"]
    accepted_coordinate: AcceptedCoordinate | None
    workspace_advertisement: WorkspaceAdvertisement


class FloorRefreshResult(BaseModel):
    """Client-owned truth about the optional workspace floor refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-floor-refresh-result-v1"] = "playbill-floor-refresh-result-v1"
    status: Literal["not_configured", "refreshed", "failed"]
    path: str | None = None
    destination: str | None = None
    floor_digest: str | None = None
    coordinate: AcceptedCoordinate | None = None
    message: str | None = None


class WorkspaceActivationResult(ActivationReceipt):
    """Activation receipt plus the independent client-workspace refresh outcome."""

    floor_refresh: FloorRefreshResult
    block_sync: BlockSyncResult | None = None


class SourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-context-v1"] = "playbill-source-context-v1"
    accepted_coordinate: AcceptedCoordinate
    documents: list[dict[str, Any]]


class SourceCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-source-check-v1"] = "playbill-source-check-v1"
    compilation_digest: str
    accepted_coordinate: AcceptedCoordinate
    alignments: list[dict[str, Any]]


class InstanceDecommissionResult(BaseModel):
    """Receipt for the terminal lifecycle state of one governed instance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-instance-decommission-result-v1"] = (
        "playbill-instance-decommission-result-v1"
    )
    #: ``would_decommission`` answers a preview, which stamped nothing; commit it
    #: with ``at`` set to this coordinate's git oid.
    status: Literal["decommissioned", "would_decommission"]
    instance_id: str
    reason: str
    decommissioned_at: str
    decommissioned_by: str
    coordinate: AcceptedCoordinate


class LedgerMirror(BaseModel):
    """Where one instance publishes its ledger, and whether that copy is current.

    `ledger set-mirror` binds a remote and waits boundedly for initial publication;
    `ledger clone-url` reads its status. A publish barrier is acknowledged when
    published_sequence reaches wait_sequence, even if newer work is pending. The
    URL carries no credential -- one that could is refused before it is stored --
    so this model is safe to print, log and hand to anyone who may read the
    instance at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-ledger-mirror-v1"] = "playbill-ledger-mirror-v1"
    instance_id: str
    mirror_url: str
    #: ``would_publish`` answers a preview: nothing was bound, requested or sent.
    status: Literal["current", "behind", "pending", "publishing", "would_publish"]
    #: A preview's accepted coordinate; binding a mirror commits only with
    #: ``at`` set to its git oid.
    coordinate: AcceptedCoordinate | None = None
    attempted_at: str | None = None
    published_main_oid: str | None = None
    requested_sequence: int = Field(default=0, ge=0)
    attempted_sequence: int = Field(default=0, ge=0)
    published_sequence: int = Field(default=0, ge=0)
    published_refs: dict[str, str] = Field(default_factory=dict)
    wait_sequence: int | None = Field(default=None, ge=0)
    detail: str | None = None


class ClaimTypeProposalLint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-proposal-lint-v1"] = "playbill-claim-type-proposal-lint-v1"
    warnings: list[dict[str, Any]]


class ClaimTypeInputProposalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-input-proposal-result-v1"] = (
        "playbill-claim-type-input-proposal-result-v1"
    )
    proposal: ProposalInspection
    lint: ClaimTypeProposalLint


class ClaimTypeMigrationResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v1"] = (
        "playbill-claim-type-migration-result-v1"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: ProposalInspection
    semantic_delta: list[SemanticFieldDelta]
    warnings: list[dict[str, Any]] = []
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-preflight-v1"] = (
        "playbill-claim-type-migration-preflight-v1"
    )
    coordinate: AcceptedCoordinate
    successor_artifact_digest: str
    dependents: list[dict[str, Any]]
    semantic_delta: list[SemanticFieldDelta]
    warnings: list[dict[str, Any]] = []
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationResultV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v2"] = (
        "playbill-claim-type-migration-result-v2"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: ProposalInspection
    semantic_delta: list[SemanticFieldDelta]
    warnings: list[dict[str, Any]] = []
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ClaimTypeMigrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-type-migration-result-v3"] = (
        "playbill-claim-type-migration-result-v3"
    )
    operation_digest: str
    dependents: list[dict[str, Any]]
    proposal: ProposalInspection
    semantic_delta: list[SemanticFieldDelta]
    warnings: list[dict[str, Any]] = []
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


ClaimTypeMigrationResponse: TypeAlias = (
    ClaimTypeMigrationResultV1
    | ClaimTypeMigrationPreflight
    | ClaimTypeMigrationResultV2
    | ClaimTypeMigrationResult
)


class CaptureEvidenceKindAdmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-capture-evidence-kind-admission-v1"]
    evidence_kind: str
    status: Literal["admitted", "not_admitted"]
    rule_id: str | None = None
    admission: Literal["origin_only", "direct", "derivational"] | None = None
    refusal_code: str | None = None
    closest_rule_id: str | None = None


class CaptureAdmissionAccount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-capture-admission-account-v1"]
    citation_id: str
    capture_digest: str
    citation_role: Literal["evidence", "copy", "legacy"]
    citation_origin: Literal["independent", "self_source", "legacy"]
    capture_contract_identity: str
    capture_contract_digest: str
    status: Literal["admitted", "not_admitted", "not_evidence"]
    decisions: list[CaptureEvidenceKindAdmission]


class ClaimViewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-claim-read-v2"]
    coordinate_kind: Literal["canonical"]
    coordinate: AcceptedCoordinate
    envelope: dict[str, Any]
    facts: list[dict[str, Any]]
    admission_evaluation_time: str
    admission_accounts: list[CaptureAdmissionAccount]
    statement: ClaimStatementCard


class CandidateStatusRecord(BaseModel):
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
    current_accepted_coordinate: AcceptedCoordinate
    path_to_acceptance: list[dict[str, Any]] = Field(default_factory=list)
    accepted_generation: AcceptedCoordinate | None = None


class AuthoringIntentViewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-intent-view-v1"] = "playbill-authoring-intent-view-v1"
    intent: dict[str, Any]


class AuthoringExampleResult(BaseModel):
    """One model-constructed, executable authoring input example."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-example-result-v1"] = "playbill-authoring-example-result-v1"
    name: AuthoringExampleName
    payload: AuthoringInput
    #: A line to read beside the payload, such as that cron is evaluated in UTC.
    note: str | None = None


class AuthoringIntentListRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-intent-list-v1"] = "playbill-authoring-intent-list-v1"
    intents: list[dict[str, Any]] = Field(default_factory=list)


class AuthoringPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-preflight-result-v1"] = (
        "playbill-authoring-preflight-result-v1"
    )
    verdict: Literal["passed", "refused"]
    certificate: dict[str, Any]
    frontier: dict[str, Any]
    lint: ClaimTypeProposalLint | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class AuthoringSubmitResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-authoring-submit-result-v1"] = "playbill-authoring-submit-result-v1"
    intent: dict[str, Any]
    status: CandidateStatusRecord
    workspace_advertisement: WorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT
    # True when this submit amends an existing Claim identity in place.
    identity_stable: bool = False
    claim_revision: int | None = None
    # One row per submitted member, so a changeset answers the same two
    # questions once per member instead of once for the whole submission.
    members: tuple[dict[str, Any], ...] = ()
    # The preflight this submit ran, when the request compiled and submitted in
    # one call; a refused verdict carries the diagnostics compile would have.
    preflight: AuthoringPreflightResult | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class InsertionAbandonResultRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-insertion-abandon-result-v1"] = "playbill-insertion-abandon-result-v1"
    intent: dict[str, Any]
    expectation: dict[str, Any]


class BlockDeclareResult(BaseModel):
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
    coordinate: AcceptedCoordinate


class BlockDepublishResult(BaseModel):
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
    #: ``would_depublish`` answers a preview, which released nothing.
    outcome: Literal["depublished", "already_depublished", "would_depublish"]
    claim_identity: str | None = None
    coordinate: AcceptedCoordinate

    @model_validator(mode="after")
    def _origin_shape(self) -> "BlockDepublishResult":
        publication = (self.intent_id, self.expectation_id, self.claim_identity)
        if self.origin == "publication":
            if any(value is None for value in publication):
                raise ValueError("a released publication names its intent, expectation and Claim")
        elif any(value is not None for value in publication):
            raise ValueError("a released declaration names no intent, expectation or Claim")
        return self


class QueryDefinitionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-definition-read-v1"] = "playbill-query-definition-read-v1"
    coordinate: AcceptedCoordinate
    path: str
    name: str
    identity: str
    artifact_digest: str
    envelope: dict[str, Any]


class QueryRun(BaseModel):
    """One executed query: its replayable result beside its execution receipt.

    ``receipt`` carries the whole ``playbill-query-execution-receipt-v1``; its
    ``result_digest`` is the receipt's content identity, and
    ``journal_record_digest`` is present only when the caller owned a journal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-query-run-v1"] = "playbill-query-run-v1"
    coordinate: AcceptedCoordinate
    name: str
    definition_path: str
    definition_digest: str
    result: _ClaimQueryResultV1
    receipt: _QueryExecutionReceiptV1
    journal_record_digest: str | None = None

    @property
    def completed(self) -> bool:
        return self.result.verdict == "completed"

    @property
    def truncated(self) -> bool:
        return self.result.truncation.truncated

    @property
    def has_conflicts(self) -> bool:
        return bool(self.result.conflicts or any(row.conflicts for row in self.result.rows))

    @property
    def artifact_definitions(self) -> tuple[_QueryArtifactDefinitionV2, ...]:
        """Typed definitions; refuse to present a partial read as a complete listing."""
        if self.result.result_shape != "artifact_definition":
            raise ValueError("this is a Claim query, not an artifact definition query")
        if not self.completed or self.truncated:
            raise ValueError("definition listing is refused or truncated")
        return tuple(row.artifact for row in self.result.rows if row.artifact is not None)


class ProcedureReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-readiness-result-v1"] = (
        "playbill-procedure-readiness-result-v1"
    )
    coordinate: AcceptedCoordinate
    evaluation_time: str
    procedure_identity: dict[str, Any]
    procedure_artifact_digest: str
    definition_digest: str
    artifact: _ProcedureArtifactAny | None = None
    state: Literal["ready", "binding_required", "unsupported"]
    required_slots: list[str]
    unsupported_nodes: list[dict[str, Any]]
    next_operation: dict[str, Any]


class PolicyInForceList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-policy-in-force-list-v1"] = "playbill-policy-in-force-list-v1"
    coordinate: AcceptedCoordinate
    policies: list[PolicyInForce]
    truncated: bool = False
    next_cursor: str | None = None


class ProcedureBindResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-bind-result-v2"] = "playbill-procedure-bind-result-v2"
    accepted_digest: str
    accepted_readiness: ProcedureReadiness
    pending: "ProcedurePendingSuccessor | None" = None
    workspace_advertisement: WorkspaceAdvertisement = NOT_ATTACHED_ADVERTISEMENT


class ProcedureRunState(BaseModel):
    investigation: InvestigationBinding | None = None
    trigger_binding: LineTriggerBinding | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-procedure-run-state-v2"] = "playbill-procedure-run-state-v2"
    run_id: str | None
    procedure_identity: dict[str, Any]
    procedure_artifact_digest: str
    bound_coordinate: AcceptedCoordinate
    head_at_admission: AcceptedCoordinate
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
    #: Withheld (actor left out) from a reader who may not see the run's
    #: arming credential, as on the run card.
    attribution: (
        Annotated[
            ProcedureRunAttribution | ProcedureRunAttributionWithheld,
            Field(discriminator="tag"),
        ]
        | None
    ) = None
    semantic_replay_key_digest: str | None = None
    semantic_result_digest: str | None = None
    receipt: (
        ProcedureRunReceiptV2
        | ProcedureRunReceiptV3
        | ProcedureRunReceiptV4
        | ProcedureRunReceiptV5
        | ProcedureRunReceipt
        | ProcedureRunReceiptWithheld
        | None
    ) = None
    receipt_digest: str | None = None
    terminal: ProcedureTerminal | None = None
    children: list[ProcedureChildInvocation] = Field(default_factory=list)
    source_observations: list[ProcedureSourceObservation] = Field(default_factory=list)
    terminal_egress: list[ProcedureTerminalEgress] = Field(default_factory=list)

    @property
    def coordinate(self) -> AcceptedCoordinate:
        return self.bound_coordinate


class NextRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: NextRepairOperation
    target: str
    required_change: str
    arguments: Any = Field(default_factory=dict)
    # Composed by the daemon from the fields beside it; absent on a hand edit
    # and on an operation whose arguments do not name every operand.
    command: str | None = None


class NextRepairRequirement(BaseModel):
    """What running a withheld repair needs that this caller does not have.

    The row stays in the queue; only its repair is withheld. ``because`` names
    the gate: the permission ``tier`` the ``tool`` runs at, the MCP tool
    ``profile`` that advertises it, and/or ``authoring`` when this caller cannot
    author here at all; ``authoring_refusal`` then carries whoami's code,
    detail and repair.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-repair-requirement-v1"] = "playbill-next-repair-requirement-v1"
    operation: NextRepairOperation
    tool: str
    tier: Literal["read_only", "governed_write", "graph_write", "admin"]
    profile: Literal["full"] | None = None
    because: list[Literal["tier", "profile", "authoring"]]
    authoring_refusal: AuthoringRefusal | None = None


class NextFinding(BaseModel):
    """One more finding about the same underlying fact as the row that carries it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-finding-v1"] = "playbill-next-finding-v1"
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: list[str] = Field(default_factory=list)
    detail: Any = Field(default_factory=dict)
    # None when this caller cannot run it; `repair_requires` then says why.
    repair: NextRepair | None
    repair_requires: NextRepairRequirement | None = None


class NextItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-item-v1"] = "playbill-next-item-v1"
    item_id: str
    severity: NextSeverity
    reason: NextReason
    subject_identity: str
    related_identities: list[str] = Field(default_factory=list)
    detail: Any = Field(default_factory=dict)
    # None when this caller's surface, tool profile or tier cannot run it: the
    # row stays, and `repair_requires` says what running it needs.
    repair: NextRepair | None
    findings: list[NextFinding] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    repair_requires: NextRepairRequirement | None = None

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class NextHealth(BaseModel):
    """One environment facet: its state, what it saw, and the repair if it needs one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-health-v1"] = "playbill-next-health-v1"
    state: str
    detail: Any = Field(default_factory=dict)
    repair: NextRepair | None = None
    # The facet needs a repair this caller cannot perform, so it was dropped.
    repair_hidden: bool = False
    repair_requires: NextRepairRequirement | None = None


class NextStatus(BaseModel):
    """The environment the queue was read in, beside the work rather than in it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-status-v1"] = "playbill-next-status-v1"
    blocking: bool
    instance: NextHealth
    floor: NextHealth
    ledger_mirror: NextHealth
    provider_lane: NextHealth
    procedure_catalog: NextHealth
    compiler: NextHealth
    line_dispatch: NextHealth
    consumers: NextHealth
    #: Whether a live Trigger schedules every internal action.
    triggers: NextHealth
    #: Rows parked by a current ``unsure`` attestation. No row is left out for
    #: the caller: one whose repair it cannot run keeps `repair_requires`.
    held: int = Field(default=0, ge=0)


class NextResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-next-result-v1", "playbill-next-result-v2"] = "playbill-next-result-v1"
    coordinate: AcceptedCoordinate
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
    # The environment the queue was read in: instance, floor, ledger mirror,
    # provider lane and Procedure catalog health, beside the work items.
    status: NextStatus
    items: list[NextItem]
    # Every row the whole answer carries -- the queue, or on a delta its
    # changed rows -- of which `items` is one page. `result_digest` names the
    # whole queue on every page; `next_cursor` continues this answer.
    total_items: int = Field(ge=0)
    next_cursor: str | None = None
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
    def _attestation_coordinate(self) -> "NextResult":
        if (self.tag == "playbill-next-result-v2") != (self.attestation_head_digest is not None):
            raise ValueError("Next v2 alone requires an attestation evidence head")
        if self.attestation_head_digest is not None:
            Sha256Value.from_tagged(self.attestation_head_digest)
        if self.removed_item_ids and (
            self.tag != "playbill-next-result-v2" or not self.delta_since
        ):
            raise ValueError("removed next item IDs are valid only on a v2 delta")
        if len(self.items) > self.total_items:
            raise ValueError("a next page cannot carry more rows than its answer")
        if self.next_cursor is not None and len(self.items) == self.total_items:
            raise ValueError("a next answer carried whole has no further page")
        carried_ids = {item.item_id for item in self.items}
        if not set(self.removed_item_ids).issubset(carried_ids):
            raise ValueError("removed next item IDs must name carried delta rows")
        return self


class CurationListResult(BaseModel):
    """G9 curation queue plus request-bound observation accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-curation-list-result-v1"] = "playbill-curation-list-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    evaluation_time: str
    operational_head_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    items: list[dict[str, Any]] = Field(default_factory=list)
    detector_coverage: list[dict[str, Any]]
    observation_coverage: dict[str, Any]
    truncated: bool = False
    next_cursor: str | None = None
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class CurationActionResult(BaseModel):
    """One attributed append-only curation lifecycle transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-curation-action-result-v1"] = "playbill-curation-action-result-v1"
    #: ``would_record`` answers a preview, which appended nothing; ``item`` is
    #: then the item as it stands.
    status: Literal["recorded", "would_record"] = "recorded"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    operational_head_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    item: dict[str, Any]


class AuditFactors(BaseModel):
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


class AuditEvidenceRef(BaseModel):
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


class AuditRow(BaseModel):
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
    factors: AuditFactors
    rank_score: int = Field(ge=1)
    evidence_refs: list[AuditEvidenceRef]


class AuditScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-scope-v1"] = "playbill-audit-scope-v1"
    claim_type_identities: list[str] = Field(default_factory=list)
    subject_kinds: list[str] = Field(default_factory=list)


class AuditCoveredClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_identity: dict[str, Any]
    artifact_digest: str


class AuditCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-coverage-v1"] = "playbill-audit-coverage-v1"
    access_permitted: bool
    declared_scope: AuditScope
    covered_claims: list[AuditCoveredClaim]
    candidate_claim_count: int = Field(ge=0)
    returned_claim_count: int = Field(ge=0)
    omitted_claim_count: int = Field(ge=0)
    omission_reasons: list[Literal["byte_budget_exceeded", "row_budget_exceeded"]]


class AuditCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-cursor-v1"] = "playbill-audit-cursor-v1"
    coordinate: AcceptedCoordinate
    evaluation_time: str
    operational_input_head_digest: str
    scope_digest: str
    next_offset: int = Field(ge=1)
    cursor_digest: str


class AuditResult(BaseModel):
    """Read-only ranked Claim patrol plus completed-run coverage accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-audit-result-v1"] = "playbill-audit-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    evaluation_time: str
    operational_input_head_digest: str
    audited_through_generation: int | None = Field(default=None, ge=0)
    rows: list[AuditRow]
    coverage: AuditCoverage
    next_cursor: AuditCursor | None = None
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
        raise ValueError("since access_profile is not a CoverageAccessProfile")
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


class SinceCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-cursor-v1"] = "playbill-since-cursor-v1"
    instance_id: str
    lower_generation: int = Field(ge=0)
    head_coordinate: AcceptedCoordinate
    access_profile: dict[str, Any]
    max_rows: int = Field(ge=1, le=1000)
    max_bytes: int = Field(ge=1, le=1_048_576)
    last_generation: int = Field(ge=1)
    last_member_path: str
    cursor_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    _profile = field_validator("access_profile")(_validate_since_access_profile)

    @model_validator(mode="after")
    def _digest(self) -> "SinceCursor":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("cursor_digest")
        if self.cursor_digest != _since_digest("playbill-since-cursor-v1", payload):
            raise ValueError("since cursor digest does not reproduce")
        return self


class SinceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-request-v1"] = "playbill-since-request-v1"
    generation: int = Field(ge=0)
    at: AcceptedCoordinate | None = None
    access_profile: dict[str, Any]
    max_rows: int = Field(default=100, ge=1, le=1000)
    max_bytes: int = Field(default=65_536, ge=1, le=1_048_576)
    cursor: SinceCursor | None = None

    _profile = field_validator("access_profile")(_validate_since_access_profile)


class SinceRow(BaseModel):
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


class SinceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-since-result-v1"] = "playbill-since-result-v1"
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    rows: list[SinceRow]
    next_cursor: SinceCursor | None = None
    truncated: bool
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digest(self) -> "SinceResult":
        payload = self.model_dump(mode="json")
        payload.pop("tag")
        payload.pop("result_digest")
        if self.result_digest != _since_digest("playbill-since-result-v1", payload):
            raise ValueError("since result digest does not reproduce")
        return self


class ProviderInterfaceImplementation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-provider-interface-implementation-v1"] = (
        "playbill-provider-interface-implementation-v1"
    )
    provider_identity: str
    provider_artifact_digest: str
    implementation_digest: str


class ProviderInterfaceEntry(BaseModel):
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
    providers: list[ProviderInterfaceImplementation] = Field(default_factory=list)
    operation_contract: _ProviderOperationContractV1 | None = None


class CoverageResult(BaseModel):
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
    coordinate: AcceptedCoordinate
    result: dict[str, Any]


class FloorFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    content_base64: str


FloorExportPart = Literal["discovery"]
"""An opt-in part of a v5 floor export.

``discovery`` adds the discovery cards (``subjects/``, ``claim-types/``,
``procedures/`` and ``coverage-manifest.json``) to the grep-first floor.
"""


class FloorExport(BaseModel):
    """The deterministic greppable floor as base64 bytes keyed by floor path.

    ``manifest`` is the decoded root ``manifest.json``: it binds every file to
    the accepted coordinate it was projected from. The service is
    filesystem-free, so materializing the directory is the client's act.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal[
        "playbill-floor-export-v1", "playbill-floor-export-v2", "playbill-floor-export-v5"
    ] = "playbill-floor-export-v2"
    coordinate: AcceptedCoordinate
    manifest: dict[str, Any]
    files: list[FloorFile]


class WorkspaceFloorWriteResult(BaseModel):
    """A verified floor export materialized by the workspace writer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-floor-write-result-v1"] = (
        "playbill-workspace-floor-write-result-v1"
    )
    # `unchanged`: the directory already held exactly this floor.
    status: Literal["written", "unchanged"] = "written"
    path: str
    destination: str
    floor_digest: str
    coordinate: AcceptedCoordinate
    file_count: int = Field(ge=1)
    git_workspace_note: GitWorkspaceNote | None = None


class FloorDeliveryResult(BaseModel):
    """The same floor delta and write receipt returned by a daemon delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-floor-delivery-result-v1"] = "playbill-floor-delivery-result-v1"
    delta: FloorDelta
    written: WorkspaceFloorWriteResult
    export: FloorExport | None = None


class FloorConsumerOutcome(BaseModel):
    """One floor refresh at an accepted head, including a typed stalled outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    tag: Literal["playbill-floor-consumer-outcome-v1"] = "playbill-floor-consumer-outcome-v1"
    generation: int = Field(ge=0)
    status: Literal["written", "unchanged", "failed"]
    file_count: int = Field(default=0, ge=0)
    error: str | None = None


class FloorDeliverNowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    include: tuple[FloorExportPart, ...] = ()
    at: AcceptedCoordinate | None = None


class FloorDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool


class WorkspaceAttachResult(BaseModel):
    """Client-owned result of binding local config to an existing daemon host."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-attach-result-v1"] = "playbill-workspace-attach-result-v1"
    instance_id: str
    workspace_root: str
    config_path: str
    transport: str
    git_workspace_note: GitWorkspaceNote | None = None


class WorkspaceDetachResult(BaseModel):
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
    #: ``would_detach`` answers a preview, which released nothing.
    status: Literal["detached", "not_registered", "would_detach"]
    workspace_root: str | None = None
    #: The host's worktree binding this was checked against; commit a preview
    #: with ``at`` set to its digest.
    coordinate: StateCoordinate | None = None


class HostWorkspaceAttachResult(BaseModel):
    """One daemon host attached to a Git worktree, before or after its init.

    An initialized host attaches when the worktree is in the ledger's own Git
    object format and holds no part of the host's managed root; nothing is
    rebuilt. ``would_attach`` answers a preview, which registered nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-host-workspace-attach-result-v1"] = (
        "playbill-host-workspace-attach-result-v1"
    )
    instance_id: str
    status: Literal["attached", "already_attached", "would_attach"]
    workspace_root: str
    #: Whether Cruxible is already initialized under the host.
    initialized: bool
    #: The host's worktree binding this was checked against, read where the
    #: attach writes it; commit a preview with ``at`` set to its digest.
    coordinate: StateCoordinate | None = None


class WorkspaceFloorStatus(BaseModel):
    """Freshness of the configured local floor against a daemon coordinate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-workspace-floor-status-v1"] = "playbill-workspace-floor-status-v1"
    status: Literal["not_configured", "missing", "current", "stale", "invalid"]
    path: str | None = None
    destination: str | None = None
    installed_coordinate: AcceptedCoordinate | None = None
    current_coordinate: AcceptedCoordinate | None = None
    message: str | None = None
