"""Materialized Subject views and deterministic direct-index query behavior."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_digest
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimPresenceFilterV1,
    QueryClaimValueRefV1,
    QueryComparisonFilterV1,
    QueryDisjunctionFilterV1,
    QueryEntryV1,
    QueryEvaluationTimeRefV1,
    QueryLiteralRefV1,
    QueryMembershipFilterV1,
    QueryNegationFilterV1,
    QueryOrderingV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
    QueryTraversalStepV1,
)
from cruxible_client.contracts.subjects import AcceptedSubject, subject_path
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.backends import (
    ClaimFactRowV1,
    ClaimQueryBackendFactoryV1,
    ClaimQueryFactsV1,
    DirectClaimFactIndex,
    SubjectQueryViewV1,
    VisibleClaimRow,
    render_subject_query_view,
    subject_query_view,
    subject_query_view_digest,
)
from cruxible_core.playbill.query.engine import (
    CLAIM_CONFLICT,
    RESULT_CONFLICT,
    SUBJECT_UNRESOLVED,
    TRAVERSAL_OBJECT_NOT_SUBJECT,
    ClaimQueryResultV1,
    claim_query_result_digest,
    evaluate_claim_query,
)
from tests.test_playbill.test_claim_query_engine import (
    AMOUNT_PREDICATE,
    DUE_PREDICATE,
    LATER,
    NOW,
    PEOPLE,
    RANK_PREDICATE,
    WORK_ITEMS,
    all_items_query,
    claim_fact,
    collection_status_query,
    coordinate,
    facts,
    reviewer_claim,
    sorted_operands,
    status_claim,
    subject,
    typed_item_query,
)
from tests.test_playbill.test_query_definitions import (
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
    accepted_query,
    active_work_query,
    claim_type,
    claim_type_pin,
    single_status_query,
)

PREDICATES = (
    AMOUNT_PREDICATE,
    DUE_PREDICATE,
    RANK_PREDICATE,
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
)
SUBJECT_KINDS = ("project.person", "project.work_item")
ABSENT_PATH = subject_path("project.work_item", "wi-absent")


# -- fact fixtures beyond the F2 corpus -----------------------------------


def custom_facts(
    subjects: Iterable[AcceptedSubject],
    claims: Iterable[ClaimFactRowV1],
) -> ClaimQueryFactsV1:
    """Build accepted facts with an explicit Subject set, unlike the full F2 corpus."""

    return ClaimQueryFactsV1(
        coordinate=coordinate(),
        subjects=tuple(sorted(subjects, key=lambda item: item.path.encode("utf-8"))),
        claims=tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8"))),
    )


def work_item_subjects() -> tuple[AcceptedSubject, ...]:
    return tuple(subject("project.work_item", item) for item in WORK_ITEMS)


def person_subjects() -> tuple[AcceptedSubject, ...]:
    return tuple(subject("project.person", item) for item in PEOPLE)


def unresolved_target_facts() -> ClaimQueryFactsV1:
    """A relation Claim whose named Subject is absent at the accepted coordinate."""

    return custom_facts(
        work_item_subjects(),
        (status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada")),
    )


def unresolved_subject_facts() -> ClaimQueryFactsV1:
    """A Claim about a Subject that is absent at the accepted coordinate."""

    return custom_facts(person_subjects(), (status_claim(1, "wi-1", "ready"),))


def all_reviewers_facts() -> ClaimQueryFactsV1:
    return facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-2", "ready"),
            reviewer_claim(3, "wi-1", "ada"),
            reviewer_claim(4, "wi-1", "grace"),
            reviewer_claim(5, "wi-2", "ada"),
        )
    )


def window_facts() -> ClaimQueryFactsV1:
    return facts(
        (
            status_claim(1, "wi-1", "ready", effective_from=LATER),
            status_claim(2, "wi-2", "blocked", effective_until=LATER),
            reviewer_claim(3, "wi-1", "ada"),
        )
    )


def literal_traversal_query() -> QueryDefinitionV1:
    """A traversal declared over a literal-object predicate, which must refuse."""

    return active_work_query(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.literal_traversal"),
        traversal=(
            QueryTraversalStepV1(
                binding="reviewer",
                from_binding="item",
                predicate=STATUS_PREDICATE,
                direction="forward",
            ),
        ),
        where=None,
        includes=(),
        orderings=(),
        parameters=(),
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
            )
        ),
        pins=(claim_type_pin(STATUS_PREDICATE),),
    )


def reverse_traversal_query() -> QueryDefinitionV1:
    """The reviewer-to-work-item read, traversed against the relation Claim."""

    return active_work_query(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.reviewer_work"),
        entry=QueryEntryV1(binding="reviewer", subject_kinds=("project.person",)),
        traversal=(
            QueryTraversalStepV1(
                binding="item",
                from_binding="reviewer",
                predicate=REVIEWER_PREDICATE,
                direction="reverse",
                target_subject_kinds=("project.work_item",),
            ),
        ),
        result_binding="item",
        orderings=(),
        includes=(),
        where=None,
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
            )
        ),
        parameters=(),
        pins=(
            ArtifactPin(
                role="claim-type",
                target=ArtifactIdentity(kind="ClaimType", name=REVIEWER_PREDICATE),
                artifact_digest=claim_type_digest(
                    claim_type(REVIEWER_PREDICATE, object_kind="subject")
                ).tagged,
            ),
        ),
    )


def membership_query() -> QueryDefinitionV1:
    return typed_item_query(
        STATUS_PREDICATE,
        name="project.tracked_items",
        where=QueryDisjunctionFilterV1(
            filters=sorted_operands(  # type: ignore[arg-type]
                QueryMembershipFilterV1(
                    left=QueryClaimValueRefV1(binding="item", predicate=STATUS_PREDICATE),
                    values=tuple(
                        sorted(
                            (
                                QueryLiteralRefV1(value="blocked"),
                                QueryLiteralRefV1(value="ready"),
                            ),
                            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
                        )
                    ),
                    value_type="string",
                ),
                QueryNegationFilterV1(
                    operand=QueryClaimPresenceFilterV1(binding="item", predicate=STATUS_PREDICATE),
                ),
            )
        ),
    )


def rank_query(direction: str) -> QueryDefinitionV1:
    return typed_item_query(
        RANK_PREDICATE,
        name="project.ranked_items",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=RANK_PREDICATE),
            operator="gte",
            right=QueryLiteralRefV1(value=1),
            value_type="integer",
        ),
        orderings=(
            QueryOrderingV1(
                key=QueryClaimValueRefV1(binding="item", predicate=RANK_PREDICATE),
                direction=direction,  # type: ignore[arg-type]
                value_type="integer",
            ),
        ),
    )


def rank_facts() -> ClaimQueryFactsV1:
    return facts(
        claim_fact(
            index,
            subject_row=subject("project.work_item", item),
            predicate=RANK_PREDICATE,
            obj=LiteralClaimObject(value=rank),
        )
        for index, (item, rank) in enumerate((("wi-1", 2), ("wi-2", 10), ("wi-3", 1)), start=1)
    )


def due_query() -> QueryDefinitionV1:
    return typed_item_query(
        DUE_PREDICATE,
        name="project.overdue_items",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=DUE_PREDICATE),
            operator="lt",
            right=QueryEvaluationTimeRefV1(),
            value_type="timestamp",
        ),
    )


def due_facts() -> ClaimQueryFactsV1:
    def due(index: int, item: str, moment: datetime) -> ClaimFactRowV1:
        return claim_fact(
            index,
            subject_row=subject("project.work_item", item),
            predicate=DUE_PREDICATE,
            obj=LiteralClaimObject(value=moment.isoformat().replace("+00:00", "Z")),
        )

    return facts((due(1, "wi-1", NOW - timedelta(days=2)), due(2, "wi-2", LATER)))


def amount_facts() -> ClaimQueryFactsV1:
    return facts(
        claim_fact(
            index,
            subject_row=subject("project.work_item", item),
            predicate=AMOUNT_PREDICATE,
            obj=LiteralClaimObject(value=value),
        )
        for index, (item, value) in enumerate((("wi-1", "9.25"), ("wi-2", "10.5")), start=1)
    )


def amount_query() -> QueryDefinitionV1:
    return typed_item_query(
        AMOUNT_PREDICATE,
        name="project.amounts",
        orderings=(
            QueryOrderingV1(
                key=QueryClaimValueRefV1(binding="item", predicate=AMOUNT_PREDICATE),
                direction="descending",
                value_type="decimal",
            ),
        ),
    )


def surfacing_collection_query() -> QueryDefinitionV1:
    return collection_status_query(
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )
    )


def subject_reference_query() -> QueryDefinitionV1:
    return typed_item_query(
        REVIEWER_PREDICATE,
        name="project.ada_items",
        object_kind="subject",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=REVIEWER_PREDICATE),
            operator="eq",
            right=QueryLiteralRefV1(value="Subject:project.person/ada"),
            value_type="subject_reference",
        ),
    )


# -- evaluation helpers ---------------------------------------------------


def evaluate(
    query: QueryDefinitionV1,
    fact_rows: ClaimQueryFactsV1,
    *,
    backend_factory: ClaimQueryBackendFactoryV1,
    evaluation_time: datetime = NOW,
    parameters: dict[str, object] | None = None,
    budgets: QueryBudgetsV1 | None = None,
) -> ClaimQueryResultV1:
    return evaluate_claim_query(
        accepted_query(query),
        facts=fact_rows,
        coordinate=fact_rows.coordinate,
        evaluation_time=evaluation_time,
        parameters=parameters,
        budgets=budgets,
        backend_factory=backend_factory,
    )


@dataclass(frozen=True)
class ParityCell:
    """One query, fact set, and read binding evaluated through both backends."""

    name: str
    query: QueryDefinitionV1
    facts: ClaimQueryFactsV1
    parameters: dict[str, object] | None = None
    budgets: QueryBudgetsV1 | None = None
    evaluation_time: datetime = NOW
    expect_refusal: str | None = None
    expect_clipped: tuple[str, ...] = ()
    expect_conflicts: bool = False


def parity_cells() -> tuple[ParityCell, ...]:
    """Every declared query shape, budget, conflict, and visibility window."""

    reviewers = all_reviewers_facts()
    competing = facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-1", "blocked")))
    single_reviewer = facts((status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada")))
    return (
        ParityCell("entry_only", all_items_query(), facts((status_claim(1, "wi-1", "ready"),))),
        ParityCell("entry_only_no_claims", all_items_query(), facts(())),
        ParityCell(
            "entry_subject_id_pin",
            single_status_query(),
            facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "blocked"))),
            parameters={"item_id": "wi-1"},
        ),
        ParityCell(
            "traversal_forward_with_include",
            active_work_query(),
            reviewers,
            parameters={"status": "ready"},
        ),
        ParityCell("traversal_reverse", reverse_traversal_query(), reviewers),
        ParityCell(
            "relation_claim_shape",
            active_work_query(result_shape="relation_claim", includes=(), orderings=()),
            single_reviewer,
            parameters={"status": "ready"},
        ),
        ParityCell(
            "optional_step_hidden_relation",
            active_work_query(
                traversal=(
                    active_work_query().traversal[0].model_copy(update={"required": False}),
                ),
                includes=(),
                orderings=(),
            ),
            facts(
                (
                    status_claim(1, "wi-1", "ready"),
                    reviewer_claim(3, "wi-1", "ada", supported=False),
                )
            ),
            parameters={"status": "ready"},
        ),
        ParityCell(
            "filter_membership_negation_disjunction",
            membership_query(),
            facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "done"))),
        ),
        ParityCell("filter_integer_descending", rank_query("descending"), rank_facts()),
        ParityCell("filter_integer_ascending", rank_query("ascending"), rank_facts()),
        ParityCell("ordering_decimal_descending", amount_query(), amount_facts()),
        ParityCell("filter_timestamp_at_now", due_query(), due_facts()),
        ParityCell("filter_timestamp_at_later", due_query(), due_facts(), evaluation_time=LATER),
        ParityCell(
            "filter_timestamp_after_both",
            due_query(),
            due_facts(),
            evaluation_time=LATER + timedelta(days=1),
        ),
        ParityCell("filter_subject_reference", subject_reference_query(), reviewers),
        ParityCell(
            "visibility_window_before",
            all_items_query(),
            window_facts(),
            expect_conflicts=False,
        ),
        ParityCell(
            "visibility_window_after",
            all_items_query(),
            window_facts(),
            evaluation_time=LATER,
        ),
        ParityCell(
            "visibility_window_traversal_after",
            active_work_query(),
            window_facts(),
            parameters={"status": "ready"},
            evaluation_time=LATER,
        ),
        ParityCell("conflict_surfaced", all_items_query(), competing, expect_conflicts=True),
        ParityCell(
            "conflict_refused",
            single_status_query(),
            competing,
            parameters={"item_id": "wi-1"},
            expect_refusal=CLAIM_CONFLICT,
        ),
        ParityCell(
            "result_cardinality_refused",
            collection_status_query(),
            facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "ready"))),
            expect_refusal=RESULT_CONFLICT,
        ),
        ParityCell(
            "result_cardinality_surfaced",
            surfacing_collection_query(),
            facts((status_claim(1, "wi-1", "ready"),)),
            expect_clipped=("max_results",),
            expect_conflicts=True,
        ),
        ParityCell(
            "budget_max_results",
            all_items_query(),
            facts(
                status_claim(index, item, "ready") for index, item in enumerate(WORK_ITEMS, start=1)
            ),
            budgets=QueryBudgetsV1(max_results=2, max_traversal_depth=0),
            expect_clipped=("max_results",),
        ),
        ParityCell(
            "budget_max_paths",
            active_work_query(includes=()),
            reviewers,
            parameters={"status": "ready"},
            budgets=QueryBudgetsV1(
                max_results=50,
                max_traversal_depth=2,
                max_paths=1,
                max_paths_per_result=1,
            ),
            expect_clipped=("max_paths",),
        ),
        ParityCell(
            "budget_max_paths_per_result",
            active_work_query(result_binding="item", result_shape="path", includes=()),
            reviewers,
            parameters={"status": "ready"},
            budgets=QueryBudgetsV1(
                max_results=50,
                max_traversal_depth=2,
                max_paths=200,
                max_paths_per_result=1,
            ),
            expect_clipped=("max_paths_per_result",),
        ),
        ParityCell(
            "budget_include_max_items",
            active_work_query(),
            facts(
                (
                    status_claim(1, "wi-1", "ready"),
                    status_claim(2, "wi-1", "ready"),
                    reviewer_claim(3, "wi-1", "ada"),
                )
            ),
            parameters={"status": "ready"},
            expect_clipped=("include_max_items",),
        ),
        ParityCell(
            "traversal_target_unresolved",
            active_work_query(includes=(), orderings=()),
            unresolved_target_facts(),
            parameters={"status": "ready"},
            expect_refusal=SUBJECT_UNRESOLVED,
        ),
        ParityCell(
            "traversal_object_not_subject",
            literal_traversal_query(),
            facts((status_claim(1, "wi-1", "ready"),)),
            expect_refusal=TRAVERSAL_OBJECT_NOT_SUBJECT,
        ),
        ParityCell(
            "claim_subject_absent",
            all_items_query(),
            unresolved_subject_facts(),
        ),
    )


PARITY_CELLS = parity_cells()


class PrimitiveOnlyBackend:
    """A backend exposing exactly the frozen surface and nothing else.

    An evaluation that succeeds through this object cannot have read accepted
    state by any route other than the five primitives and the bound coordinate.
    """

    def __init__(self, inner: DirectClaimFactIndex) -> None:
        self._inner = inner
        self.calls: set[str] = set()

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the evaluator read {name!r}, which is not a backend primitive")

    @property
    def coordinate(self) -> AcceptedProjectionCoordinate:
        return self._inner.coordinate

    def subjects(self, kinds: tuple[str, ...], *, subject_id: str | None = None) -> tuple[str, ...]:
        self.calls.add("subjects")
        return self._inner.subjects(kinds, subject_id=subject_id)

    def subject(self, artifact_path: str) -> AcceptedSubject | None:
        self.calls.add("subject")
        return self._inner.subject(artifact_path)

    def claims_on(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        self.calls.add("claims_on")
        return self._inner.claims_on(artifact_path, predicate)

    def claims_to(self, artifact_path: str, predicate: str) -> tuple[VisibleClaimRow, ...]:
        self.calls.add("claims_to")
        return self._inner.claims_to(artifact_path, predicate)

    def visibility(self, row: ClaimFactRowV1) -> VisibleClaimRow | None:
        self.calls.add("visibility")
        return self._inner.visibility(row)


@dataclass
class RecordingFactory:
    """Wrap the reference index so the evaluator sees the frozen surface only."""

    backends: list[PrimitiveOnlyBackend] = field(default_factory=list)

    def __call__(
        self,
        fact_rows: ClaimQueryFactsV1,
        *,
        definition: QueryDefinitionV1,
        evaluation_time: datetime,
    ) -> PrimitiveOnlyBackend:
        backend = PrimitiveOnlyBackend(
            DirectClaimFactIndex(fact_rows, definition=definition, evaluation_time=evaluation_time)
        )
        self.backends.append(backend)
        return backend


# -- the materialized Subject view ----------------------------------------


def test_the_materialized_view_is_a_pure_function_of_the_accepted_facts() -> None:
    fact_rows = all_reviewers_facts()

    first = subject_query_view(fact_rows)
    second = subject_query_view(fact_rows)

    assert first == second
    assert render_subject_query_view(first) == render_subject_query_view(second)
    assert subject_query_view_digest(first) == subject_query_view_digest(second)
    assert first.coordinate == fact_rows.coordinate
    assert [row.path for row in first.subjects] == sorted(
        (item.path for item in fact_rows.subjects),
        key=lambda item: item.encode("utf-8"),
    )
    assert [row.claim_path for row in first.claims] == sorted(
        (item.accepted.path for item in fact_rows.claims),
        key=lambda item: item.encode("utf-8"),
    )


def test_the_view_states_both_traversal_directions_in_claim_path_order() -> None:
    view = subject_query_view(all_reviewers_facts())
    item = subject_path("project.work_item", "wi-1")
    person = subject_path("project.person", "ada")

    asserted = next(
        row
        for row in view.adjacency
        if row.subject_path == item and row.predicate == REVIEWER_PREDICATE
    )
    incident = next(
        row
        for row in view.adjacency
        if row.subject_path == person and row.predicate == REVIEWER_PREDICATE
    )

    assert len(asserted.asserted_claim_paths) == 2
    assert asserted.incident_claim_paths == ()
    assert asserted.asserted_claim_paths == tuple(
        sorted(asserted.asserted_claim_paths, key=lambda value: value.encode("utf-8"))
    )
    assert len(incident.incident_claim_paths) == 2
    assert incident.asserted_claim_paths == ()


def test_a_claim_whose_subject_is_absent_is_never_materialized_or_visible() -> None:
    fact_rows = unresolved_subject_facts()
    view = subject_query_view(fact_rows)
    index = DirectClaimFactIndex(fact_rows, definition=all_items_query(), evaluation_time=NOW)

    assert fact_rows.claims != ()
    assert view.claims == ()
    assert view.adjacency == ()
    assert index.claims_on(subject_path("project.work_item", "wi-1"), STATUS_PREDICATE) == ()
    assert index.visibility(fact_rows.claims[0]) is None


def test_an_unresolved_relation_target_is_materialized_rather_than_dropped() -> None:
    view = subject_query_view(unresolved_target_facts())
    target = subject_path("project.person", "ada")

    assert target not in {row.path for row in view.subjects}
    incident = next(row for row in view.adjacency if row.subject_path == target)
    assert len(incident.incident_claim_paths) == 1


def test_the_view_carries_no_evaluation_time_verdict_or_conflict() -> None:
    fields = set(SubjectQueryViewV1.model_fields)

    assert fields == {"tag", "coordinate", "subjects", "claims", "adjacency"}
    assert not {"verdict", "currency", "evaluated_at", "conflicts"} & fields


# -- backend contract conformance -----------------------------------------



def test_the_evaluator_reads_state_only_through_the_frozen_backend_surface() -> None:
    fact_rows = all_reviewers_facts()
    factory = RecordingFactory()

    restricted = evaluate(
        active_work_query(),
        fact_rows,
        backend_factory=factory,
        parameters={"status": "ready"},
    )
    reference = evaluate(
        active_work_query(),
        fact_rows,
        backend_factory=DirectClaimFactIndex,
        parameters={"status": "ready"},
    )

    assert claim_query_result_digest(restricted) == claim_query_result_digest(reference)
    assert factory.backends[0].calls == {"subjects", "subject", "claims_on"}


# -- the parity matrix ----------------------------------------------------


def test_the_parity_matrix_covers_every_budget_refusal_and_result_shape() -> None:
    clipped = {budget for cell in PARITY_CELLS for budget in cell.expect_clipped}
    refusals = {cell.expect_refusal for cell in PARITY_CELLS if cell.expect_refusal is not None}
    shapes = {cell.query.result_shape for cell in PARITY_CELLS}
    times = {cell.evaluation_time for cell in PARITY_CELLS}

    assert clipped == {
        "include_max_items",
        "max_paths",
        "max_paths_per_result",
        "max_results",
    }
    assert refusals == {
        CLAIM_CONFLICT,
        RESULT_CONFLICT,
        SUBJECT_UNRESOLVED,
        TRAVERSAL_OBJECT_NOT_SUBJECT,
    }
    assert shapes == {"path", "relation_claim", "subject"}
    assert len(times) >= 3
    assert any(cell.expect_conflicts for cell in PARITY_CELLS)
