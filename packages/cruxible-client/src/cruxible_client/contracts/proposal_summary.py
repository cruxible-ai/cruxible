"""Bounded, factored proposal review pages shared by every transport."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import OriginatingLine


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


class ReviewSummaryGroup(_SummaryModel):
    group: str
    subject_kind: str
    artifact_kind: str
    disposition: str
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
    # Counts across all template groups for each kind on this page.
    subject_kinds: dict[str, ReviewSummaryCounts]
    findings: tuple[ReviewSummaryValue, ...]
    finding_count: int = Field(ge=0)
    findings_next_cursor: str | None = None
    groups: tuple[ReviewSummaryGroup, ...]
    group_count: int = Field(ge=0)
    next_cursor: str | None = None
    detail_available: Literal[True] = True
