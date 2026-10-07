"""Strict HTTP request contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts import (
    CURATION_LIST_DEFAULT_LIMIT,
    CURATION_LIST_MAX_LIMIT,
    NEXT_DEFAULT_LIMIT,
    NEXT_MAX_LIMIT,
    FloorExportPart,
)
from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.inputs import AuthoringInput
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentCompileRequest,
    AuthoringIntentCompileRequestV1,
    AuthoringIntentCompileRequestV2,
)
from cruxible_client.contracts.authoring.models import (
    InsertionAbandonRequest as InsertionAbandonRequest,
)
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampAny,
    ReviewWorkspaceObservation,
)
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.ledger_mirror import MIRROR_URL_MAX_LENGTH
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.types import (
    DECOMMISSION_REASON_MAX_LENGTH,
    CompilerCoordinate,
    GitObjectFormat,
    OperatingProfile,
    PrincipalRecord,
    validate_decommission_prose,
)
from cruxible_core.claims.claim_type_inputs import ClaimTypeInputRecord
from cruxible_core.coverage.adapter import WorkingSourceObservation
from cruxible_core.coverage.contracts import CoverageCardBudget
from cruxible_core.coverage.indexes import CoverageScanBudget
from cruxible_core.curation.curation_calibration import (
    AUDIT_BUDGET_DEFAULT_MAX_BYTES,
    AUDIT_BUDGET_DEFAULT_MAX_ROWS,
)
from cruxible_core.indexes.projection import AcceptedCoordinate


class _StrictPlaybillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The route module retains its existing local names, but these are aliases to
# the canonical client-owned wire models rather than parallel definitions.
PlaybillAuthoringCompileRequest = AuthoringIntentCompileRequestV1
PlaybillAuthoringCompileRequestV2 = AuthoringIntentCompileRequestV2
PlaybillAuthoringCompileRequestV3 = AuthoringIntentCompileRequest


class InitRequest(_StrictPlaybillRequest):
    principals: tuple[PrincipalRecord, ...]
    operating_profile: OperatingProfile = "local"
    require_independent_approval: bool = False
    workspace_root: str | None = None
    # None inherits an attached workspace's format, else the SHA-1 default. An
    # explicit value that contradicts the workspace refuses before any state is
    # written.
    git_object_format: GitObjectFormat | None = None
    # Optional at bootstrap: an instance
    # that publishes nowhere is a complete instance, and `ledger set-mirror`
    # binds one later without rebuilding anything.
    mirror_url: str | None = Field(default=None, max_length=MIRROR_URL_MAX_LENGTH)


class LedgerMirrorRequest(_StrictPlaybillRequest):
    """The remote this ledger publishes to. Never a URL carrying a credential."""

    url: str = Field(min_length=1, max_length=MIRROR_URL_MAX_LENGTH)
    #: Publishing to a new remote cannot be called back: previews by default.
    dry_run: DryRun = None
    at: PreviewAt = None


class LedgerPublishRequest(_StrictPlaybillRequest):
    """Wait at most this many seconds for the configured mirror to acknowledge."""

    timeout: float = Field(default=60.0, ge=0.0, le=60.0, allow_inf_nan=False, strict=True)
    dry_run: DryRun = None
    at: PreviewAt = None


class InstanceDecommissionRequest(_StrictPlaybillRequest):
    """The operator's stated reason for ending this instance's governed writes."""

    reason: str = Field(min_length=1, max_length=DECOMMISSION_REASON_MAX_LENGTH)
    #: Decommissioning cannot be undone: previews by default, commits with ``at``.
    dry_run: DryRun = None
    at: PreviewAt = None

    @field_validator("reason")
    @classmethod
    def _prose(cls, value: str) -> str:
        # The bound alone is not the record's constraint: a control character
        # passes it here and then fails strict validation inside the write,
        # where the ValidationError is an untyped 500 rather than a refusal the
        # caller can read. The same function decides at both layers.
        return validate_decommission_prose(value)


class StoreBodyRequest(_StrictPlaybillRequest):
    content_base64: str


class ProposeDocumentRequest(_StrictPlaybillRequest):
    shell: DocumentShell
    proposal_name: str
    source_compilation_digest: str | None = None
    base: AcceptedCoordinate | None = None
    dry_run: DryRun = None
    at: PreviewAt = None


class CompilerUpgradeRequest(_StrictPlaybillRequest):
    target: CompilerCoordinate
    base: AcceptedCoordinate
    proposal_name: str
    dry_run: DryRun = None
    at: PreviewAt = None


class ProposePrincipalRequest(_StrictPlaybillRequest):
    principal: PrincipalRecord
    proposal_name: str
    base: AcceptedCoordinate | None = None
    dry_run: DryRun = None
    at: PreviewAt = None


class ApprovalRequest(_StrictPlaybillRequest):
    attestation: ApprovalAttestation


class ReviewRequest(_StrictPlaybillRequest):
    include_body: bool = False
    workspace_observation: ReviewWorkspaceObservation | None = None


class ApprovalChallengeRequest(_StrictPlaybillRequest):
    signer_id: str
    include_body: bool = False


class PlaybillExplainRequest(_StrictPlaybillRequest):
    subject: SemanticAddress
    at: AcceptedCoordinate
    detail: Literal["summary", "evidence", "proof"] = "summary"
    include_body: bool = False


class SourceBundleRequest(_StrictPlaybillRequest):
    bundle: SourceCompilationBundle


class SourceProposeRequest(SourceBundleRequest):
    source_name: str
    proposal_name: str
    dry_run: DryRun = None
    at: PreviewAt = None


class ProposeClaimTypeRequest(_StrictPlaybillRequest):
    claim_type: ClaimType
    proposal_name: str
    base: AcceptedCoordinate | None = None
    dry_run: DryRun = None
    at: PreviewAt = None


class ProposeClaimTypeInputRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-claim-type-input-propose-request-v1"] = (
        "playbill-claim-type-input-propose-request-v1"
    )
    input: ClaimTypeInputRecord
    proposal_name: str
    dry_run: DryRun = None
    at: PreviewAt = None


class AuthoringInputCompileRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-input-compile-request-v1"] = (
        "playbill-authoring-input-compile-request-v1"
    )
    input: AuthoringInput
    intent_id: str | None = None


class AuthoringPreflightRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-preflight-request-v1"] = (
        "playbill-authoring-intent-preflight-request-v1"
    )


class AuthoringRebaseRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-rebase-request-v1"] = (
        "playbill-authoring-intent-rebase-request-v1"
    )


class AuthoringSubmitRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-submit-request-v1"] = (
        "playbill-authoring-intent-submit-request-v1"
    )


class BlockDeclareRequest(_StrictPlaybillRequest):
    """The stamp a workspace just wrote, offered to the instance for registration."""

    stamp: ProjectionBlockStampAny


class BlockDepublishRequest(_StrictPlaybillRequest):
    """The page block whose publication registration is being released."""

    source_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    dry_run: DryRun = None
    at: PreviewAt = None


class ProposalReadmitRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-proposal-readmit-request-v1"] = "playbill-proposal-readmit-request-v1"
    dry_run: DryRun = None
    at: PreviewAt = None


class ProposalWithdrawRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-proposal-withdraw-request-v1"] = "playbill-proposal-withdraw-request-v1"
    reason: str = Field(min_length=1, max_length=1_000)
    dry_run: DryRun = None
    at: PreviewAt = None


class NextRequestV1(_StrictPlaybillRequest):
    tag: Literal["playbill-next-request-v1"] = "playbill-next-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: dict[str, Any]
    expiring_within: dict[str, Any] | None = None
    workspace_observation: dict[str, Any] | None = None
    since_result_digest: str | None = None
    limit: int = Field(default=NEXT_DEFAULT_LIMIT, ge=1, le=NEXT_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=2048)
    # Who reads the queue: the surface a repair renders for, and the tools an
    # MCP session advertises. The daemon supplies the caller's tier itself.
    caller_surface: Literal["cli", "mcp", "sdk"] | None = None
    caller_tools: tuple[str, ...] | None = None


class NextRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-next-request-v2"] = "playbill-next-request-v2"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: dict[str, Any]
    expiring_within: dict[str, Any] | None = None
    workspace_observation: dict[str, Any] | None = None
    since_result_digest: str | None = None
    limit: int = Field(default=NEXT_DEFAULT_LIMIT, ge=1, le=NEXT_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=2048)
    at_attestation_head_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    caller_surface: Literal["cli", "mcp", "sdk"] | None = None
    caller_tools: tuple[str, ...] | None = None


class CurationListRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-list-request-v1"] = "playbill-curation-list-request-v1"
    evaluation_time: datetime
    access_profile: dict[str, Any]
    workspace_observation: dict[str, Any] | None = None
    limit: int = Field(default=CURATION_LIST_DEFAULT_LIMIT, ge=1, le=CURATION_LIST_MAX_LIMIT)
    cursor: str | None = Field(default=None, max_length=4096)


class AuditRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-audit-request-v1"] = "playbill-audit-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: dict[str, Any]
    scope: dict[str, Any] = Field(
        default_factory=lambda: {
            "tag": "playbill-audit-scope-v1",
            "claim_type_identities": [],
            "subject_kinds": [],
        }
    )
    budget: dict[str, Any] = Field(
        default_factory=lambda: {
            "tag": "playbill-audit-budget-v1",
            "max_rows": AUDIT_BUDGET_DEFAULT_MAX_ROWS,
            "max_bytes": AUDIT_BUDGET_DEFAULT_MAX_BYTES,
        }
    )
    cursor: dict[str, Any] | None = None


class CurationOverruleRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-overrule-request-v1"] = "playbill-curation-overrule-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None


class CurationAcceptFixedRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-accept-fixed-request-v1"] = (
        "playbill-curation-accept-fixed-request-v1"
    )
    item_id: str
    expected_latest_event_digest: str
    reason: str
    accepted_proposal_id: str
    accepted_changeset_digest: str
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None


class CurationSuppressRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-suppress-request-v1"] = "playbill-curation-suppress-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str
    scope: Literal["item", "pattern", "instance"]
    until_generation: int | None = None
    attribution_refs: tuple[str, ...] = ()
    dry_run: DryRun = None
    at: PreviewAt = None


class ResolveCoverageRequest(_StrictPlaybillRequest):
    """The vendor-neutral coverage request (§11.7).

    Observations, never paths: the caller binds each working path to a declared
    logical source and hashes the bytes it read, and only the resulting
    observation crosses the wire. The daemon reads no client filesystem, and no
    access profile is accepted here -- a request may not widen its own
    disclosure.
    """

    at: AcceptedCoordinate | None = None
    observations: tuple[WorkingSourceObservation, ...]
    budget: CoverageCardBudget | None = None
    scan_budget: CoverageScanBudget | None = None


class FloorDeltaRequest(_StrictPlaybillRequest):
    """Ask for what brings a floor at ``base_generation`` to ``at`` (default: head).

    ``base_generation`` and ``base_renderer`` come from the client's own floor
    manifest; with either absent the answer is the whole floor.
    """

    at: AcceptedCoordinate | None = None
    base_generation: int | None = Field(default=None, ge=0)
    base_renderer: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class FloorExportRequest(_StrictPlaybillRequest):
    at: AcceptedCoordinate | None = None
    format_version: Literal[2, 5] = 5
    # Opt-in parts of a v5 floor; "discovery" adds the discovery cards.
    include: tuple[FloorExportPart, ...] = ()
    review_notes_oid: str | None = None
