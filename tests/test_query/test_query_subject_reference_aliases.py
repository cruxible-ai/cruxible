"""A Subject reference a query compares resolves through aliases (review F-006).

A typed ``subject_reference`` literal or parameter resolves at the evaluated
coordinate exactly as at every other entry point: the exact Subject, else the
one Subject of its kind an alias names; several refuse naming each. The stored
definition, its digest and the parameter digest never change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.query.grammar import (
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryLiteralRef,
    QueryMembershipFilter,
)
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_client.contracts.write import WriteRequest
from cruxible_core.indexes.subject_aliases import (
    SubjectReferenceAmbiguous,
    tree_reference_resolver,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.query import evaluate_accepted_query
from tests.core_support._identity_support import OWNER, accept_tree, seed_identity_surface, shell
from tests.test_claims.test_claim_query_engine import facts, reviewer_claim, typed_item_query
from tests.test_query.test_query_definitions import REVIEWER_PREDICATE, accepted_query

PERSON = "project.person"
AT = datetime(2026, 10, 9, 12, tzinfo=UTC)


def _write(instance: PlaybillInstance, *changes: dict[str, Any]) -> None:
    outcome = service_playbill_write(
        instance,
        request=WriteRequest.model_validate({"because": "A", "changes": list(changes)}),
        caller=WriteCaller(actor=OWNER, may_activate=True),
    )
    assert outcome.status == "accepted", outcome


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    world = seed_identity_surface(tmp_path)
    accept_tree(
        world,
        {
            subject_path(PERSON, "ada"): render_subject(shell("ada", kind=PERSON)),
            subject_path(PERSON, "bob"): render_subject(shell("bob", kind=PERSON)),
        },
        name="people",
    )
    _write(
        world,
        {"op": "add", "subject": f"{PERSON}/ada", "field": "semantic.alias", "value": "ada-alias"},
    )
    return world


def _run(instance: PlaybillInstance, where: object) -> Any:
    head = instance.accepted_coordinate()
    rows = facts((reviewer_claim(1, "wi-1", "ada"),)).model_copy(update={"coordinate": head})
    query = typed_item_query(
        REVIEWER_PREDICATE, name="project.alias_probe", object_kind="subject", where=where
    )
    definition = accepted_query(query)
    result = evaluate_accepted_query(
        instance, definition, coordinate=head, evaluation_time=AT, facts=lambda: rows
    )
    return definition, result


def _compared(value: str) -> QueryComparisonFilter:
    return QueryComparisonFilter(
        left=QueryClaimValueRef(binding="item", predicate=REVIEWER_PREDICATE),
        operator="eq",
        right=QueryLiteralRef(value=f"Subject:{PERSON}/{value}"),
        value_type="subject_reference",
    )


def test_named_query_literal_reference_resolves_alias(instance: PlaybillInstance) -> None:
    _definition, canonical = _run(instance, _compared("ada"))
    assert canonical.verdict == "completed", canonical
    assert [row.result_subject_identity for row in canonical.rows] == [
        "Subject:project.work_item/wi-1"
    ]
    definition, aliased = _run(instance, _compared("ada-alias"))
    assert aliased.verdict == "completed", aliased
    assert aliased.rows == canonical.rows
    # The stored definition is read as written: its digest is its own.
    assert aliased.definition_digest == definition.artifact_digest


def test_membership_values_resolve_alias(instance: PlaybillInstance) -> None:
    where = QueryMembershipFilter(
        left=QueryClaimValueRef(binding="item", predicate=REVIEWER_PREDICATE),
        values=(QueryLiteralRef(value=f"Subject:{PERSON}/ada-alias"),),
        value_type="subject_reference",
    )
    _definition, result = _run(instance, where)
    assert result.verdict == "completed", result
    assert len(result.rows) == 1


def test_an_ambiguous_literal_alias_refuses_naming_each(instance: PlaybillInstance) -> None:
    _write(
        instance,
        {"op": "add", "subject": f"{PERSON}/bob", "field": "semantic.alias", "value": "ada-alias"},
    )
    _definition, result = _run(instance, _compared("ada-alias"))
    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "cruxible.query.alias_ambiguous"
    assert f"{PERSON}/ada" in result.refusal.message and f"{PERSON}/bob" in result.refusal.message


def test_condition_operands_resolve_the_same_on_live_and_cold_trees(
    instance: PlaybillInstance,
) -> None:
    """Settle conditions and corroboration resolve at the parent tree, live or replayed."""

    oid = instance.accepted_coordinate().git_oid
    live = tree_reference_resolver(instance.immutable_tree_at(oid))
    cold = tree_reference_resolver(instance.tree_at(oid))
    for resolve in (live, cold):
        assert resolve(f"Subject:{PERSON}/ada-alias") == f"Subject:{PERSON}/ada"
        assert resolve(f"Subject:{PERSON}/ada") == f"Subject:{PERSON}/ada"
        assert resolve(f"Subject:{PERSON}/nobody") == f"Subject:{PERSON}/nobody"
        assert resolve("not a reference") == "not a reference"
    _write(
        instance,
        {"op": "add", "subject": f"{PERSON}/bob", "field": "semantic.alias", "value": "ada-alias"},
    )
    oid = instance.accepted_coordinate().git_oid
    for tree in (instance.immutable_tree_at(oid), instance.tree_at(oid)):
        with pytest.raises(SubjectReferenceAmbiguous):
            tree_reference_resolver(tree)(f"Subject:{PERSON}/ada-alias")
