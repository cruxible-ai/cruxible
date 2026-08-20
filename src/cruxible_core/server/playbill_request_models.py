"""Strict Playbill-only HTTP request contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.claim_types import ClaimType
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import CoverageCardBudgetV1
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.discovery import DiscoveryBudgetV1, ExpansionBudgetV1
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.query.definitions import QueryDefinitionV1
from cruxible_core.playbill.query.grammar import QueryBudgetsV1
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.subjects import SubjectShell
from cruxible_core.playbill.types import OperatingProfile, PrincipalRecord
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


class PlaybillProposeClaimRequest(_StrictPlaybillRequest):
    authoring: DirectClaimAuthoringV1
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillProposeClaimsRequest(_StrictPlaybillRequest):
    authorings: tuple[DirectClaimAuthoringV1, ...]
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillProposeQueryDefinitionRequest(_StrictPlaybillRequest):
    query: QueryDefinitionV1
    proposal_name: str
    base: AcceptedCoordinate | None = None


class PlaybillClaimExplainRequest(_StrictPlaybillRequest):
    at: AcceptedCoordinate | None = None
    evaluation_time: datetime | None = None


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
