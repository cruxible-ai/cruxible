"""Composed filters pay for admitted entries, with the full facts as oracle."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimPresenceFilter,
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryConjunctionFilter,
    QueryDisjunctionFilter,
    QueryEntry,
    QueryLiteralRef,
    QueryParameterDeclaration,
    QueryParameterRef,
    QuerySubjectFieldRef,
)
from cruxible_core.indexes.typed_state import owner_for_identity, owner_values, schema_sql
from cruxible_core.query.backends import ClaimQueryFactsV1
from cruxible_core.query.engine import evaluate_claim_query
from cruxible_core.service.discovery.query_pushdown import select_query_facts
from tests.core_support._knowledge_loop_support import SUBJECT_KIND
from tests.test_claims.test_claim_query_engine import (
    NOW,
    coordinate,
    sorted_operands,
    status_claim,
    subject,
    typed_item_query,
)
from tests.test_query.test_query_definitions import STATUS_PREDICATE, accepted_query, claim_type

TARGET = "item-0000"
TARGET_PATH = f"subjects/{SUBJECT_KIND}/{TARGET}.json"
BUDGET = QueryBudgets(max_results=1, max_traversal_depth=0)


def _insert(connection: sqlite3.Connection, accepted: Any, source: Any) -> None:
    """Use the production scalar projection for exact accepted fixture artifacts."""
    owner = owner_for_identity(source.identity.qualified)
    assert owner is not None
    values = {
        "identity": source.identity.qualified,
        "path": accepted.path,
        "format_tag": source.artifact_format,
        "artifact_digest": accepted.artifact_digest,
        "predecessor_digest": None,
        "revision": 1,
        "lifecycle": "live",
        **owner_values(owner, source),
    }
    connection.execute("INSERT INTO members VALUES (?,?,?,1)", (accepted.path, "oid", "digest"))
    connection.execute(
        f"INSERT INTO {owner.table} ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
        tuple(values.values()),
    )


@pytest.fixture(scope="module")
def populations() -> Iterator[list[tuple[sqlite3.Connection, ClaimQueryFactsV1]]]:
    worlds: list[tuple[sqlite3.Connection, ClaimQueryFactsV1]] = []
    connections = []
    try:
        for size in (200, 2000):
            connection = sqlite3.connect(":memory:")
            connections.append(connection)
            connection.executescript(schema_sql())
            subjects = tuple(subject(SUBJECT_KIND, f"item-{i:04}") for i in range(size))
            # Every Subject matches the same value; a global value seek must fail
            # the cost guard even though the requested entry is identical.
            claims = tuple(status_claim(i, f"item-{i:04}", "ready") for i in range(size))
            for row in subjects:
                _insert(connection, row, row.shell)
            for row in claims:
                _insert(connection, row.accepted, row.accepted.claim)
            connection.commit()
            worlds.append(
                (
                    connection,
                    ClaimQueryFactsV1(coordinate=coordinate(), subjects=subjects, claims=claims),
                )
            )
        yield worlds
    finally:
        for connection in connections:
            connection.close()


def _eq(ref: Any, value: str) -> QueryComparisonFilter:
    return QueryComparisonFilter(
        left=ref, operator="eq", right=QueryLiteralRef(value=value), value_type="string"
    )


def _query(mode: str) -> Any:
    kind = _eq(QuerySubjectFieldRef(binding="item", field="subject_kind"), SUBJECT_KIND)
    identity = _eq(QuerySubjectFieldRef(binding="item", field="subject_id"), TARGET)
    value = _eq(QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE), "ready")
    if mode == "bound_kind":
        where = kind
    elif mode == "bound_value_conjunction":
        where = QueryConjunctionFilter(filters=sorted_operands(kind, value, identity))
    elif mode == "identity_kind_conjunction":
        where = QueryConjunctionFilter(filters=sorted_operands(kind, identity))
    elif mode == "identity_value_conjunction":
        where = QueryConjunctionFilter(filters=sorted_operands(value, identity))
    else:
        branches = QueryDisjunctionFilter(
            filters=sorted_operands(
                kind, _eq(QuerySubjectFieldRef(binding="item", field="subject_id"), "absent")
            )
        )
        where = QueryConjunctionFilter(filters=sorted_operands(branches, identity))
    query = typed_item_query(STATUS_PREDICATE, name="project.restricted", where=where)
    query = query.model_copy(
        update={
            "evaluation_policy": query.evaluation_policy.model_copy(
                update={
                    "visible_verdicts": (
                        "contradicted",
                        "stale",
                        "supported",
                        "uncovered",
                        "unresolved",
                    )
                }
            )
        }
    )
    if mode.startswith("bound_"):
        query = query.model_copy(
            update={
                "entry": QueryEntry(
                    binding="item",
                    subject_kinds=(SUBJECT_KIND,),
                    subject_id=QueryParameterRef(parameter="id"),
                ),
                "parameters": (QueryParameterDeclaration(name="id", value_type="string"),),
            }
        )
    return query


@pytest.mark.parametrize(
    "mode",
    [
        "bound_kind",
        "bound_value_conjunction",
        "identity_kind_conjunction",
        "identity_value_conjunction",
        "identity_disjunction_conjunction",
    ],
)
def test_restricted_filters_have_flat_sql_work_and_match_full_load(
    populations: Any, mode: str, record_property: Any
) -> None:
    query = _query(mode)
    parameters = {"id": TARGET} if mode.startswith("bound_") else None
    source = {claim_type_path(STATUS_PREDICATE): render_claim_type(claim_type(STATUS_PREDICATE))}
    costs = []
    for connection, whole in populations:
        steps = 0

        def tick() -> int:
            nonlocal steps
            steps += 1
            return 0

        connection.set_progress_handler(tick, 1)
        try:
            selected = select_query_facts(
                connection, query, parameters=parameters, budgets=BUDGET, source=source
            )
        finally:
            connection.set_progress_handler(None, 0)
        costs.append(steps)
        assert selected is not None and selected.paths == (TARGET_PATH,)
        scoped = whole.model_copy(
            update={
                "subjects": tuple(row for row in whole.subjects if row.path in selected.paths),
                "claims": tuple(row for row in whole.claims if row.subject_path in selected.paths),
            }
        )
        kwargs = {
            "coordinate": whole.coordinate,
            "evaluation_time": NOW,
            "parameters": parameters,
            "budgets": BUDGET,
        }
        actual = evaluate_claim_query(
            accepted_query(query), facts=scoped, entry_paths=selected.paths, **kwargs
        )
        expected = evaluate_claim_query(accepted_query(query), facts=whole, **kwargs)
        assert len(actual.rows) == 1
        assert canonical_bytes(actual.model_dump(mode="json")) == canonical_bytes(
            expected.model_dump(mode="json")
        )
    record_property("sql_vm_steps_small_large", costs)
    assert costs[1] <= costs[0] * 1.25 + 1024, costs


@pytest.mark.parametrize("compound", [QueryConjunctionFilter, QueryDisjunctionFilter])
def test_unsupported_compounds_fall_back_without_enumerating_supported_children(
    populations: Any, compound: Any
) -> None:
    query = _query("identity_kind_conjunction")
    where = compound(
        filters=sorted_operands(
            _eq(QuerySubjectFieldRef(binding="item", field="subject_kind"), SUBJECT_KIND),
            QueryClaimPresenceFilter(binding="item", predicate=STATUS_PREDICATE, negated=True),
        )
    )
    connection, _ = populations[0]
    statements = []
    connection.set_trace_callback(statements.append)
    try:
        assert (
            select_query_facts(
                connection,
                query.model_copy(update={"where": where}),
                parameters=None,
                budgets=BUDGET,
                source={},
            )
            is None
        )
    finally:
        connection.set_trace_callback(None)
    assert statements == []


@pytest.mark.parametrize("bound", [False, True])
def test_disjunction_branches_keep_the_same_incoming_candidates(
    populations: Any, bound: bool
) -> None:
    query = _query("bound_kind" if bound else "identity_kind_conjunction")
    where = QueryDisjunctionFilter(
        filters=sorted_operands(
            _eq(QuerySubjectFieldRef(binding="item", field="subject_id"), TARGET),
            _eq(QuerySubjectFieldRef(binding="item", field="subject_id"), "item-0001"),
        )
    )
    query = query.model_copy(update={"where": where})
    parameters = {"id": TARGET} if bound else None
    connection, whole = populations[0]
    selected = select_query_facts(
        connection, query, parameters=parameters, budgets=BUDGET, source={}
    )
    expected_paths = (
        (TARGET_PATH,) if bound else (TARGET_PATH, f"subjects/{SUBJECT_KIND}/item-0001.json")
    )
    assert selected is not None and selected.paths == expected_paths
    scoped = whole.model_copy(
        update={
            "subjects": tuple(row for row in whole.subjects if row.path in selected.paths),
            "claims": tuple(row for row in whole.claims if row.subject_path in selected.paths),
        }
    )
    kwargs = {
        "coordinate": whole.coordinate,
        "evaluation_time": NOW,
        "parameters": parameters,
        "budgets": BUDGET,
    }
    actual = evaluate_claim_query(
        accepted_query(query), facts=scoped, entry_paths=selected.paths, **kwargs
    )
    expected = evaluate_claim_query(accepted_query(query), facts=whole, **kwargs)
    assert canonical_bytes(actual.model_dump(mode="json")) == canonical_bytes(
        expected.model_dump(mode="json")
    )
