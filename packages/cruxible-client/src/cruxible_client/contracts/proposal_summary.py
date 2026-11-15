"""Bounded, factored proposal review pages shared by every transport."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cruxible_client.contracts.capture_reads import BodyRestricted
from cruxible_client.contracts.doc_sections import SectionPath
from cruxible_client.contracts.governance import (
    ActivationPolicy,
    ApprovalRequirement,
    PermissionTier,
)
from cruxible_client.contracts.operational_reads import MandateScope
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.proposal_models import OriginatingLine
from cruxible_client.contracts.refusal_receipts import PriorRefusalCoverage
from cruxible_client.contracts.write import ChangeOutcome


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


class ReviewGuidance(_SummaryModel):
    """A governed Document that ``guides`` a ClaimType or Procedure this proposal touches."""

    definition: str = Field(description="ClaimType:<predicate> or Procedure:<name>.")
    document: str
    title: str | None = None


#: Value changes one summary page shows, at most.
REVIEW_CHANGES_PAGE = 10
#: Definitions one summary page shows, at most.
REVIEW_DEFINITIONS_PAGE = 20
#: Characters of cited text one citation shows before it is cut.
REVIEW_CITED_TEXT_MAX_CHARS = 600
#: Citations one change shows, at most.
REVIEW_CITATIONS_MAX = 3
#: (Subject kind, field, op) count rows a summary carries, at most.
REVIEW_CHANGE_COUNTS_MAX = 20


def _omit_none(value: object) -> bool:
    return value is None


def _omit_empty(value: object) -> bool:
    return value in (None, (), [], {})


class ReviewCitation(_SummaryModel):
    """One Capture a proposed Claim cites, where in it, and the text it cites.

    ``source`` is the Capture's logical source (a catalogued file's name, a
    feed); ``section`` and ``anchor`` locate a file citation. ``text`` is the
    cited bytes, cut at ``REVIEW_CITED_TEXT_MAX_CHARS`` (``text_truncated``).
    It is absent when this reader may not read Capture bodies
    (``text_withheld``), or when the Capture commits to no exact bytes.
    """

    capture: str = Field(description="CAP-<12 hex>; get and read_capture accept it.")
    origin: str = Field(description="independent, self_source (own words) or legacy.")
    source: str | None = Field(default=None, exclude_if=_omit_none)
    section: SectionPath | None = Field(default=None, exclude_if=_omit_none)
    anchor: str | None = Field(default=None, exclude_if=_omit_none)
    text: str | None = Field(default=None, exclude_if=_omit_none)
    text_truncated: bool = Field(default=False, exclude_if=lambda value: not value)
    text_withheld: bool = Field(default=False, exclude_if=lambda value: not value)


class ReviewChange(_SummaryModel):
    """One proposed value change, in the write outcome's form, with what it cites.

    ``change`` is a ``ChangeOutcome`` exactly as ``write`` answers one: op,
    Subject, field, before and after, the Claim and its candidate verdict.
    ``role`` is the Claim's role; ``rationale`` the item's own reason, when a
    Procedure proposed it (the change set's ``rationale`` covers the rest).
    """

    change: ChangeOutcome
    role: str | None = Field(default=None, exclude_if=_omit_none)
    rationale: str | None = Field(default=None, exclude_if=_omit_none)
    citations: tuple[ReviewCitation, ...] = Field(default=(), exclude_if=_omit_empty)
    citations_more: int = Field(default=0, ge=0, exclude_if=lambda value: not value)


class ReviewChangeCount(_SummaryModel):
    """How many of a proposal's value changes have this Subject kind, field and op."""

    subject_kind: str
    field: str | None = Field(default=None, exclude_if=_omit_none)
    op: str
    count: int = Field(ge=1)


class ReviewLineBinding(_SummaryModel):
    """One Line slot: the provider build (``name/<12 hex>``) and operation it is bound to."""

    slot: str
    provider: str
    operation: str


class ReviewLineDefinition(_SummaryModel):
    kind: Literal["line"] = "line"
    procedure: str
    authority: str
    bindings: tuple[ReviewLineBinding, ...] = ()
    parameters: dict[str, ReviewSummaryValue] = Field(default_factory=dict)
    #: Capture output -> the origins its acquisition may reach.
    acquisition: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    budgets: dict[str, int] = Field(default_factory=dict)
    inbox: tuple[str, ...] = ()
    trigger_input: str | None = Field(default=None, exclude_if=_omit_none)


class ReviewMandateDefinition(_SummaryModel):
    kind: Literal["procedure-mandate"] = "procedure-mandate"
    line: str
    grants: Literal["propose", "settle"]
    valid_from: datetime
    expires_at: datetime
    namespace: tuple[str, ...] = ()
    scope: tuple[MandateScope, ...] = ()
    subjects: str | None = Field(default=None, exclude_if=_omit_none)
    condition: str | None = Field(default=None, exclude_if=_omit_none)
    subject_creation: tuple[str, ...] = Field(default=(), exclude_if=_omit_empty)
    suspended: bool = Field(default=False, exclude_if=lambda value: not value)


class ReviewRunbookDefinition(_SummaryModel):
    kind: Literal["runbook"] = "runbook"
    description: str | None = Field(default=None, exclude_if=_omit_none)
    #: In order: ``Line:<name>`` or ``action:<name>``.
    steps: tuple[str, ...]


class ReviewTriggerDefinition(_SummaryModel):
    kind: Literal["trigger"] = "trigger"
    schedule: str
    target: str


ReviewDefinitionDetail = Annotated[
    ReviewLineDefinition
    | ReviewMandateDefinition
    | ReviewRunbookDefinition
    | ReviewTriggerDefinition,
    Field(discriminator="kind"),
]


class ReviewFieldChange(_SummaryModel):
    """One top-level field a revision changes: its value before and after."""

    field: str
    before: ReviewSummaryValue | None = None
    after: ReviewSummaryValue | None = None


class ReviewDefinition(_SummaryModel):
    """One proposed definition: its kind, name and action, and its typed fields.

    ``detail`` is the definition as it would stand (Line, ProcedureMandate,
    Runbook, Trigger); other kinds, and retirements, carry none. ``changed``
    lists a revision's changed fields.
    """

    kind: str
    name: str
    action: Literal["created", "revised", "retired"]
    detail: ReviewDefinitionDetail | None = Field(default=None, exclude_if=_omit_none)
    changed: tuple[ReviewFieldChange, ...] = Field(default=(), exclude_if=_omit_empty)


class ProposalReviewSummary(_SummaryModel):
    tag: Literal["playbill-proposal-review-summary-v1"] = "playbill-proposal-review-summary-v1"
    proposal_id: str
    candidate_digest: str
    settlement_base: AcceptedCoordinate
    originating_line: OriginatingLine | None = None
    rationale: ReviewSummaryValue | None = None
    #: The reviewer's view: the value changes (Claims, Subjects) of this page,
    #: in the write outcome's form with what each cites, and the definitions.
    changes: tuple[ReviewChange, ...] = ()
    change_count: int = Field(default=0, ge=0)
    #: Position of the next page of changes (``changes_from``), when there is one.
    changes_next: int | None = Field(default=None, exclude_if=_omit_none)
    #: Counts per Subject kind, field and op over every value change (bounded).
    change_counts: tuple[ReviewChangeCount, ...] = Field(default=(), exclude_if=_omit_empty)
    change_counts_truncated: bool = Field(default=False, exclude_if=lambda value: not value)
    definitions: tuple[ReviewDefinition, ...] = ()
    definition_count: int = Field(default=0, ge=0)
    definitions_next: int | None = Field(default=None, exclude_if=_omit_none)
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
    # Review guidance for the ClaimTypes and Procedure this proposal touches,
    # read from the backlink index at the current head (bounded).
    guidance: tuple[ReviewGuidance, ...] = Field(default=(), exclude_if=lambda value: not value)
    groups: tuple[ReviewSummaryGroup, ...]
    group_count: int = Field(ge=0)
    next_cursor: str | None = None
    #: C14: a candidate Claim cites a Capture withheld from this reader, so the
    #: proposed values, findings and item reasons are left out (accepted
    #: ``before`` values and the candidate digest stay).
    withheld: BodyRestricted | None = Field(default=None, exclude_if=lambda value: value is None)

    def __str__(self) -> str:
        """The review as ``cruxible proposal review`` prints it; ``repr()`` stays the model."""

        from cruxible_client.contracts.text_display import render_review_summary

        return render_review_summary(self)
