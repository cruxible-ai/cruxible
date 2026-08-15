"""Strict request contracts for the surviving daemon host surface."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client import contracts
from cruxible_core.server.playbill_request_models import (  # noqa: F401
    PlaybillApprovalChallengeRequest,
    PlaybillApprovalRequest,
    PlaybillExplainRequest,
    PlaybillInitRequest,
    PlaybillProposeDocumentRequest,
    PlaybillProposePrincipalRequest,
    PlaybillReviewRequest,
    PlaybillSourceBundleRequest,
    PlaybillSourceProposeRequest,
    PlaybillStoreBodyRequest,
)


class _StrictHostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaybillHostCreateRequest(_StrictHostRequest):
    instance_id: str | None = None


class BootstrapClaimRequest(_StrictHostRequest):
    bootstrap_secret: str = Field(min_length=1)


class RuntimeCredentialCreateRequest(_StrictHostRequest):
    label: str = Field(min_length=1)
    permission_mode: contracts.RuntimeCredentialPermissionMode = "admin"
