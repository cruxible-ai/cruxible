"""Strict Playbill-only HTTP request contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.attestations import ApprovalAttestation
from cruxible_core.playbill.documents import DocumentShell
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.source_catalog import SourceCompilationBundle
from cruxible_core.playbill.types import OperatingProfile, PrincipalRecord


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
