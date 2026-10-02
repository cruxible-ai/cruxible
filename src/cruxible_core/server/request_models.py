"""Strict request contracts for the surviving daemon host surface."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client import contracts
from cruxible_client.contracts.change_control import DryRun, PreviewAt
from cruxible_client.contracts.runtime_credentials import RuntimeCredentialPrincipalProofV1
from cruxible_core.server.playbill_request_models import (  # noqa: F401
    PlaybillApprovalChallengeRequest,
    PlaybillApprovalRequest,
    PlaybillCompilerUpgradeRequest,
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
    workspace_root: str | None = None
    dry_run: DryRun = None
    at: PreviewAt = None


class PlaybillHostWorkspaceAttachRequest(_StrictHostRequest):
    workspace_root: str = Field(min_length=1)
    dry_run: DryRun = None
    at: PreviewAt = None


class BootstrapClaimRequest(_StrictHostRequest):
    bootstrap_secret: str = Field(min_length=1)
    dry_run: DryRun = None
    at: PreviewAt = None


class RuntimeCredentialCreateRequest(_StrictHostRequest):
    # The principal the credential acts as; minting needs its authority.
    principal_id: str = Field(min_length=1, max_length=128)
    permission_mode: contracts.RuntimeCredentialPermissionMode
    # A description only (default: the principal ID); it never decides who acts.
    label: str | None = Field(default=None, min_length=1, max_length=256)
    principal_proof: RuntimeCredentialPrincipalProofV1 | None = None
    dry_run: DryRun = None
    at: PreviewAt = None


class RuntimeCredentialRotateRequest(_StrictHostRequest):
    # The bound principal's signed consent to the replacement's exact terms;
    # needed unless the request already acts as that principal.
    principal_proof: RuntimeCredentialPrincipalProofV1 | None = None
    #: Rotating revokes the old token, which cannot be undone: it previews by
    #: default and commits only with ``at``.
    dry_run: DryRun = None
    at: PreviewAt = None
