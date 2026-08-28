"""PC-F direct Claim-query evaluation: visibility, traversal, budgets, determinism."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import GenerationRoot, SemanticRoot, canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claim_verdicts import (
    ClaimAdjudicationRuleV1,
    claim_adjudication_rule,
)
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimArtifact,
    ClaimBacking,
    ClaimReferentContext,
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
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
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import (
    AcceptedSubject,
    SubjectShell,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.compiler import PC_E1_COMPILER
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.query.engine import (
    BUDGET_EXCEEDS_MAXIMUM,
    CLAIM_CONFLICT,
    COORDINATE_MISMATCH,
    EVALUATION_TIME_NOT_ABSOLUTE,
    PARAMETER_MISSING,
    PARAMETER_TYPE_MISMATCH,
    PARAMETER_UNDECLARED,
    RESULT_CONFLICT,
    VALUE_TYPE_MISMATCH,
    ClaimFactRowV1,
    ClaimQueryError,
    ClaimQueryFactsV1,
    ClaimQueryResultV1,
    claim_query_result_digest,
    evaluate_claim_query,
    query_attempted_parameter_digest,
    query_execution_receipt,
    query_parameter_digest,
    resolve_query_parameters,
)
from tests.test_playbill.test_query_definitions import (
    OWNER_AUTHORITY,
    REVIEWER_PREDICATE,
    STATUS_PREDICATE,
    accepted_query,
    active_work_query,
    claim_type,
    single_status_query,
)

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)
AUTHORITY_BASIS = ("authority:owner",)

WORK_ITEMS = ("wi-1", "wi-2", "wi-3")
PEOPLE = ("ada", "grace")

RANK_PREDICATE = "project.work_item.rank"
AMOUNT_PREDICATE = "project.work_item.amount"
DUE_PREDICATE = "project.work_item.due_at"


# -- accepted fact fixtures ----------------------------------------------


def subject(kind: str, identifier: str) -> AcceptedSubject:
    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{identifier}"),
        subject_kind=kind,
        subject_id=identifier,
        authority=OWNER_AUTHORITY,
    )
    return AcceptedSubject(
        path=subject_path(kind, identifier),
        shell=shell,
        artifact_digest=subject_digest(shell).tagged,
    )


def rule_for(predicate: str, *, object_kind: str = "literal") -> ClaimAdjudicationRuleV1:
    contract: ClaimType = claim_type(predicate, object_kind=object_kind)
    return claim_adjudication_rule(
        contract,
        claim_type_digest=claim_type_digest(contract).tagged,
    )


def claim_fact(
    index: int,
    *,
    subject_row: AcceptedSubject,
    predicate: str,
    obj: LiteralClaimObject | SubjectClaimObject,
    supported: bool = True,
    effective_from: datetime | None = None,
    effective_until: datetime | None = None,
) -> ClaimFactRowV1:
    object_kind = "subject" if isinstance(obj, SubjectClaimObject) else "literal"
    contract = claim_type(predicate, object_kind=object_kind)
    digest = claim_type_digest(contract).tagged
    claim_id = f"CLM-{index:032x}"
    artifact = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_row.path),
            claim_type=contract.identity,
            claim_type_digest=digest,
            predicate=predicate,
            object=obj,
            role="normative",
            effective_from=effective_from,
            effective_until=effective_until,
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_row.artifact_digest,
                observed_at=NOW,
            ),
        ),
        authority=OWNER_AUTHORITY,
        pins=(ArtifactPin(role="claim-type", target=contract.identity, artifact_digest=digest),),
    )
    return ClaimFactRowV1(
        accepted=AcceptedClaim(
            path=claim_path(claim_id),
            claim=artifact,
            statement_digest=claim_statement_digest(artifact.statement).tagged,
            artifact_digest=claim_artifact_digest(artifact).tagged,
        ),
        rule=rule_for(predicate, object_kind=object_kind),
        resolved_authority_basis=AUTHORITY_BASIS if supported else (),
    )


def coordinate(*, generation: str = "22") -> AcceptedProjectionCoordinate:
    return AcceptedProjectionCoordinate(
        instance_id="inst_claim_query",
        repository_path="/tmp/claim-query",
        git_object_format="sha1",
        git_oid="11" * 20,
        semantic_root=SemanticRoot("aa" * 32).tagged,
        generation_root=GenerationRoot(generation * 32).tagged,
        compiler=PC_E1_COMPILER,
    )


def facts(claims: tuple[ClaimFactRowV1, ...]) -> ClaimQueryFactsV1:
    subjects = tuple(
        sorted(
            (
                *(subject("project.work_item", item) for item in WORK_ITEMS),
                *(subject("project.person", item) for item in PEOPLE),
            ),
            key=lambda item: item.path.encode("utf-8"),
        )
    )
    ordered = tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8")))
    return ClaimQueryFactsV1(coordinate=coordinate(), subjects=subjects, claims=ordered)


def status_claim(index: int, item: str, value: str, **overrides: object) -> ClaimFactRowV1:
    return claim_fact(
        index,
        subject_row=subject("project.work_item", item),
        predicate=STATUS_PREDICATE,
        obj=LiteralClaimObject(value=value),
        **overrides,  # type: ignore[arg-type]
    )


def reviewer_claim(index: int, item: str, person: str, **overrides: object) -> ClaimFactRowV1:
    return claim_fact(
        index,
        subject_row=subject("project.work_item", item),
        predicate=REVIEWER_PREDICATE,
        obj=SubjectClaimObject(
            address=SemanticAddress.whole_artifact(subject_path("project.person", person))
        ),
        **overrides,  # type: ignore[arg-type]
    )


def run(
    query: QueryDefinitionV1,
    fact_rows: ClaimQueryFactsV1,
    *,
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
    )


def collection_status_query(**overrides: object) -> QueryDefinitionV1:
    """The one-cardinality Subject read without an entry Subject pin."""

    return single_status_query(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.only_work_item"),
        entry=QueryEntryV1(binding="item", subject_kinds=("project.work_item",)),
        parameters=(),
        **overrides,
    )


def claim_type_pin_for(predicate: str, *, object_kind: str = "literal") -> ArtifactPin:
    contract = claim_type(predicate, object_kind=object_kind)
    return ArtifactPin(
        role="claim-type",
        target=contract.identity,
        artifact_digest=claim_type_digest(contract).tagged,
    )


def sorted_operands(*filters: object) -> tuple[object, ...]:
    """Return grammar operands in their required canonical byte order."""

    return tuple(
        sorted(filters, key=lambda item: canonical_bytes(item.model_dump(mode="json")))  # type: ignore[attr-defined]
    )


def typed_item_query(
    predicate: str,
    *,
    name: str,
    object_kind: str = "literal",
    where: object = None,
    orderings: tuple[QueryOrderingV1, ...] = (),
) -> QueryDefinitionV1:
    """One single-predicate Subject read used to exercise the typed grammar."""

    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        entry=QueryEntryV1(binding="item", subject_kinds=("project.work_item",)),
        where=where,  # type: ignore[arg-type]
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
                QueryProjectionFieldV1(
                    name="value",
                    value=QueryClaimValueRefV1(binding="item", predicate=predicate),
                ),
            )
        ),
        orderings=orderings,
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgetsV1(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=50, max_traversal_depth=0),
        authority=OWNER_AUTHORITY,
        pins=(claim_type_pin_for(predicate, object_kind=object_kind),),
    )


def all_items_query(**overrides: object) -> QueryDefinitionV1:
    """A many-cardinality Subject read over every work item."""

    fields: dict[str, object] = {
        "identity": ArtifactIdentity(kind="QueryDefinition", name="project.work_items"),
        "entry": QueryEntryV1(binding="item", subject_kinds=("project.work_item",)),
        "result_binding": "item",
        "result_shape": "subject",
        "result_cardinality": "many",
        "dedupe": "subject",
        "projection": QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
                QueryProjectionFieldV1(
                    name="status",
                    value=QueryClaimValueRefV1(binding="item", predicate=STATUS_PREDICATE),
                ),
            )
        ),
        "orderings": (
            QueryOrderingV1(
                key=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                value_type="string",
            ),
        ),
        "evaluation_policy": QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        "default_budgets": QueryBudgetsV1(max_results=10, max_traversal_depth=0),
        "maximum_budgets": QueryBudgetsV1(max_results=50, max_traversal_depth=0),
        "authority": OWNER_AUTHORITY,
        "pins": (
            ArtifactPin(
                role="claim-type",
                target=ArtifactIdentity(kind="ClaimType", name=STATUS_PREDICATE),
                artifact_digest=claim_type_digest(claim_type(STATUS_PREDICATE)).tagged,
            ),
        ),
    }
    fields.update(overrides)
    return QueryDefinitionV1(**fields)  # type: ignore[arg-type]


# -- scalar Claim reads ---------------------------------------------------


def test_scalar_claim_read_projects_the_object_and_states_why_the_row_is_present() -> None:
    result = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "blocked"))),
        parameters={"item_id": "wi-1"},
    )

    assert result.verdict == "completed"
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.result_subject_identity == "Subject:project.work_item/wi-1"
    assert [(item.name, item.state, item.value) for item in row.fields] == [
        ("status", "present", "ready")
    ]
    assert [(item.predicate, item.verdict, item.currency) for item in row.read_claims] == [
        (STATUS_PREDICATE, "supported", "current")
    ]
    assert result.truncation.clipped_budgets == ()
    assert result.truncation.candidate_result_count == 1


def test_a_claim_outside_the_verdict_policy_is_not_a_visible_row_value() -> None:
    result = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready", supported=False),)),
        parameters={"item_id": "wi-1"},
    )

    assert result.verdict == "completed"
    assert [(item.name, item.state, item.value) for item in result.rows[0].fields] == [
        ("status", "absent", None)
    ]
    assert result.rows[0].read_claims == ()


def test_missing_undeclared_and_mistyped_parameters_refuse_fail_closed() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"),))

    assert run(single_status_query(), fact_rows).refusal is not None
    assert run(single_status_query(), fact_rows).refusal.code == PARAMETER_MISSING  # type: ignore[union-attr]
    undeclared = run(
        single_status_query(),
        fact_rows,
        parameters={"item_id": "wi-1", "other": "x"},
    )
    assert undeclared.refusal is not None
    assert undeclared.refusal.code == PARAMETER_UNDECLARED
    mistyped = run(single_status_query(), fact_rows, parameters={"item_id": 7})
    assert mistyped.refusal is not None
    assert mistyped.refusal.code == PARAMETER_TYPE_MISMATCH
    assert mistyped.rows == ()


# -- relationship Claim traversal ----------------------------------------


def test_relationship_traversal_binds_targets_filters_and_hydrates_includes() -> None:
    result = run(
        active_work_query(),
        facts(
            (
                status_claim(1, "wi-1", "ready"),
                status_claim(2, "wi-2", "blocked"),
                reviewer_claim(3, "wi-1", "ada"),
                reviewer_claim(4, "wi-1", "grace"),
                reviewer_claim(5, "wi-2", "ada"),
            )
        ),
        parameters={"status": "ready"},
    )

    assert result.verdict == "completed"
    assert [row.result_subject_identity for row in result.rows] == [
        "Subject:project.person/ada",
        "Subject:project.person/grace",
    ]
    assert [tuple(item.value for item in row.fields) for row in result.rows] == [
        ("wi-1", "ada"),
        ("wi-1", "grace"),
    ]
    assert all(len(row.path) == 1 for row in result.rows)
    assert all(row.path[0].predicate == REVIEWER_PREDICATE for row in result.rows)
    statuses = [row.includes[0] for row in result.rows]
    assert all(item.name == "status" for item in statuses)
    assert all(item.items[0].claim_object["value"] == "ready" for item in statuses)  # type: ignore[index]
    assert all(item.items[0].subject_identity is None for item in statuses)
    assert result.truncation.evaluated_path_count == 2
    assert result.truncation.retained_path_count == 2


def test_reverse_traversal_reaches_the_relation_claim_subject_side() -> None:
    query = active_work_query(
        identity=ArtifactIdentity(kind="QueryDefinition", name="project.reviewer_work"),
        entry=QueryEntryV1(binding="reviewer", subject_kinds=("project.person",)),
        traversal=(
            active_work_query()
            .traversal[0]
            .model_copy(
                update={
                    "binding": "item",
                    "from_binding": "reviewer",
                    "direction": "reverse",
                    "target_subject_kinds": ("project.work_item",),
                }
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
    result = run(
        query,
        facts((reviewer_claim(3, "wi-1", "ada"), reviewer_claim(4, "wi-2", "ada"))),
    )

    assert result.verdict == "completed"
    assert [row.result_subject_identity for row in result.rows] == [
        "Subject:project.work_item/wi-1",
        "Subject:project.work_item/wi-2",
    ]
    assert [row.bindings[0].subject_id for row in result.rows] == ["ada", "ada"]


def test_relation_claim_shape_names_the_exact_traversed_claim() -> None:
    query = active_work_query(result_shape="relation_claim", includes=(), orderings=())
    result = run(
        query,
        facts((status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada"))),
        parameters={"status": "ready"},
    )

    assert len(result.rows) == 1
    relation = result.rows[0].relation_claim
    assert relation is not None
    assert relation.predicate == REVIEWER_PREDICATE
    assert relation.subject_identity == "Subject:project.work_item/wi-1"
    assert relation.verdict == "supported"


def test_a_hidden_relation_claim_removes_its_edge_without_removing_the_entry_row() -> None:
    optional = active_work_query().traversal[0].model_copy(update={"required": False})
    query = active_work_query(traversal=(optional,), includes=(), orderings=())
    result = run(
        query,
        facts(
            (
                status_claim(1, "wi-1", "ready"),
                reviewer_claim(3, "wi-1", "ada", supported=False),
            )
        ),
        parameters={"status": "ready"},
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.bindings[1].subject_identity is None
    assert row.result_subject_identity is None
    assert [(item.name, item.state) for item in row.fields] == [
        ("item_id", "present"),
        ("reviewer_id", "absent"),
    ]


# -- cardinality and conflict --------------------------------------------


def test_refuse_on_conflict_names_the_competing_statement_digests() -> None:
    competing = (status_claim(1, "wi-1", "ready"), status_claim(2, "wi-1", "blocked"))
    result = run(
        single_status_query(),
        facts(competing),
        parameters={"item_id": "wi-1"},
    )

    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == CLAIM_CONFLICT
    assert result.refusal.statement_digests == tuple(
        sorted(item.accepted.statement_digest for item in competing)
    )
    assert result.refusal.subject_identities == ("Subject:project.work_item/wi-1",)
    assert result.rows == ()


def test_surface_conflicts_reports_the_conflict_set_and_never_picks_a_winner() -> None:
    competing = (status_claim(1, "wi-1", "ready"), status_claim(2, "wi-1", "blocked"))
    result = run(all_items_query(), facts(competing))

    assert result.verdict == "completed"
    conflicted = next(
        row
        for row in result.rows
        if row.result_subject_identity.endswith("wi-1")  # type: ignore[union-attr]
    )
    assert [(item.name, item.state, item.value) for item in conflicted.fields] == [
        ("item_id", "present", "wi-1"),
        ("status", "conflict", None),
    ]
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind == "claim_object"
    assert conflict.predicate == STATUS_PREDICATE
    assert conflict.statement_digests == tuple(
        sorted(item.accepted.statement_digest for item in competing)
    )


def test_agreeing_accepted_claims_are_not_a_conflict() -> None:
    result = run(
        all_items_query(),
        facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-1", "ready"))),
    )

    assert result.conflicts == ()
    row = next(item for item in result.rows if item.result_subject_identity.endswith("wi-1"))  # type: ignore[union-attr]
    assert [item.value for item in row.fields] == ["wi-1", "ready"]
    assert len(row.read_claims) == 2


def test_a_one_cardinality_query_refuses_rather_than_returning_one_of_many_rows() -> None:
    result = run(
        collection_status_query(),
        facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "ready"))),
    )

    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == RESULT_CONFLICT
    assert result.refusal.subject_identities == (
        "Subject:project.work_item/wi-1",
        "Subject:project.work_item/wi-2",
        "Subject:project.work_item/wi-3",
    )


def test_a_surfacing_one_cardinality_query_states_the_competing_rows_and_the_clip() -> None:
    query = collection_status_query(
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        )
    )
    result = run(query, facts((status_claim(1, "wi-1", "ready"),)))

    assert result.verdict == "completed"
    assert len(result.rows) == 1
    assert result.truncation.clipped_budgets == ("max_results",)
    assert result.truncation.candidate_result_count == 3
    assert result.conflicts[0].kind == "result_cardinality"
    assert len(result.conflicts[0].subject_identities) == 3


# -- explicit evaluation time and expiry ----------------------------------


def test_visibility_follows_the_explicit_evaluation_time_in_both_directions() -> None:
    fact_rows = facts(
        (
            status_claim(1, "wi-1", "ready", effective_from=LATER),
            status_claim(2, "wi-2", "blocked", effective_until=LATER),
        )
    )

    early = run(all_items_query(), fact_rows, evaluation_time=NOW)
    late = run(all_items_query(), fact_rows, evaluation_time=LATER)

    def status_of(result: ClaimQueryResultV1, item: str) -> tuple[str, object]:
        row = next(
            entry
            for entry in result.rows
            if entry.result_subject_identity.endswith(item)  # type: ignore[union-attr]
        )
        field = next(entry for entry in row.fields if entry.name == "status")
        return field.state, field.value

    assert status_of(early, "wi-1") == ("absent", None)
    assert status_of(early, "wi-2") == ("present", "blocked")
    assert status_of(late, "wi-1") == ("present", "ready")
    assert status_of(late, "wi-2") == ("absent", None)


def test_a_declared_result_expiry_is_derived_from_the_evaluation_time() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada")))
    result = run(active_work_query(), fact_rows, parameters={"status": "ready"})

    assert result.evaluated_at == NOW
    assert result.expires_at == NOW + timedelta(hours=1)
    assert run(all_items_query(), fact_rows).expires_at is None


def test_a_naive_evaluation_time_and_a_foreign_coordinate_refuse() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"),))
    with pytest.raises(ClaimQueryError) as naive:
        evaluate_claim_query(
            accepted_query(all_items_query()),
            facts=fact_rows,
            coordinate=fact_rows.coordinate,
            evaluation_time=datetime(2026, 8, 16, 12, 0),
        )
    foreign = evaluate_claim_query(
        accepted_query(all_items_query()),
        facts=fact_rows,
        coordinate=coordinate(generation="33"),
        evaluation_time=NOW,
    )

    assert naive.value.code == EVALUATION_TIME_NOT_ABSOLUTE
    assert foreign.refusal is not None
    assert foreign.refusal.code == COORDINATE_MISMATCH


# -- pagination, truncation, and path budgets -----------------------------


def test_a_clipped_result_budget_is_always_named_in_the_truncation_accounting() -> None:
    fact_rows = facts(
        (status_claim(index, item, "ready") for index, item in enumerate(WORK_ITEMS, start=1))
    )
    result = run(
        all_items_query(),
        fact_rows,
        budgets=QueryBudgetsV1(max_results=2, max_traversal_depth=0),
    )

    assert result.truncation.clipped_budgets == ("max_results",)
    assert result.truncation.candidate_result_count == 3
    assert result.truncation.returned_result_count == 2
    assert result.truncation.truncated is True
    assert [row.fields[0].value for row in result.rows] == ["wi-1", "wi-2"]


def test_path_budgets_clip_traversal_and_per_result_fan_out_explicitly() -> None:
    fact_rows = facts(
        (
            status_claim(1, "wi-1", "ready"),
            reviewer_claim(3, "wi-1", "ada"),
            reviewer_claim(4, "wi-1", "grace"),
        )
    )
    per_result = run(
        active_work_query(result_binding="item", result_shape="path", includes=()),
        fact_rows,
        parameters={"status": "ready"},
        budgets=QueryBudgetsV1(
            max_results=50,
            max_traversal_depth=2,
            max_paths=200,
            max_paths_per_result=1,
        ),
    )
    traversal = run(
        active_work_query(includes=()),
        fact_rows,
        parameters={"status": "ready"},
        budgets=QueryBudgetsV1(
            max_results=50,
            max_traversal_depth=2,
            max_paths=1,
            max_paths_per_result=1,
        ),
    )

    assert per_result.truncation.clipped_budgets == ("max_paths_per_result",)
    assert per_result.truncation.evaluated_path_count == 2
    assert per_result.truncation.retained_path_count == 1
    assert traversal.truncation.clipped_budgets == ("max_paths",)
    assert traversal.truncation.evaluated_path_count == 1
    assert len(traversal.rows) == 1


def test_a_clipped_include_names_itself_in_the_truncation_accounting() -> None:
    result = run(
        active_work_query(),
        facts(
            (
                status_claim(1, "wi-1", "ready"),
                status_claim(2, "wi-1", "ready"),
                reviewer_claim(3, "wi-1", "ada"),
            )
        ),
        parameters={"status": "ready"},
    )

    include = result.rows[0].includes[0]
    assert include.candidate_count == 2
    assert len(include.items) == 1
    assert include.truncated is True
    assert result.truncation.clipped_budgets == ("include_max_items",)
    assert result.truncation.truncated_includes == ("status",)


def test_a_caller_budget_above_the_declared_ceiling_refuses() -> None:
    result = run(
        all_items_query(),
        facts((status_claim(1, "wi-1", "ready"),)),
        budgets=QueryBudgetsV1(max_results=500, max_traversal_depth=0),
    )

    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == BUDGET_EXCEEDS_MAXIMUM


def test_declared_ordering_is_total_and_descending_reverses_only_present_keys() -> None:
    fact_rows = facts(
        (
            status_claim(1, "wi-1", "blocked"),
            status_claim(2, "wi-3", "ready"),
        )
    )
    ascending = run(all_items_query(), fact_rows)
    descending = run(
        all_items_query(
            orderings=(
                QueryOrderingV1(
                    key=QueryClaimValueRefV1(binding="item", predicate=STATUS_PREDICATE),
                    direction="descending",
                    value_type="string",
                ),
            )
        ),
        fact_rows,
    )

    assert [row.fields[0].value for row in ascending.rows] == ["wi-1", "wi-2", "wi-3"]
    assert [row.fields[0].value for row in descending.rows] == ["wi-3", "wi-1", "wi-2"]


# -- the typed grammar ----------------------------------------------------


def test_integer_comparison_and_ordering_use_numbers_not_canonical_bytes() -> None:
    query = typed_item_query(
        RANK_PREDICATE,
        name="project.ranked_items",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=RANK_PREDICATE),
            operator="gte",
            right=QueryLiteralRefV1(value=2),
            value_type="integer",
        ),
        orderings=(
            QueryOrderingV1(
                key=QueryClaimValueRefV1(binding="item", predicate=RANK_PREDICATE),
                direction="descending",
                value_type="integer",
            ),
        ),
    )
    result = run(
        query,
        facts(
            (
                claim_fact(
                    index,
                    subject_row=subject("project.work_item", item),
                    predicate=RANK_PREDICATE,
                    obj=LiteralClaimObject(value=rank),
                )
                for index, (item, rank) in enumerate(
                    (("wi-1", 2), ("wi-2", 10), ("wi-3", 1)), start=1
                )
            )
        ),
    )

    assert [(row.fields[0].value, row.fields[1].value) for row in result.rows] == [
        ("wi-2", 10),
        ("wi-1", 2),
    ]


def test_decimal_ordering_compares_magnitudes_and_a_mistyped_object_refuses() -> None:
    ordered = typed_item_query(
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

    def amount(index: int, item: str, value: str) -> ClaimFactRowV1:
        return claim_fact(
            index,
            subject_row=subject("project.work_item", item),
            predicate=AMOUNT_PREDICATE,
            obj=LiteralClaimObject(value=value),
        )

    result = run(ordered, facts((amount(1, "wi-1", "9.25"), amount(2, "wi-2", "10.5"))))
    mistyped = run(ordered, facts((amount(1, "wi-1", "not-a-number"),)))

    assert [row.fields[0].value for row in result.rows] == ["wi-2", "wi-1", "wi-3"]
    assert mistyped.verdict == "refused"
    assert mistyped.refusal is not None
    assert mistyped.refusal.code == VALUE_TYPE_MISMATCH


def test_a_timestamp_filter_reads_the_explicit_evaluation_time_reference() -> None:
    query = typed_item_query(
        DUE_PREDICATE,
        name="project.overdue_items",
        where=QueryComparisonFilterV1(
            left=QueryClaimValueRefV1(binding="item", predicate=DUE_PREDICATE),
            operator="lt",
            right=QueryEvaluationTimeRefV1(),
            value_type="timestamp",
        ),
    )

    def due(index: int, item: str, moment: datetime) -> ClaimFactRowV1:
        return claim_fact(
            index,
            subject_row=subject("project.work_item", item),
            predicate=DUE_PREDICATE,
            obj=LiteralClaimObject(value=moment.isoformat().replace("+00:00", "Z")),
        )

    fact_rows = facts((due(1, "wi-1", NOW - timedelta(days=2)), due(2, "wi-2", LATER)))

    assert [row.fields[0].value for row in run(query, fact_rows).rows] == ["wi-1"]
    assert [row.fields[0].value for row in run(query, fact_rows, evaluation_time=LATER).rows] == [
        "wi-1"
    ]
    assert [
        row.fields[0].value
        for row in run(query, fact_rows, evaluation_time=LATER + timedelta(days=1)).rows
    ] == ["wi-1", "wi-2"]


def test_membership_negation_and_disjunction_narrow_without_widening() -> None:
    query = typed_item_query(
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
    result = run(
        query,
        facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "done"))),
    )

    assert [row.fields[0].value for row in result.rows] == ["wi-1", "wi-3"]
    assert [row.fields[1].state for row in result.rows] == ["present", "absent"]


def test_a_subject_reference_comparison_reads_the_related_subject_identity() -> None:
    query = typed_item_query(
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
    result = run(
        query,
        facts((reviewer_claim(1, "wi-1", "ada"), reviewer_claim(2, "wi-2", "grace"))),
    )

    assert [row.fields[0].value for row in result.rows] == ["wi-1"]
    assert result.rows[0].fields[1].value == "Subject:project.person/ada"


# -- determinism and receipts ---------------------------------------------


def test_the_same_inputs_produce_byte_identical_results_including_truncation() -> None:
    fact_rows = facts(
        (
            status_claim(1, "wi-1", "ready"),
            status_claim(2, "wi-2", "ready"),
            reviewer_claim(3, "wi-1", "ada"),
            reviewer_claim(4, "wi-1", "grace"),
            reviewer_claim(5, "wi-2", "ada"),
        )
    )
    budgets = QueryBudgetsV1(
        max_results=2,
        max_traversal_depth=2,
        max_paths=200,
        max_paths_per_result=5,
    )
    first = run(active_work_query(), fact_rows, parameters={"status": "ready"}, budgets=budgets)
    second = run(active_work_query(), fact_rows, parameters={"status": "ready"}, budgets=budgets)

    assert canonical_bytes(first.model_dump(mode="json")) == canonical_bytes(
        second.model_dump(mode="json")
    )
    assert claim_query_result_digest(first) == claim_query_result_digest(second)
    assert first.truncation.clipped_budgets == ("max_results",)


def test_a_different_evaluation_time_or_parameter_changes_the_result_digest() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada")))
    base = run(active_work_query(), fact_rows, parameters={"status": "ready"})
    shifted = run(
        active_work_query(),
        fact_rows,
        evaluation_time=LATER,
        parameters={"status": "ready"},
    )
    other = run(active_work_query(), fact_rows, parameters={"status": "blocked"})

    assert claim_query_result_digest(base) != claim_query_result_digest(shifted)
    assert claim_query_result_digest(base) != claim_query_result_digest(other)
    assert base.parameter_digest != other.parameter_digest


def test_the_execution_receipt_pins_definition_parameters_coordinate_and_truncation() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"), reviewer_claim(3, "wi-1", "ada")))
    result = run(active_work_query(), fact_rows, parameters={"status": "ready"})
    receipt = query_execution_receipt(result)

    assert receipt.definition_path == result.definition_path
    assert receipt.definition_digest == result.definition_digest
    assert receipt.parameter_digest == result.parameter_digest
    assert receipt.coordinate == fact_rows.coordinate
    assert receipt.evaluation_time == NOW
    assert receipt.budgets == active_work_query().default_budgets
    assert receipt.truncation == result.truncation
    assert receipt.verdict == "completed"
    assert receipt.refusal_code is None
    assert receipt.result_digest == claim_query_result_digest(result)


def test_a_refusal_after_binding_commits_the_resolved_parameters_it_bound() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"),))
    over_ceiling = QueryBudgetsV1(max_results=500, max_traversal_depth=0)
    first = run(
        single_status_query(),
        fact_rows,
        parameters={"item_id": "wi-1"},
        budgets=over_ceiling,
    )
    second = run(
        single_status_query(),
        fact_rows,
        parameters={"item_id": "wi-2"},
        budgets=over_ceiling,
    )

    assert first.refusal is not None and first.refusal.code == BUDGET_EXCEEDS_MAXIMUM
    assert second.refusal is not None and second.refusal.code == BUDGET_EXCEEDS_MAXIMUM
    assert first.parameter_digest == query_parameter_digest(
        resolve_query_parameters(single_status_query(), {"item_id": "wi-1"})
    )
    assert second.parameter_digest == query_parameter_digest(
        resolve_query_parameters(single_status_query(), {"item_id": "wi-2"})
    )
    assert first.parameter_digest != second.parameter_digest
    assert query_execution_receipt(first).parameter_digest == first.parameter_digest
    assert query_execution_receipt(second).parameter_digest == second.parameter_digest


def test_a_refusal_before_binding_never_borrows_the_empty_binding_digest() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"), status_claim(2, "wi-2", "ready")))
    parameterless = run(collection_status_query(), fact_rows)
    unbound = run(single_status_query(), fact_rows)
    undeclared = run(
        single_status_query(),
        fact_rows,
        parameters={"item_id": "wi-1", "other": "x"},
    )

    assert parameterless.refusal is not None and parameterless.refusal.code == RESULT_CONFLICT
    assert parameterless.parameter_digest == query_parameter_digest(())
    assert unbound.refusal is not None and unbound.refusal.code == PARAMETER_MISSING
    assert unbound.parameter_digest == query_attempted_parameter_digest(None)
    assert unbound.parameter_digest != parameterless.parameter_digest
    assert undeclared.refusal is not None and undeclared.refusal.code == PARAMETER_UNDECLARED
    assert undeclared.parameter_digest == query_attempted_parameter_digest(
        {"item_id": "wi-1", "other": "x"}
    )
    assert undeclared.parameter_digest not in {
        unbound.parameter_digest,
        parameterless.parameter_digest,
        query_parameter_digest(
            resolve_query_parameters(single_status_query(), {"item_id": "wi-1"})
        ),
    }
    assert query_execution_receipt(unbound).parameter_digest == unbound.parameter_digest


def test_an_attempted_parameter_outside_the_canonical_value_set_still_refuses() -> None:
    fact_rows = facts((status_claim(1, "wi-1", "ready"),))
    foreign = evaluate_claim_query(
        accepted_query(single_status_query()),
        facts=fact_rows,
        coordinate=coordinate(generation="33"),
        evaluation_time=NOW,
        parameters={"item_id": 1.5},
    )

    assert foreign.refusal is not None and foreign.refusal.code == COORDINATE_MISMATCH
    assert foreign.parameter_digest == query_attempted_parameter_digest({"item_id": 1.5})
    assert foreign.parameter_digest != query_attempted_parameter_digest({"item_id": "1.5"})


def test_a_refused_execution_receipt_names_its_refusal_code() -> None:
    result = run(single_status_query(), facts((status_claim(1, "wi-1", "ready"),)))
    receipt = query_execution_receipt(result)

    assert receipt.verdict == "refused"
    assert receipt.refusal_code == PARAMETER_MISSING


def test_a_result_can_neither_hide_a_refusal_nor_hide_a_clipping_budget() -> None:
    completed = run(all_items_query(), facts((status_claim(1, "wi-1", "ready"),)))
    clipped = run(
        all_items_query(),
        facts((status_claim(1, "wi-1", "ready"),)),
        budgets=QueryBudgetsV1(max_results=1, max_traversal_depth=0),
    )

    assert clipped.truncation.clipped_budgets == ("max_results",)
    with pytest.raises(ValueError, match="refused exactly when"):
        ClaimQueryResultV1.model_validate(
            {**completed.model_dump(mode="json"), "verdict": "refused"}
        )
    with pytest.raises(ValueError, match="agree with the returned row count"):
        ClaimQueryResultV1.model_validate(
            {
                **clipped.model_dump(mode="json"),
                "truncation": {
                    **clipped.truncation.model_dump(mode="json"),
                    "clipped_budgets": [],
                },
            }
        )


def test_a_hidden_claim_is_counted_so_absence_and_invisibility_differ() -> None:
    """A projected field reads "absent" either way; the advisory says which it was."""
    result = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready", supported=False),)),
        parameters={"item_id": "wi-1"},
    )

    assert [(item.name, item.state) for item in result.rows[0].fields] == [("status", "absent")]
    visibility = result.verdict_visibility
    assert visibility is not None
    assert visibility.excluded_claim_count == 1
    assert [
        (item.verdict, item.excluded_claim_count) for item in visibility.excluded_by_verdict
    ] == [("uncovered", 1)]
    assert visibility.visible_verdicts == ("supported",)


def test_a_query_that_hides_nothing_carries_no_visibility_advisory() -> None:
    result = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready"),)),
        parameters={"item_id": "wi-1"},
    )

    assert result.verdict_visibility is None


def test_the_result_preimage_carries_no_verdict_visibility_advisory() -> None:
    """The advisory reports what was not read, so it cannot move the digest."""
    hidden = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready", supported=False),)),
        parameters={"item_id": "wi-1"},
    )

    assert "verdict_visibility" not in _digest_preimage(hidden)
    assert hidden.verdict_visibility is not None
    without = hidden.model_copy(update={"verdict_visibility": None})
    assert claim_query_result_digest(without) == claim_query_result_digest(hidden)


def _digest_preimage(result: ClaimQueryResultV1) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("verdict_visibility")
    return payload


def test_the_advisory_counts_only_what_this_query_declined_to_read() -> None:
    """A hidden Claim on a Subject this query never asked about is not its exclusion."""
    result = run(
        single_status_query(),
        facts(
            (
                status_claim(1, "wi-1", "ready", supported=False),
                status_claim(2, "wi-2", "ready", supported=False),
            )
        ),
        parameters={"item_id": "wi-1"},
    )

    visibility = result.verdict_visibility
    assert visibility is not None
    # wi-2 is hidden too, but this evaluation only ever read wi-1's slot.
    assert visibility.excluded_claim_count == 1
    assert [item.excluded_claim_count for item in visibility.excluded_by_verdict] == [1]


def test_a_recorded_exclusion_reason_is_never_itself_visible() -> None:
    """The comparison and the record use one verdict form, so they cannot disagree."""
    result = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready", supported=False),)),
        parameters={"item_id": "wi-1"},
    )

    visibility = result.verdict_visibility
    assert visibility is not None
    for exclusion in visibility.excluded_by_verdict:
        assert exclusion.verdict not in visibility.visible_verdicts


def test_the_state_tap_value_is_identical_whether_or_not_rows_were_hidden() -> None:
    """A hidden row must not change a Procedure run's identity.

    The state-tap value feeds run_value_digest, which admits and replays the
    run. The advisory is deliberately outside the query's own result digest;
    this keeps it outside the run's too.
    """
    from cruxible_core.service.playbill_procedures import state_tap_value

    hidden = run(
        single_status_query(),
        facts((status_claim(1, "wi-1", "ready", supported=False),)),
        parameters={"item_id": "wi-1"},
    )
    assert hidden.verdict_visibility is not None

    without = hidden.model_copy(update={"verdict_visibility": None})

    assert state_tap_value(hidden) == state_tap_value(without)
    assert "verdict_visibility" not in state_tap_value(hidden)
