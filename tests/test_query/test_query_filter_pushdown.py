"""Indexed selections answer byte-for-byte as the unpruned accepted-facts oracle."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryConjunctionFilter,
    QueryDisjunctionFilter,
    QueryEntry,
    QueryLiteralRef,
    QueryMembershipFilter,
    QueryNegationFilter,
    QueryOrdering,
    QueryParameterDeclaration,
    QueryParameterRef,
    QuerySubjectFieldRef,
)
from cruxible_core.indexes.typed_state import schema_sql
from cruxible_core.service.discovery import query as served
from cruxible_core.service.discovery.compact_query import _accepted
from cruxible_core.service.discovery.query_pushdown import select_query_facts
from tests.core_support._knowledge_loop_support import (
    EVALUATION_TIME,
    PREDICATE,
    SUBJECT_KIND,
    seed_claims,
    work_item_query,
)
from tests.core_support._relation_query_support import BATCH_KIND, seed_relations

WHEN = datetime.fromisoformat(EVALUATION_TIME)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Any:
    instance, owner = seed_claims(tmp_path_factory.mktemp("pushdown"))
    seed_relations(instance, owner)
    return instance


def _eq(ref: Any, value: Any, value_type: str = "string") -> QueryComparisonFilter:
    return QueryComparisonFilter(
        left=ref, operator="eq", right=QueryLiteralRef(value=value), value_type=value_type
    )


def _combine(model: Any, filters: list[Any]) -> Any:
    filters = list(
        {canonical_bytes(item.model_dump(mode="json")): item for item in filters}.values()
    )
    if len(filters) == 1:
        return filters[0]
    return model(
        filters=tuple(
            sorted(filters, key=lambda item: canonical_bytes(item.model_dump(mode="json")))
        )
    )


def _compare(world: Any, query: Any, *, parameters: Any = None, budgets: Any = None) -> Any:
    definition = _accepted(query)
    coordinate = world.accepted_coordinate()
    whole = served.build_accepted_query_facts(world, coordinate=coordinate)
    actual = served.evaluate_accepted_query(
        world,
        definition,
        coordinate=coordinate,
        evaluation_time=WHEN,
        parameters=parameters,
        budgets=budgets,
    )
    expected = served.evaluate_accepted_query(
        world,
        definition,
        coordinate=coordinate,
        evaluation_time=WHEN,
        parameters=parameters,
        budgets=budgets,
        facts=lambda: whole,
    )
    assert canonical_bytes(actual.model_dump(mode="json")) == canonical_bytes(
        expected.model_dump(mode="json")
    )
    return actual


def test_randomized_filters_parameters_ordering_and_absence_match_full_load(world: Any) -> None:
    rng = random.Random(1016)
    base = work_item_query()
    base = base.model_copy(
        update={
            "entry": QueryEntry(binding="item", subject_kinds=(BATCH_KIND, SUBJECT_KIND)),
            "evaluation_policy": base.evaluation_policy.model_copy(
                update={
                    "visible_verdicts": (
                        "contradicted",
                        "stale",
                        "supported",
                        "uncovered",
                        "unresolved",
                    ),
                }
            ),
        }
    )
    identifier = QuerySubjectFieldRef(binding="item", field="subject_id")
    kind = QuerySubjectFieldRef(binding="item", field="subject_kind")
    status = QueryClaimValueRef(binding="item", predicate=PREDICATE)
    for _ in range(48):
        value = rng.choice(["wi-42", "wi-43", "b-1", "absent"])
        status_value = rng.choice(["ready", "blocked", "done"])
        filters = [
            _eq(identifier, value),
            _eq(status, status_value),
            QueryComparisonFilter(
                left=kind,
                operator=rng.choice(["eq", "gte", "lt"]),
                right=QueryLiteralRef(value=rng.choice([SUBJECT_KIND, BATCH_KIND])),
                value_type="string",
            ),
            QueryMembershipFilter(
                left=identifier,
                values=tuple(
                    sorted(
                        [QueryLiteralRef(value=value), QueryLiteralRef(value="b-2")],
                        key=lambda item: canonical_bytes(item.model_dump(mode="json")),
                    )
                ),
                value_type="string",
            ),
            QueryClaimPresenceFilter(binding="item", predicate=PREDICATE, negated=True),
            QueryNegationFilter(operand=_eq(status, status_value)),
        ]
        chosen = rng.choice(filters)
        if rng.choice([False, True]):
            chosen = _combine(
                rng.choice([QueryConjunctionFilter, QueryDisjunctionFilter]),
                [chosen, _eq(identifier, "wi-43")],
            )
        query = base.model_copy(
            update={
                "where": chosen,
                "orderings": (
                    QueryOrdering(
                        key=status,
                        direction=rng.choice(["ascending", "descending"]),
                        value_type="string",
                    ),
                ),
            }
        )
        _compare(
            world,
            query,
            budgets=QueryBudgets(max_results=rng.choice([1, 2, 5]), max_traversal_depth=0),
        )
    # Caller-bound entry and comparison parameters, including missing, invalid,
    # undeclared, defaulted and unresolved identities, keep the evaluator's refusals.
    parameterized = base.model_copy(
        update={
            "entry": QueryEntry(
                binding="item",
                subject_kinds=(SUBJECT_KIND,),
                subject_id=QueryParameterRef(parameter="id"),
            ),
            "parameters": (
                QueryParameterDeclaration(
                    name="id", value_type="string", required=False, default="wi-42"
                ),
            ),
        }
    )
    for parameters in (None, {}, {"id": "wi-43"}, {"id": "absent"}, {"id": 7}, {"extra": "x"}):
        _compare(world, parameterized, parameters=parameters)
    parameterized = base.model_copy(
        update={
            "where": QueryComparisonFilter(
                left=status,
                operator="eq",
                right=QueryParameterRef(parameter="status"),
                value_type="string",
            ),
            "parameters": (QueryParameterDeclaration(name="status", value_type="string"),),
        }
    )
    for value in ("ready", "blocked", "done", "unknown", 7):
        _compare(world, parameterized, parameters={"status": value})


def test_plain_named_budget_keeps_total_and_loads_only_retained_facts(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.indexes.typed_state import TypedStateReader

    versions = []
    attestations = TypedStateReader.claim_attestations

    def read_attestations(self: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("claim_versions") is not None:
            versions.append(kwargs["claim_versions"])
            assert kwargs.get("claim_identities") is None
        return attestations(self, *args, **kwargs)

    monkeypatch.setattr(TypedStateReader, "claim_attestations", read_attestations)
    query = work_item_query()
    observed = []
    build = served.build_accepted_query_facts

    def counted(*args: Any, **kwargs: Any) -> Any:
        result = build(*args, **kwargs)
        observed.append((kwargs.get("entry_subject_paths"), result))
        return result

    monkeypatch.setattr(served, "build_accepted_query_facts", counted)
    result = _compare(world, query, budgets=QueryBudgets(max_results=1, max_traversal_depth=0))
    assert result.truncation.candidate_result_count == 2
    assert result.truncation.returned_result_count == 1
    assert result.truncation.clipped_budgets == ("max_results",)
    scoped = [facts for paths, facts in observed if paths is not None]
    assert scoped and all(len(facts.claims) == 1 and len(facts.subjects) == 1 for facts in scoped)
    assert versions and all(len(items) == 1 for items in versions)
    assert versions[0] == tuple(
        (row.accepted.claim.identity.qualified, row.accepted.artifact_digest)
        for row in scoped[0].claims
    )


def test_selection_retains_all_competitors_and_falls_back_for_whole_set_filters(world: Any) -> None:
    coordinate = world.accepted_coordinate()
    base = work_item_query()
    with world.bind_accepted_projection(coordinate) as projection:

        def selection(query: Any) -> Any:
            return select_query_facts(
                projection.typed.connection,
                query,
                parameters=None,
                budgets=None,
                source=world.immutable_tree_at(coordinate.git_oid),
            )

        status = QueryClaimValueRef(binding="item", predicate=PREDICATE)
        # The default named policy hides verdicts: its advisory reads cannot be pruned.
        assert selection(base.model_copy(update={"where": _eq(status, "ready")})) is None
        for where in (
            QueryClaimPresenceFilter(binding="item", predicate=PREDICATE, negated=True),
            QueryNegationFilter(operand=_eq(status, "ready")),
        ):
            assert selection(base.model_copy(update={"where": where})) is None
        selected = selection(
            base.model_copy(
                update={
                    "where": _eq(QuerySubjectFieldRef(binding="item", field="subject_id"), "wi-42")
                }
            )
        )
        assert selected is not None and len(selected.paths) == 1
    full = served.build_accepted_query_facts(world, coordinate=coordinate)
    scoped = served.build_accepted_query_facts(
        world,
        coordinate=coordinate,
        predicates=(PREDICATE,),
        subject_kinds=(SUBJECT_KIND,),
        entry_subject_paths=selected.paths,
    )
    assert scoped.claims == tuple(
        row
        for row in full.claims
        if row.subject_path in selected.paths
        and row.accepted.claim.statement.predicate == PREDICATE
    )


def test_kind_counts_follow_insert_update_delete_and_transaction_rollback() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())

    def counts() -> list[Any]:
        return connection.execute(
            "SELECT * FROM query_subject_counts ORDER BY subject_kind,lifecycle"
        ).fetchall()

    connection.execute("INSERT INTO members VALUES ('subjects/a/one.json','oid','digest',1)")
    connection.execute(
        "INSERT INTO subjects VALUES "
        "('Subject:a/one','subjects/a/one.json','playbill-subject-v1','digest',NULL,1,'live','a','one')"
    )
    connection.commit()
    assert counts() == [("a", "live", 1)]
    connection.execute("UPDATE subjects SET lifecycle='retired'")
    assert counts() == [("a", "retired", 1)]
    connection.execute("UPDATE subjects SET subject_kind='b'")
    assert counts() == [("b", "retired", 1)]
    connection.execute("DELETE FROM subjects")
    assert counts() == []
    connection.rollback()
    assert counts() == [("a", "live", 1)]
    connection.close()


def test_budget_uses_path_order_and_counts_unloaded_subjects() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(schema_sql())
    for identifier in ("a", "a-", "a0"):
        path = f"subjects/{SUBJECT_KIND}/{identifier}.json"
        connection.execute("INSERT INTO members VALUES (?,?,?,1)", (path, "oid", "digest"))
        connection.execute(
            "INSERT INTO subjects VALUES (?,?,?,'digest',NULL,1,'live',?,?)",
            (
                f"Subject:{SUBJECT_KIND}/{identifier}",
                path,
                "playbill-subject-v1",
                SUBJECT_KIND,
                identifier,
            ),
        )
    selected = select_query_facts(
        connection,
        work_item_query(),
        parameters=None,
        budgets=QueryBudgets(max_results=1, max_traversal_depth=0),
        source={},
    )
    assert selected is not None
    assert selected.paths == (f"subjects/{SUBJECT_KIND}/a-.json",)
    assert selected.candidate_count == 3
    connection.close()


def test_value_selection_keeps_conflicting_claims_and_refusal_policy(tmp_path: Any) -> None:
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_service.test_compact_query import _contend

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "pushdown-competitor")
    base = work_item_query()
    query = base.model_copy(
        update={
            "where": _eq(QueryClaimValueRef(binding="item", predicate=PREDICATE), "ready"),
            "evaluation_policy": base.evaluation_policy.model_copy(
                update={
                    "visible_verdicts": (
                        "contradicted",
                        "stale",
                        "supported",
                        "uncovered",
                        "unresolved",
                    ),
                }
            ),
        }
    )
    assert _compare(instance, query).rows == ()
    refused = query.model_copy(
        update={
            "result_cardinality": "one",
            "default_budgets": QueryBudgets(max_results=1, max_traversal_depth=0),
            "maximum_budgets": QueryBudgets(max_results=1, max_traversal_depth=0),
            "evaluation_policy": query.evaluation_policy.model_copy(
                update={"conflict_behavior": "refuse_on_conflict"}
            ),
        }
    )
    assert _compare(instance, refused).verdict == "refused"


def test_entry_parameter_preserves_canonical_form_when_its_claim_is_absent(tmp_path: Any) -> None:
    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.claim_types import claim_type_digest
    from tests.core_support._identity_support import (
        IDENTITY_CLAIM_TYPES,
        KIND,
        seed_identity_surface,
    )
    from tests.test_query.test_query_subject_reference_aliases import _write

    instance = seed_identity_surface(tmp_path)
    _write(instance, {"op": "set", "subject": f"{KIND}/alpha", "field": "state", "value": "open"})
    claim_type = next(item for item in IDENTITY_CLAIM_TYPES if item.predicate == f"{KIND}.state")
    query = work_item_query().model_copy(
        update={
            "entry": QueryEntry(
                binding="item", subject_kinds=(KIND,), subject_id=QueryParameterRef(parameter="id")
            ),
            "parameters": (QueryParameterDeclaration(name="id", value_type="string"),),
            "projection": None,
            "where": _eq(
                QueryClaimValueRef(binding="item", predicate=claim_type.predicate),
                " 2026-10-09t12:00:00+00:00 ",
                "timestamp",
            ),
            "pins": (
                ArtifactPin(
                    role="claim-type",
                    target=claim_type.identity,
                    artifact_digest=claim_type_digest(claim_type).tagged,
                ),
            ),
        }
    )
    result = _compare(instance, query, parameters={"id": "beta"})
    assert result.verdict == "completed" and result.rows == ()
