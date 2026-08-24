"""Claim-native worlds and QueryDefinitions for the PC-F modeling-parity suite.

One module per concern: this one declares WHAT each parity domain says, the
donor module declares what the legacy surface said about the same world, and
``test_modeling_parity`` ties the two together through a pinned oracle.

The three domains are the ones PC-F names: project-domain, agent-operation, and
one business domain (supply-chain blast radius). Each world is the Claim-native
restatement of the donor world seeded in
``tests/test_playbill/test_modeling_parity_donors.py`` -- same entities, same
identifiers, same property values, re-expressed as Subjects carrying Claims.
"""

from __future__ import annotations

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryClaimValueRefV1,
    QueryComparisonFilterV1,
    QueryEntryV1,
    QueryLiteralRefV1,
    QueryMembershipFilterV1,
    QueryOrderingV1,
    QueryParameterDeclarationV1,
    QueryParameterRefV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
    QueryTraversalStepV1,
)
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1
from tests.test_playbill._modeling_parity_support import (
    EVALUATION_TIME,
    PARITY_AUTHORITY,
    claim_fact,
    claim_type_pin,
    facts,
    instant,
    subject,
)

# -- domain vocabulary ----------------------------------------------------

WORK_ITEM = "project.work_item"
STATE_NOTE = "project.state_note"
REVIEW_REQUEST = "project.review_request"
PRODUCT_AREA = "project.product_area"
INCIDENT = "supply.incident"
SUPPLY_WORK_ITEM = "supply.work_item"

WI_TITLE = "project.work_item.title"
WI_SUMMARY = "project.work_item.summary"
WI_STATUS = "project.work_item.status"
WI_PRIORITY = "project.work_item.priority"
WI_TYPE = "project.work_item.type"
WI_TARGETS_AREA = "project.work_item.targets_area"
SN_KIND = "project.state_note.kind"
SN_CREATED_AT = "project.state_note.created_at"
SN_ABOUT_WORK_ITEM = "project.state_note.about_work_item"
SN_ABOUT_REVIEW_REQUEST = "project.state_note.about_review_request"
RR_STATUS = "project.review_request.status"
INC_TITLE = "supply.incident.title"
INC_SEVERITY = "supply.incident.severity"
INC_STATUS = "supply.incident.status"
SWI_TITLE = "supply.work_item.title"
SWI_STATUS = "supply.work_item.status"
SWI_PRIORITY = "supply.work_item.priority"
SWI_TYPE = "supply.work_item.type"
SWI_ADDRESSES_INCIDENT = "supply.work_item.addresses_incident"

_MANY_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=("supported",),
    visible_currency=("current",),
    conflict_behavior="surface_conflicts",
)
_ONE_POLICY = QueryEvaluationPolicyV1(
    visible_verdicts=("supported",),
    visible_currency=("current",),
    conflict_behavior="refuse_on_conflict",
)


def _work_item_pins(*predicates: str) -> tuple:
    return tuple(claim_type_pin(item, subject_kinds=(WORK_ITEM,)) for item in predicates)


# -- agent-operation ------------------------------------------------------

_AO_WORK_ITEMS = {
    "wi-1": ("Land the Claim-native query engine", "PC-F slice work", "feature", "active", "high"),
    "wi-2": ("Fix the ordering tiebreak", "blocked on review", "bug", "blocked", "critical"),
    "wi-3": ("Retire the donor island", "purge prep", "cleanup", "active", "medium"),
    "wi-4": ("Archive the old kits", "deferred for now", "docs", "deferred", "low"),
}
_AO_NOTES = {
    "sn-1": ("implementation_note", instant(10, 9)),
    "sn-2": ("review_note", instant(11, 9)),
    "sn-3": ("scratchpad", instant(10, 12)),
    "sn-4": ("scratchpad", instant(12, 12)),
}
_AO_REVIEWS = {"rr-1": "requested"}
_AO_NOTE_REVIEWS = {"sn-2": "rr-1"}


def agent_operation_facts(*, competing_status_on: str | None = None) -> ClaimQueryFactsV1:
    """Return the agent-operation world; optionally with a competing status Claim.

    ``competing_status_on`` adds a SECOND accepted, supported status Claim for
    one work item. The donor property store could not hold two -- the later
    write overwrote the earlier one. Here both stand, and the one-cardinality
    read has to say so.
    """

    subjects = [subject(WORK_ITEM, item) for item in _AO_WORK_ITEMS]
    subjects.extend(subject(STATE_NOTE, note) for note in _AO_NOTES)
    subjects.extend(subject(REVIEW_REQUEST, review) for review in _AO_REVIEWS)
    claims = []
    index = 0
    for identifier, (title, summary, kind, status, priority) in _AO_WORK_ITEMS.items():
        row = subject(WORK_ITEM, identifier)
        for predicate, value in (
            (WI_TITLE, title),
            (WI_SUMMARY, summary),
            (WI_TYPE, kind),
            (WI_STATUS, status),
            (WI_PRIORITY, priority),
        ):
            index += 1
            claims.append(claim_fact(index, subject_row=row, predicate=predicate, value=value))
    for identifier, (kind, created_at) in _AO_NOTES.items():
        row = subject(STATE_NOTE, identifier)
        for predicate, value in ((SN_KIND, kind), (SN_CREATED_AT, created_at)):
            index += 1
            claims.append(claim_fact(index, subject_row=row, predicate=predicate, value=value))
        index += 1
        claims.append(
            claim_fact(
                index,
                subject_row=row,
                predicate=SN_ABOUT_WORK_ITEM,
                value=subject(WORK_ITEM, "wi-1"),
                object_subject_kinds=(WORK_ITEM,),
            )
        )
        if identifier in _AO_NOTE_REVIEWS:
            index += 1
            claims.append(
                claim_fact(
                    index,
                    subject_row=row,
                    predicate=SN_ABOUT_REVIEW_REQUEST,
                    value=subject(REVIEW_REQUEST, _AO_NOTE_REVIEWS[identifier]),
                    object_subject_kinds=(REVIEW_REQUEST,),
                )
            )
    for identifier, status in _AO_REVIEWS.items():
        index += 1
        claims.append(
            claim_fact(
                index,
                subject_row=subject(REVIEW_REQUEST, identifier),
                predicate=RR_STATUS,
                value=status,
            )
        )
    if competing_status_on is not None:
        index += 1
        claims.append(
            claim_fact(
                index,
                subject_row=subject(WORK_ITEM, competing_status_on),
                predicate=WI_STATUS,
                value="blocked",
            )
        )
    return facts("agent-operation", tuple(subjects), tuple(claims))


def agent_operation_expired_status_facts() -> ClaimQueryFactsV1:
    """Return the world with wi-3's ``active`` status closed at an explicit instant.

    The donor read has no evaluation-time axis at all: a property is whatever
    the last write left there. A Claim carries its own effective interval, so
    the same declaration answers differently before and after that instant
    without anything being rewritten.
    """

    subjects = [subject(WORK_ITEM, item) for item in _AO_WORK_ITEMS]
    claims = []
    index = 0
    for identifier, (title, summary, kind, status, priority) in _AO_WORK_ITEMS.items():
        row = subject(WORK_ITEM, identifier)
        for predicate, value in (
            (WI_TITLE, title),
            (WI_SUMMARY, summary),
            (WI_TYPE, kind),
            (WI_STATUS, status),
            (WI_PRIORITY, priority),
        ):
            index += 1
            claims.append(
                claim_fact(
                    index,
                    subject_row=row,
                    predicate=predicate,
                    value=value,
                    effective_until=(
                        EVALUATION_TIME if identifier == "wi-3" and predicate == WI_STATUS else None
                    ),
                )
            )
    return facts("agent-operation", tuple(subjects), tuple(claims))


def work_queue_query() -> QueryDefinitionV1:
    """The agent-operation ``work_queue`` read, restated over Claims."""

    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name="parity.agent_operation.work_queue"),
        description="Active work items dispatched for implementation.",
        entry=QueryEntryV1(binding="item", subject_kinds=(WORK_ITEM,)),
        where=QueryMembershipFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=WI_STATUS),
            values=(QueryLiteralRefV1(value="active"),),
            value_type="string",
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="priority",
                    value=QueryClaimValueRefV1(binding="item", predicate=WI_PRIORITY),
                ),
                QueryProjectionFieldV1(
                    name="summary",
                    value=QueryClaimValueRefV1(binding="item", predicate=WI_SUMMARY),
                ),
                QueryProjectionFieldV1(
                    name="title",
                    value=QueryClaimValueRefV1(binding="item", predicate=WI_TITLE),
                ),
                QueryProjectionFieldV1(
                    name="type",
                    value=QueryClaimValueRefV1(binding="item", predicate=WI_TYPE),
                ),
                QueryProjectionFieldV1(
                    name="work_item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
            )
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=100, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=100, max_traversal_depth=0),
        authority=PARITY_AUTHORITY,
        pins=_work_item_pins(WI_PRIORITY, WI_STATUS, WI_SUMMARY, WI_TITLE, WI_TYPE),
    )


def _note_query(
    name: str,
    *,
    description: str,
    where,
    direction: str,
    parameters: tuple[QueryParameterDeclarationV1, ...] = (
        QueryParameterDeclarationV1(name="work_item_id", value_type="string"),
    ),
) -> QueryDefinitionV1:
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        description=description,
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRefV1(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="note",
                from_binding="item",
                predicate=SN_ABOUT_WORK_ITEM,
                direction="reverse",
                target_subject_kinds=(STATE_NOTE,),
                where=where,
            ),
        ),
        result_binding="note",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id",
                    value=QuerySubjectFieldRefV1(binding="note", field="subject_id"),
                ),
            )
        ),
        orderings=(
            QueryOrderingV1(
                key=QueryClaimValueRefV1(binding="note", predicate=SN_CREATED_AT),
                direction=direction,  # type: ignore[arg-type]
                value_type="timestamp",
            ),
        ),
        parameters=parameters,
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(
                SN_ABOUT_WORK_ITEM,
                subject_kinds=(STATE_NOTE,),
                object_subject_kinds=(WORK_ITEM,),
            ),
            claim_type_pin(SN_CREATED_AT, subject_kinds=(STATE_NOTE,)),
            claim_type_pin(SN_KIND, subject_kinds=(STATE_NOTE,)),
        ),
    )


def work_item_scratchpad_query() -> QueryDefinitionV1:
    """The agent-operation ``work_item_scratchpad`` read, restated over Claims."""

    return _note_query(
        "parity.agent_operation.work_item_scratchpad",
        description="A work item's scratchpad notes in created order.",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="note", predicate=SN_KIND),
            operator="eq",
            right=QueryLiteralRefV1(value="scratchpad"),
            value_type="string",
        ),
        direction="ascending",
    )


def state_notes_for_work_item_query() -> QueryDefinitionV1:
    """The agent-operation ``state_notes_for_work_item`` read, restated over Claims."""

    return _note_query(
        "parity.agent_operation.state_notes_for_work_item",
        description="Curated state notes attached to a work item, newest first.",
        where=QueryMembershipFilterV1(
            left=QueryClaimValueRefV1(binding="note", predicate=SN_KIND),
            values=(QueryLiteralRefV1(value="scratchpad"),),
            value_type="string",
            negated=True,
        ),
        direction="descending",
    )


def work_item_status_query() -> QueryDefinitionV1:
    """A one-cardinality status read that refuses rather than picking a winner."""

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.work_item_status"
        ),
        description="The status of one work item.",
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRefV1(parameter="work_item_id"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="status",
                    value=QueryClaimValueRefV1(binding="item", predicate=WI_STATUS),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="work_item_id", value_type="string"),),
        evaluation_policy=_ONE_POLICY,
        default_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
        authority=PARITY_AUTHORITY,
        pins=_work_item_pins(WI_STATUS),
    )


def notes_of_kind_query() -> QueryDefinitionV1:
    """The Claim-native stand-in for the donor's step-constraint mini-language.

    ``constraint: "target.kind == $kind"`` compares one traversal candidate's
    property against a bound parameter. The Claim-native form is an ordinary
    typed comparison filter whose right side is a parameter reference -- same
    meaning, declared type instead of an inferred one, and a refusal instead of
    a silently-passing candidate when the value does not typecheck.
    """

    return _note_query(
        "parity.agent_operation.notes_of_kind",
        description="A work item's notes of one caller-supplied kind, in created order.",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="note", predicate=SN_KIND),
            operator="eq",
            right=QueryParameterRefV1(parameter="kind"),
            value_type="string",
        ),
        direction="ascending",
        parameters=(
            QueryParameterDeclarationV1(name="kind", value_type="string"),
            QueryParameterDeclarationV1(name="work_item_id", value_type="string"),
        ),
    )


def work_item_status_surfacing_query() -> QueryDefinitionV1:
    """The same one-cardinality status read under a surfacing conflict policy.

    Refusing and surfacing are the two dispositions the accepted policy allows.
    Neither one picks a winner, which is the whole of the divergence from the
    donor's last-write-wins property store.
    """

    base = work_item_status_query()
    return base.model_copy(
        update={
            "identity": ArtifactIdentity(
                kind="QueryDefinition",
                name="parity.agent_operation.work_item_status_surfaced",
            ),
            "evaluation_policy": _MANY_POLICY,
        }
    )


def notes_without_review_query() -> QueryDefinitionV1:
    """The Claim-native stand-in for the donor's ``where_not_related`` anti-join.

    A relation edge IS a Claim on the note, so "no edge of this predicate" is a
    negated Claim-presence filter and needs no new grammar. What this cannot say
    -- and nothing in the grammar can -- is "no edge to a review IN SOME STATE":
    Claim presence never reaches the far endpoint, and there is no negated
    traversal.
    """

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.notes_without_review"
        ),
        description="Notes about a work item that hang off no review request at all.",
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRefV1(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="note",
                from_binding="item",
                predicate=SN_ABOUT_WORK_ITEM,
                direction="reverse",
                target_subject_kinds=(STATE_NOTE,),
            ),
        ),
        where=QueryClaimPresenceFilterV1(
            binding="note",
            predicate=SN_ABOUT_REVIEW_REQUEST,
            negated=True,
        ),
        result_binding="note",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id",
                    value=QuerySubjectFieldRefV1(binding="note", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="work_item_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(
                SN_ABOUT_REVIEW_REQUEST,
                subject_kinds=(STATE_NOTE,),
                object_subject_kinds=(REVIEW_REQUEST,),
            ),
            claim_type_pin(
                SN_ABOUT_WORK_ITEM,
                subject_kinds=(STATE_NOTE,),
                object_subject_kinds=(WORK_ITEM,),
            ),
        ),
    )


def notes_on_open_review_query() -> QueryDefinitionV1:
    """The Claim-native stand-in for the donor's ``where_related`` semi-join.

    The donor kept a candidate when a SEPARATE edge existed, without ever
    binding what it found. A traversal step is the only join the Claim-native
    grammar has, so the joined Subject IS bound and Subject dedupe collapses the
    fan-out back down. Same rows; a wider row shape and a spent binding.
    """

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.notes_on_open_review"
        ),
        description="Notes about a work item that also hang off a review in one status.",
        entry=QueryEntryV1(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRefV1(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="note",
                from_binding="item",
                predicate=SN_ABOUT_WORK_ITEM,
                direction="reverse",
                target_subject_kinds=(STATE_NOTE,),
            ),
            QueryTraversalStepV1(
                binding="review",
                from_binding="note",
                predicate=SN_ABOUT_REVIEW_REQUEST,
                direction="forward",
                target_subject_kinds=(REVIEW_REQUEST,),
                where=QueryComparisonFilterV1(
                    left=QueryClaimValueRefV1(binding="review", predicate=RR_STATUS),
                    operator="eq",
                    right=QueryParameterRefV1(parameter="review_status"),
                    value_type="string",
                ),
            ),
        ),
        result_binding="note",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id",
                    value=QuerySubjectFieldRefV1(binding="note", field="subject_id"),
                ),
            )
        ),
        parameters=(
            QueryParameterDeclarationV1(name="review_status", value_type="string"),
            QueryParameterDeclarationV1(name="work_item_id", value_type="string"),
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=2),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=2),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(RR_STATUS, subject_kinds=(REVIEW_REQUEST,)),
            claim_type_pin(
                SN_ABOUT_REVIEW_REQUEST,
                subject_kinds=(STATE_NOTE,),
                object_subject_kinds=(REVIEW_REQUEST,),
            ),
            claim_type_pin(
                SN_ABOUT_WORK_ITEM,
                subject_kinds=(STATE_NOTE,),
                object_subject_kinds=(WORK_ITEM,),
            ),
        ),
    )


# -- project-domain -------------------------------------------------------

_PD_AREAS = {"pa-core": "Core runtime", "pa-ui": "Inspection UI"}
_PD_WORK_ITEMS = {
    "wi-a": ("Port the traversal semantics", "feature", "active", "high", "pa-core"),
    "wi-b": ("Delete the overlay authority", "cleanup", "closed", "low", "pa-core"),
    "wi-c": ("Unattached work", "research", "active", "medium", None),
}


def project_domain_facts() -> ClaimQueryFactsV1:
    """Return the project-domain world: product areas and the work targeting them."""

    subjects = [subject(PRODUCT_AREA, area) for area in _PD_AREAS]
    subjects.extend(subject(WORK_ITEM, item) for item in _PD_WORK_ITEMS)
    claims = []
    index = 0
    for identifier, (title, kind, status, priority, area) in _PD_WORK_ITEMS.items():
        row = subject(WORK_ITEM, identifier)
        for predicate, value in (
            (WI_TITLE, title),
            (WI_TYPE, kind),
            (WI_STATUS, status),
            (WI_PRIORITY, priority),
        ):
            index += 1
            claims.append(claim_fact(index, subject_row=row, predicate=predicate, value=value))
        if area is not None:
            index += 1
            claims.append(
                claim_fact(
                    index,
                    subject_row=row,
                    predicate=WI_TARGETS_AREA,
                    value=subject(PRODUCT_AREA, area),
                    object_subject_kinds=(PRODUCT_AREA,),
                )
            )
    return facts("project-domain", tuple(subjects), tuple(claims))


def work_items_for_area_query() -> QueryDefinitionV1:
    """The project-domain ``work_items_for_area`` read, restated over Claims."""

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.project_domain.work_items_for_area"
        ),
        description="Flat work items attached to a product area.",
        entry=QueryEntryV1(
            binding="area",
            subject_kinds=(PRODUCT_AREA,),
            subject_id=QueryParameterRefV1(parameter="area_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="work",
                from_binding="area",
                predicate=WI_TARGETS_AREA,
                direction="reverse",
                target_subject_kinds=(WORK_ITEM,),
            ),
        ),
        result_binding="work",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="id",
                    value=QuerySubjectFieldRefV1(binding="work", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="area_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(
                WI_TARGETS_AREA,
                subject_kinds=(WORK_ITEM,),
                object_subject_kinds=(PRODUCT_AREA,),
            ),
        ),
    )


# -- supply-chain blast radius (the business domain) ----------------------

_SC_INCIDENTS = {
    "inc-1": ("Fab fire at tier-2 supplier", "critical", "open"),
    "inc-2": ("Port congestion", "medium", "open"),
    "inc-3": ("Resolved customs hold", "high", "closed"),
}
_SC_WORK_ITEMS = {
    "wi-s1": ("Qualify alternate supplier", "operations", "active", "critical", "inc-1"),
    "wi-s2": ("Old mitigation", "operations", "closed", "low", "inc-1"),
    "wi-s3": ("Expedite inventory", "operations", "blocked", "high", "inc-1"),
}


def supply_chain_facts() -> ClaimQueryFactsV1:
    """Return the supply-chain world: incidents and the response work on them."""

    subjects = [subject(INCIDENT, item) for item in _SC_INCIDENTS]
    subjects.extend(subject(SUPPLY_WORK_ITEM, item) for item in _SC_WORK_ITEMS)
    claims = []
    index = 0
    for identifier, (title, severity, status) in _SC_INCIDENTS.items():
        row = subject(INCIDENT, identifier)
        for predicate, value in (
            (INC_TITLE, title),
            (INC_SEVERITY, severity),
            (INC_STATUS, status),
        ):
            index += 1
            claims.append(claim_fact(index, subject_row=row, predicate=predicate, value=value))
    for identifier, (title, kind, status, priority, incident) in _SC_WORK_ITEMS.items():
        row = subject(SUPPLY_WORK_ITEM, identifier)
        for predicate, value in (
            (SWI_TITLE, title),
            (SWI_TYPE, kind),
            (SWI_STATUS, status),
            (SWI_PRIORITY, priority),
        ):
            index += 1
            claims.append(claim_fact(index, subject_row=row, predicate=predicate, value=value))
        index += 1
        claims.append(
            claim_fact(
                index,
                subject_row=row,
                predicate=SWI_ADDRESSES_INCIDENT,
                value=subject(INCIDENT, incident),
                object_subject_kinds=(INCIDENT,),
            )
        )
    return facts("supply-chain", tuple(subjects), tuple(claims))


def incident_work_items_query() -> QueryDefinitionV1:
    """The supply-chain ``incident_work_items`` read, restated over Claims."""

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.supply_chain.incident_work_items"
        ),
        description="Open response work addressing this incident.",
        entry=QueryEntryV1(
            binding="incident",
            subject_kinds=(INCIDENT,),
            subject_id=QueryParameterRefV1(parameter="incident_id"),
        ),
        traversal=(
            QueryTraversalStepV1(
                binding="work",
                from_binding="incident",
                predicate=SWI_ADDRESSES_INCIDENT,
                direction="reverse",
                target_subject_kinds=(SUPPLY_WORK_ITEM,),
                where=QueryMembershipFilterV1(
                    left=QueryClaimValueRefV1(binding="work", predicate=SWI_STATUS),
                    values=(QueryLiteralRefV1(value="closed"),),
                    value_type="string",
                    negated=True,
                ),
            ),
        ),
        result_binding="work",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="priority",
                    value=QueryClaimValueRefV1(binding="work", predicate=SWI_PRIORITY),
                ),
                QueryProjectionFieldV1(
                    name="status",
                    value=QueryClaimValueRefV1(binding="work", predicate=SWI_STATUS),
                ),
                QueryProjectionFieldV1(
                    name="title",
                    value=QueryClaimValueRefV1(binding="work", predicate=SWI_TITLE),
                ),
                QueryProjectionFieldV1(
                    name="type",
                    value=QueryClaimValueRefV1(binding="work", predicate=SWI_TYPE),
                ),
                QueryProjectionFieldV1(
                    name="work_item_id",
                    value=QuerySubjectFieldRefV1(binding="work", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclarationV1(name="incident_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=1),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(
                SWI_ADDRESSES_INCIDENT,
                subject_kinds=(SUPPLY_WORK_ITEM,),
                object_subject_kinds=(INCIDENT,),
            ),
            claim_type_pin(SWI_PRIORITY, subject_kinds=(SUPPLY_WORK_ITEM,)),
            claim_type_pin(SWI_STATUS, subject_kinds=(SUPPLY_WORK_ITEM,)),
            claim_type_pin(SWI_TITLE, subject_kinds=(SUPPLY_WORK_ITEM,)),
            claim_type_pin(SWI_TYPE, subject_kinds=(SUPPLY_WORK_ITEM,)),
        ),
    )


def open_incidents_by_severity_query() -> QueryDefinitionV1:
    """The supply-chain ``open_incidents_by_severity`` read, restated over Claims.

    The donor ordered by the DECLARED ORDINAL of the ``incident_severity`` enum.
    ``QueryValueTypeV1`` has no enum member, so the nearest declarable ordering
    is lexicographic over the severity string. The result SET is identical; the
    sequence is not, and the suite pins that divergence rather than hiding it.
    """

    return QueryDefinitionV1(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.supply_chain.open_incidents_by_severity"
        ),
        description="Open incidents, most severe first by lexicographic severity.",
        entry=QueryEntryV1(binding="incident", subject_kinds=(INCIDENT,)),
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="incident", predicate=INC_STATUS),
            operator="eq",
            right=QueryLiteralRefV1(value="open"),
            value_type="string",
        ),
        result_binding="incident",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="incident_id",
                    value=QuerySubjectFieldRefV1(binding="incident", field="subject_id"),
                ),
                QueryProjectionFieldV1(
                    name="severity",
                    value=QueryClaimValueRefV1(binding="incident", predicate=INC_SEVERITY),
                ),
                QueryProjectionFieldV1(
                    name="title",
                    value=QueryClaimValueRefV1(binding="incident", predicate=INC_TITLE),
                ),
            )
        ),
        orderings=(
            QueryOrderingV1(
                key=QueryClaimValueRefV1(binding="incident", predicate=INC_SEVERITY),
                direction="descending",
                value_type="string",
            ),
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=200, max_traversal_depth=0),
        authority=PARITY_AUTHORITY,
        pins=(
            claim_type_pin(INC_SEVERITY, subject_kinds=(INCIDENT,)),
            claim_type_pin(INC_STATUS, subject_kinds=(INCIDENT,)),
            claim_type_pin(INC_TITLE, subject_kinds=(INCIDENT,)),
        ),
    )
