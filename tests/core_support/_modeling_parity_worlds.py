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
    QueryDefinition,
    QueryEvaluationPolicy,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryEntry,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryOrdering,
    QueryParameterDeclaration,
    QueryParameterRef,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
    QueryTraversalStep,
)
from cruxible_core.query.backends import ClaimQueryFactsV1
from tests.core_support._modeling_parity_support import (
    EVALUATION_TIME,
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

_MANY_POLICY = QueryEvaluationPolicy(
    visible_verdicts=("supported",),
    visible_currency=("current",),
    conflict_behavior="surface_conflicts",
)
_ONE_POLICY = QueryEvaluationPolicy(
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


def work_queue_query() -> QueryDefinition:
    """The agent-operation ``work_queue`` read, restated over Claims."""

    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name="parity.agent_operation.work_queue"),
        description="Active work items dispatched for implementation.",
        entry=QueryEntry(binding="item", subject_kinds=(WORK_ITEM,)),
        where=QueryMembershipFilter(
            left=QueryClaimValueRef(binding="item", predicate=WI_STATUS),
            values=(QueryLiteralRef(value="active"),),
            value_type="string",
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="priority",
                    value=QueryClaimValueRef(binding="item", predicate=WI_PRIORITY),
                ),
                QueryProjectionField(
                    name="summary",
                    value=QueryClaimValueRef(binding="item", predicate=WI_SUMMARY),
                ),
                QueryProjectionField(
                    name="title",
                    value=QueryClaimValueRef(binding="item", predicate=WI_TITLE),
                ),
                QueryProjectionField(
                    name="type",
                    value=QueryClaimValueRef(binding="item", predicate=WI_TYPE),
                ),
                QueryProjectionField(
                    name="work_item_id",
                    value=QuerySubjectFieldRef(binding="item", field="subject_id"),
                ),
            )
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
        pins=_work_item_pins(WI_PRIORITY, WI_STATUS, WI_SUMMARY, WI_TITLE, WI_TYPE),
    )


def _note_query(
    name: str,
    *,
    description: str,
    where,
    direction: str,
    parameters: tuple[QueryParameterDeclaration, ...] = (
        QueryParameterDeclaration(name="work_item_id", value_type="string"),
    ),
) -> QueryDefinition:
    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        description=description,
        entry=QueryEntry(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRef(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStep(
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
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id",
                    value=QuerySubjectFieldRef(binding="note", field="subject_id"),
                ),
            )
        ),
        orderings=(
            QueryOrdering(
                key=QueryClaimValueRef(binding="note", predicate=SN_CREATED_AT),
                direction=direction,  # type: ignore[arg-type]
                value_type="timestamp",
            ),
        ),
        parameters=parameters,
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
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


def work_item_scratchpad_query() -> QueryDefinition:
    """The agent-operation ``work_item_scratchpad`` read, restated over Claims."""

    return _note_query(
        "parity.agent_operation.work_item_scratchpad",
        description="A work item's scratchpad notes in created order.",
        where=QueryComparisonFilter(
            left=QueryClaimValueRef(binding="note", predicate=SN_KIND),
            operator="eq",
            right=QueryLiteralRef(value="scratchpad"),
            value_type="string",
        ),
        direction="ascending",
    )


def state_notes_for_work_item_query() -> QueryDefinition:
    """The agent-operation ``state_notes_for_work_item`` read, restated over Claims."""

    return _note_query(
        "parity.agent_operation.state_notes_for_work_item",
        description="Curated state notes attached to a work item, newest first.",
        where=QueryMembershipFilter(
            left=QueryClaimValueRef(binding="note", predicate=SN_KIND),
            values=(QueryLiteralRef(value="scratchpad"),),
            value_type="string",
            negated=True,
        ),
        direction="descending",
    )


def work_item_status_query() -> QueryDefinition:
    """A one-cardinality status read that refuses rather than picking a winner."""

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.work_item_status"
        ),
        description="The status of one work item.",
        entry=QueryEntry(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRef(parameter="work_item_id"),
        ),
        result_binding="item",
        result_shape="subject",
        result_cardinality="one",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="status",
                    value=QueryClaimValueRef(binding="item", predicate=WI_STATUS),
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="work_item_id", value_type="string"),),
        evaluation_policy=_ONE_POLICY,
        default_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        pins=_work_item_pins(WI_STATUS),
    )


def notes_of_kind_query() -> QueryDefinition:
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
        where=QueryComparisonFilter(
            left=QueryClaimValueRef(binding="note", predicate=SN_KIND),
            operator="eq",
            right=QueryParameterRef(parameter="kind"),
            value_type="string",
        ),
        direction="ascending",
        parameters=(
            QueryParameterDeclaration(name="kind", value_type="string"),
            QueryParameterDeclaration(name="work_item_id", value_type="string"),
        ),
    )


def work_item_status_surfacing_query() -> QueryDefinition:
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


def notes_without_review_query() -> QueryDefinition:
    """The Claim-native stand-in for the donor's ``where_not_related`` anti-join.

    A relation edge IS a Claim on the note, so "no edge of this predicate" is a
    negated Claim-presence filter and needs no new grammar. What this cannot say
    -- and nothing in the grammar can -- is "no edge to a review IN SOME STATE":
    Claim presence never reaches the far endpoint, and there is no negated
    traversal.
    """

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.notes_without_review"
        ),
        description="Notes about a work item that hang off no review request at all.",
        entry=QueryEntry(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRef(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStep(
                binding="note",
                from_binding="item",
                predicate=SN_ABOUT_WORK_ITEM,
                direction="reverse",
                target_subject_kinds=(STATE_NOTE,),
            ),
        ),
        where=QueryClaimPresenceFilter(
            binding="note",
            predicate=SN_ABOUT_REVIEW_REQUEST,
            negated=True,
        ),
        result_binding="note",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id",
                    value=QuerySubjectFieldRef(binding="note", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="work_item_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
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


def notes_on_open_review_query() -> QueryDefinition:
    """The Claim-native stand-in for the donor's ``where_related`` semi-join.

    The donor kept a candidate when a SEPARATE edge existed, without ever
    binding what it found. A traversal step is the only join the Claim-native
    grammar has, so the joined Subject IS bound and Subject dedupe collapses the
    fan-out back down. Same rows; a wider row shape and a spent binding.
    """

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.agent_operation.notes_on_open_review"
        ),
        description="Notes about a work item that also hang off a review in one status.",
        entry=QueryEntry(
            binding="item",
            subject_kinds=(WORK_ITEM,),
            subject_id=QueryParameterRef(parameter="work_item_id"),
        ),
        traversal=(
            QueryTraversalStep(
                binding="note",
                from_binding="item",
                predicate=SN_ABOUT_WORK_ITEM,
                direction="reverse",
                target_subject_kinds=(STATE_NOTE,),
            ),
            QueryTraversalStep(
                binding="review",
                from_binding="note",
                predicate=SN_ABOUT_REVIEW_REQUEST,
                direction="forward",
                target_subject_kinds=(REVIEW_REQUEST,),
                where=QueryComparisonFilter(
                    left=QueryClaimValueRef(binding="review", predicate=RR_STATUS),
                    operator="eq",
                    right=QueryParameterRef(parameter="review_status"),
                    value_type="string",
                ),
            ),
        ),
        result_binding="note",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id",
                    value=QuerySubjectFieldRef(binding="note", field="subject_id"),
                ),
            )
        ),
        parameters=(
            QueryParameterDeclaration(name="review_status", value_type="string"),
            QueryParameterDeclaration(name="work_item_id", value_type="string"),
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=2),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=2),
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


def work_items_for_area_query() -> QueryDefinition:
    """The project-domain ``work_items_for_area`` read, restated over Claims."""

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.project_domain.work_items_for_area"
        ),
        description="Flat work items attached to a product area.",
        entry=QueryEntry(
            binding="area",
            subject_kinds=(PRODUCT_AREA,),
            subject_id=QueryParameterRef(parameter="area_id"),
        ),
        traversal=(
            QueryTraversalStep(
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
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="id",
                    value=QuerySubjectFieldRef(binding="work", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="area_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
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


def incident_work_items_query() -> QueryDefinition:
    """The supply-chain ``incident_work_items`` read, restated over Claims."""

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.supply_chain.incident_work_items"
        ),
        description="Open response work addressing this incident.",
        entry=QueryEntry(
            binding="incident",
            subject_kinds=(INCIDENT,),
            subject_id=QueryParameterRef(parameter="incident_id"),
        ),
        traversal=(
            QueryTraversalStep(
                binding="work",
                from_binding="incident",
                predicate=SWI_ADDRESSES_INCIDENT,
                direction="reverse",
                target_subject_kinds=(SUPPLY_WORK_ITEM,),
                where=QueryMembershipFilter(
                    left=QueryClaimValueRef(binding="work", predicate=SWI_STATUS),
                    values=(QueryLiteralRef(value="closed"),),
                    value_type="string",
                    negated=True,
                ),
            ),
        ),
        result_binding="work",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="priority",
                    value=QueryClaimValueRef(binding="work", predicate=SWI_PRIORITY),
                ),
                QueryProjectionField(
                    name="status",
                    value=QueryClaimValueRef(binding="work", predicate=SWI_STATUS),
                ),
                QueryProjectionField(
                    name="title",
                    value=QueryClaimValueRef(binding="work", predicate=SWI_TITLE),
                ),
                QueryProjectionField(
                    name="type",
                    value=QueryClaimValueRef(binding="work", predicate=SWI_TYPE),
                ),
                QueryProjectionField(
                    name="work_item_id",
                    value=QuerySubjectFieldRef(binding="work", field="subject_id"),
                ),
            )
        ),
        parameters=(QueryParameterDeclaration(name="incident_id", value_type="string"),),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=1),
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


def open_incidents_by_severity_query() -> QueryDefinition:
    """The supply-chain ``open_incidents_by_severity`` read, restated over Claims.

    The donor ordered by the DECLARED ORDINAL of the ``incident_severity`` enum.
    ``QueryValueType`` has no enum member, so the nearest declarable ordering
    is lexicographic over the severity string. The result SET is identical; the
    sequence is not, and the suite pins that divergence rather than hiding it.
    """

    return QueryDefinition(
        identity=ArtifactIdentity(
            kind="QueryDefinition", name="parity.supply_chain.open_incidents_by_severity"
        ),
        description="Open incidents, most severe first by lexicographic severity.",
        entry=QueryEntry(binding="incident", subject_kinds=(INCIDENT,)),
        where=QueryComparisonFilter(
            left=QueryClaimValueRef(binding="incident", predicate=INC_STATUS),
            operator="eq",
            right=QueryLiteralRef(value="open"),
            value_type="string",
        ),
        result_binding="incident",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="incident_id",
                    value=QuerySubjectFieldRef(binding="incident", field="subject_id"),
                ),
                QueryProjectionField(
                    name="severity",
                    value=QueryClaimValueRef(binding="incident", predicate=INC_SEVERITY),
                ),
                QueryProjectionField(
                    name="title",
                    value=QueryClaimValueRef(binding="incident", predicate=INC_TITLE),
                ),
            )
        ),
        orderings=(
            QueryOrdering(
                key=QueryClaimValueRef(binding="incident", predicate=INC_SEVERITY),
                direction="descending",
                value_type="string",
            ),
        ),
        evaluation_policy=_MANY_POLICY,
        default_budgets=QueryBudgets(max_results=200, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=200, max_traversal_depth=0),
        pins=(
            claim_type_pin(INC_SEVERITY, subject_kinds=(INCIDENT,)),
            claim_type_pin(INC_STATUS, subject_kinds=(INCIDENT,)),
            claim_type_pin(INC_TITLE, subject_kinds=(INCIDENT,)),
        ),
    )
