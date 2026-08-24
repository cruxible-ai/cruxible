"""Strict Playbill-only HTTP request contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.attestations import ApprovalAttestation
from cruxible_client.contracts.authoring.inputs import AuthoringInputV1
from cruxible_client.contracts.authoring.models import (
    AuthoringPayloadV1,
    AuthoringProgramStampV1,
    AuthoringReferenceExpectationV1,
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


class PlaybillAuthoringCreateRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-create-request-v1"] = (
        "playbill-authoring-intent-create-request-v1"
    )
    payload: AuthoringPayloadV1


class PlaybillAuthoringInputCreateRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-input-create-request-v1"] = (
        "playbill-authoring-input-create-request-v1"
    )
    input: AuthoringInputV1


class PlaybillAuthoringCompileRequest(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-compile-request-v1"] = (
        "playbill-authoring-intent-compile-request-v1"
    )
    payload: AuthoringPayloadV1
    intent_id: str | None = None


class PlaybillAuthoringCreateRequestV2(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-create-request-v2"] = (
        "playbill-authoring-intent-create-request-v2"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]


class PlaybillAuthoringCompileRequestV2(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-compile-request-v2"] = (
        "playbill-authoring-intent-compile-request-v2"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    intent_id: str | None = None


class PlaybillAuthoringCreateRequestV3(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-create-request-v3"] = (
        "playbill-authoring-intent-create-request-v3"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    program_stamp: AuthoringProgramStampV1


class PlaybillAuthoringCompileRequestV3(_StrictPlaybillRequest):
    tag: Literal["playbill-authoring-intent-compile-request-v3"] = (
        "playbill-authoring-intent-compile-request-v3"
    )
    payload: AuthoringPayloadV1
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    program_stamp: AuthoringProgramStampV1
    intent_id: str | None = None


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
