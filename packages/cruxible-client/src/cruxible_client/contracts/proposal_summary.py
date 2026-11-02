"""Bounded, factored proposal review pages shared by every transport."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cruxible_client.contracts.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import OriginatingLine
from cruxible_client.contracts.refusal_receipts import PriorRefusalCoverage


class _SummaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewSummaryValue(_SummaryModel):
    value: JsonValue
    truncated: bool = False


class ReviewSummaryCounts(_SummaryModel):
    created: int = Field(default=0, ge=0)
    revised: int = Field(default=0, ge=0)
    retired: int = Field(default=0, ge=0)


class ReviewSummaryRow(_SummaryModel):
    path: str
    before: dict[str, ReviewSummaryValue] | None
    after: dict[str, ReviewSummaryValue] | None
    findings: tuple[ReviewSummaryValue, ...] = ()
    finding_count: int = Field(default=0, ge=0)
    prior_refusals: tuple[ReviewSummaryValue, ...] = ()
    prior_refusals_more: bool = False


class ReviewSummaryGroup(_SummaryModel):
    group: str
    subject_kind: str
    artifact_kind: str
    disposition: Literal["create", "replace", "retire", "delete"]
    closure_role: Literal["authored", "generated_successor", "invalidation"]
    procedure_digest: str | None = None
    node: str | None = None
    template: int | None = None
    counts: ReviewSummaryCounts
    # Keys are before.<field> / after.<field>. Rows carry only varying fields.
    shared_fields: dict[str, ReviewSummaryValue]
    varying_columns: tuple[str, ...]
    rows: tuple[ReviewSummaryRow, ...]
    next_cursor: str | None = None


class ProposalReviewSummary(_SummaryModel):
    tag: Literal["playbill-proposal-review-summary-v1"] = "playbill-proposal-review-summary-v1"
    proposal_id: str
    candidate_digest: str
    settlement_base: AcceptedCoordinate
    originating_line: OriginatingLine | None = None
    rationale: ReviewSummaryValue | None = None
    counts: ReviewSummaryCounts
    # Proposal-wide counts, capped independently of the group page.
    subject_kinds: dict[str, ReviewSummaryCounts]
    subject_kind_count: int = Field(ge=0)
    subject_kinds_truncated: bool = False
    required_tier: PermissionTier
    activation_policy: ActivationPolicy
    approval_requirements: tuple[ApprovalRequirement, ...]
    item_reasons: tuple[ReviewSummaryValue, ...] = ()
    item_reason_count: int = Field(default=0, ge=0)
    prior_refusal_member_count: int = Field(default=0, ge=0)
    prior_refusal_coverage: PriorRefusalCoverage | None = None
    projection_advisory: Literal["Workspace projection coverage requires --detail."] = (
        "Workspace projection coverage requires --detail."
    )
    findings: tuple[ReviewSummaryValue, ...]
    finding_count: int = Field(ge=0)
    findings_next_cursor: str | None = None
    groups: tuple[ReviewSummaryGroup, ...]
    group_count: int = Field(ge=0)
    next_cursor: str | None = None
