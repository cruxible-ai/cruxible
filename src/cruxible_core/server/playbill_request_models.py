"""Strict Playbill-only HTTP request contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1
from cruxible_client.contracts.authoring.models import (
    AuthoringIntentCompileRequestV1,
    AuthoringIntentCompileRequestV2,
    AuthoringIntentCompileRequestV3,
    AuthoringIntentCreateRequestV1,
    AuthoringIntentCreateRequestV2,
    AuthoringIntentCreateRequestV3,
    InsertionConfirmationObservationV1,
)
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.discovery import DiscoveryBudgetV1, ExpansionBudgetV1
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.query.grammar import QueryBudgetsV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_catalog import SourceCompilationBundle
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.types import OperatingProfile, PrincipalRecord
from cruxible_core.playbill.claim_type_inputs import ClaimTypeInputV1
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.search import (
    SEARCH_KINDS,
    PlaybillSearchBudgetsV1,
    PlaybillSearchCursorV1,
    SearchKind,
    SearchMode,
    SearchStatus,
)
from cruxible_core.service.playbill_claims import DirectClaimAuthoringV1


class _StrictPlaybillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


# The route module retains its existing local names, but these are aliases to
# the canonical client-owned wire models rather than parallel definitions.
PlaybillAuthoringCreateRequest = AuthoringIntentCreateRequestV1
PlaybillAuthoringCreateRequestV2 = AuthoringIntentCreateRequestV2
PlaybillAuthoringCreateRequestV3 = AuthoringIntentCreateRequestV3
PlaybillAuthoringCompileRequest = AuthoringIntentCompileRequestV1
PlaybillAuthoringCompileRequestV2 = AuthoringIntentCompileRequestV2
PlaybillAuthoringCompileRequestV3 = AuthoringIntentCompileRequestV3


class PlaybillInitRequest(_StrictPlaybillRequest):
    principals: tuple[PrincipalRecord, ...]
    operating_profile: OperatingProfile = "local"


class PlaybillStoreBodyRequest(_StrictPlaybillRequest):
    content_base64: str


class PlaybillProposeDocumentRequest(_StrictPlaybillRequest):
    shell: DocumentShell
    proposal_name: str
    source_compilation_digest: str | None = None
    base: AcceptedCoordinate | None = None


class PlaybillProposePrincipalRequest(_StrictPlaybillRequest):
    principal: PrincipalRecord
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillApprovalRequest(_StrictPlaybillRequest):
    attestation: ApprovalAttestation


class PlaybillReviewRequest(_StrictPlaybillRequest):
    include_body: bool = False


class PlaybillApprovalChallengeRequest(_StrictPlaybillRequest):
    signer_id: str
    include_body: bool = False


class PlaybillExplainRequest(_StrictPlaybillRequest):
    subject: SemanticAddress
    at: AcceptedCoordinate
    detail: Literal["summary", "evidence", "proof"] = "summary"
    include_body: bool = False


class PlaybillSourceBundleRequest(_StrictPlaybillRequest):
    bundle: SourceCompilationBundle


class PlaybillSourceProposeRequest(PlaybillSourceBundleRequest):
    source_name: str
    proposal_name: str


class PlaybillProposeSubjectRequest(_StrictPlaybillRequest):
    shell: SubjectShell
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillProposeClaimTypeRequest(_StrictPlaybillRequest):
    claim_type: ClaimType
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillProposeClaimTypeInputRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-claim-type-input-propose-request-v1"] = (
        "playbill-claim-type-input-propose-request-v1"
    )
    input: ClaimTypeInputV1
    proposal_name: str


class PlaybillProposeClaimRequest(_StrictPlaybillRequest):
    authoring: DirectClaimAuthoringV1
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillProposeClaimsRequest(_StrictPlaybillRequest):
    authorings: tuple[DirectClaimAuthoringV1, ...]
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillAuthoringInputCreateRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-input-create-request-v1"] = (
        "playbill-authoring-input-create-request-v1"
    )
    input: AuthoringInputV1


class PlaybillAuthoringInputCompileRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-input-compile-request-v1"] = (
        "playbill-authoring-input-compile-request-v1"
    )
    input: AuthoringInputV1
    intent_id: str | None = None


class PlaybillAuthoringPreflightRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-preflight-request-v1"] = (
        "playbill-authoring-intent-preflight-request-v1"
    )


class PlaybillAuthoringRebaseRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-rebase-request-v1"] = (
        "playbill-authoring-intent-rebase-request-v1"
    )


class PlaybillAuthoringSubmitRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-submit-request-v1"] = (
        "playbill-authoring-intent-submit-request-v1"
    )


class PlaybillInsertionConfirmRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-insertion-confirm-request-v1"] = "playbill-insertion-confirm-request-v1"
    observation: InsertionConfirmationObservationV1


class PlaybillInsertionAbandonRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-insertion-abandon-request-v1"] = "playbill-insertion-abandon-request-v1"


class PlaybillProposeQueryDefinitionRequest(_StrictPlaybillRequest):
    query: QueryDefinitionV1
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillClaimExplainRequest(_StrictPlaybillRequest):
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None


class PlaybillProposalReadmitRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-proposal-readmit-request-v1"] = "playbill-proposal-readmit-request-v1"


class PlaybillRunQueryRequest(_StrictPlaybillRequest):
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None
    parameters: dict[str, Any] | None = None
    budgets: QueryBudgetsV1 | None = None


class PlaybillDiscoverRequest(_StrictPlaybillRequest):
    query: str | None = None
    entrypoint: str | None = None
    at: AcceptedCoordinate | None = None
    evaluation_time: str | None = None
    profile: Literal["interfaces", "subjects", "all"] = "interfaces"
    budget: DiscoveryBudgetV1 = DiscoveryBudgetV1()


class PlaybillSearchRequest(_StrictPlaybillRequest):
    mode: SearchMode
    query: str | None = None
    kinds: tuple[SearchKind, ...] = SEARCH_KINDS
    subject: SemanticAddress | None = None
    statuses: tuple[SearchStatus, ...] = ()
    cursor: PlaybillSearchCursorV1 | None = None
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None
    budgets: PlaybillSearchBudgetsV1 = PlaybillSearchBudgetsV1()


class PlaybillNextRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-next-request-v1"] = "playbill-next-request-v1"
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime
    access_profile: dict[str, Any]
    expiring_within: dict[str, Any] | None = None
    workspace_observation: dict[str, Any] | None = None


class PlaybillCurationListRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-list-request-v1"] = "playbill-curation-list-request-v1"
    evaluation_time: datetime
    access_profile: dict[str, Any]
    workspace_observation: dict[str, Any] | None = None


class PlaybillAuditRequest(_StrictPlaybillRequest):
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
            "max_rows": 100,
            "max_bytes": 65_536,
        }
    )
    cursor: dict[str, Any] | None = None


class PlaybillCurationOverruleRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-overrule-request-v1"] = "playbill-curation-overrule-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str
    attribution_refs: tuple[str, ...] = ()


class PlaybillCurationAcceptFixedRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-accept-fixed-request-v1"] = (
        "playbill-curation-accept-fixed-request-v1"
    )
    item_id: str
    expected_latest_event_digest: str
    reason: str
    accepted_proposal_id: str
    accepted_changeset_digest: str
    attribution_refs: tuple[str, ...] = ()


class PlaybillCurationSuppressRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-curation-suppress-request-v1"] = "playbill-curation-suppress-request-v1"
    item_id: str
    expected_latest_event_digest: str
    reason: str
    scope: Literal["item", "pattern", "instance"]
    until_generation: int | None = None
    attribution_refs: tuple[str, ...] = ()


class PlaybillExpandRequest(_StrictPlaybillRequest):
    address: SemanticAddress
    at: AcceptedCoordinate | None = None
    evaluation_time: str | None = None
    facets: tuple[str, ...] = ()
    budget: ExpansionBudgetV1 = ExpansionBudgetV1()


class PlaybillResolveCoverageRequest(_StrictPlaybillRequest):
    """The vendor-neutral coverage request (§11.7).

    Observations, never paths: the caller binds each working path to a declared
    logical source and hashes the bytes it read, and only the resulting
    observation crosses the wire. The daemon reads no client filesystem, and no
    access profile is accepted here -- a request may not widen its own
    disclosure.
    """

    at: AcceptedCoordinate | None = None
    observations: tuple[WorkingSourceObservationV1, ...]
    budget: CoverageCardBudgetV1 | None = None
    scan_budget: CoverageScanBudgetV1 | None = None


class PlaybillFloorExportRequest(_StrictPlaybillRequest):
    at: AcceptedCoordinate | None = None
