"""A Subject-reference parameter binds whichever form the caller wrote (case-law G4).

A caller may write a Subject as its bare id, ``kind/id`` or ``Subject:kind/id``.
Parameter binding spells it the way its position compares: the entry's
Subject-id parameter selects by the bare id; a ``subject_reference`` parameter
compares as ``Subject:kind/id``, the spelling of every stored Subject reference.
A form a position cannot match refuses with its repair, never an empty answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.claims import SubjectClaimObject
from cruxible_client.contracts.query.definitions import QueryDefinition
from cruxible_client.contracts.query.grammar import (
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryEntry,
    QueryMembershipFilter,
    QueryParameterDeclaration,
    QueryParameterRef,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.query.engine import PARAMETER_TYPE_MISMATCH
from tests.test_claims.test_claim_query_engine import (
    claim_fact,
    facts,
    reviewer_claim,
    run,
    subject,
    typed_item_query,
)
from tests.test_query.test_query_definitions import REVIEWER_PREDICATE

PERSON = "project.person"
ITEM = "project.work_item"
ROWS = facts((reviewer_claim(1, "wi-1", "ada"), reviewer_claim(2, "wi-2", "grace")))


def _query(*, where: object = None, entry_parameter: str | None = None, **types: str) -> Any:
    base = typed_item_query(
        REVIEWER_PREDICATE, name="project.reference_forms", object_kind="subject"
    )
    payload = base.model_dump(mode="json")
    if where is not None:
        payload["where"] = where.model_dump(mode="json")  # type: ignore[attr-defined]
    payload["parameters"] = [
        QueryParameterDeclaration(name=name, value_type=value_type).model_dump(mode="json")  # type: ignore[arg-type]
        for name, value_type in sorted(types.items())
    ]
    if entry_parameter is not None:
        payload["entry"] = QueryEntry(
            binding="item",
            subject_kinds=(ITEM,),
            subject_id=QueryParameterRef(parameter=entry_parameter),
        ).model_dump(mode="json")
    return QueryDefinition.model_validate(payload)


def _reviewer_is(parameter: str) -> QueryComparisonFilter:
    return QueryComparisonFilter(
        left=QueryClaimValueRef(binding="item", predicate=REVIEWER_PREDICATE),
        operator="eq",
        right=QueryParameterRef(parameter=parameter),
        value_type="subject_reference",
    )


def _items(query: QueryDefinition, **parameters: object) -> list[str]:
    result = run(query, ROWS, parameters=dict(parameters))
    assert result.verdict == "completed", result.refusal
    return [str(row.result_subject_identity) for row in result.rows]


@pytest.mark.parametrize("form", [f"Subject:{PERSON}/ada", f"{PERSON}/ada"])
def test_a_compared_reference_parameter_matches_in_either_qualified_form(form: str) -> None:
    query = _query(where=_reviewer_is("reviewer"), reviewer="subject_reference")
    assert _items(query, reviewer=form) == [f"Subject:{ITEM}/wi-1"]


def test_both_qualified_forms_bind_one_parameter_digest() -> None:
    query = _query(where=_reviewer_is("reviewer"), reviewer="subject_reference")
    canonical = run(query, ROWS, parameters={"reviewer": f"Subject:{PERSON}/ada"})
    short = run(query, ROWS, parameters={"reviewer": f"{PERSON}/ada"})
    assert short.parameter_digest == canonical.parameter_digest


def test_a_membership_parameter_matches_in_either_qualified_form() -> None:
    where = QueryMembershipFilter(
        left=QueryClaimValueRef(binding="item", predicate=REVIEWER_PREDICATE),
        values=(QueryParameterRef(parameter="reviewer"),),
        value_type="subject_reference",
    )
    query = _query(where=where, reviewer="subject_reference")
    for form in (f"Subject:{PERSON}/grace", f"{PERSON}/grace"):
        assert _items(query, reviewer=form) == [f"Subject:{ITEM}/wi-2"]


def test_a_bare_id_cannot_name_a_compared_subject_and_refuses_with_its_repair() -> None:
    query = _query(where=_reviewer_is("reviewer"), reviewer="subject_reference")
    result = run(query, ROWS, parameters={"reviewer": "ada"})
    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == PARAMETER_TYPE_MISMATCH
    assert "kind/id or Subject:kind/id" in result.refusal.message


@pytest.mark.parametrize("value_type", ["string", "subject_reference"])
@pytest.mark.parametrize("form", ["wi-2", f"{ITEM}/wi-2", f"Subject:{ITEM}/wi-2"])
def test_the_entry_parameter_selects_in_every_form(value_type: str, form: str) -> None:
    query = _query(entry_parameter="item_id", item_id=value_type)
    assert _items(query, item_id=form) == [f"Subject:{ITEM}/wi-2"]


def test_the_bare_entry_id_binds_unchanged() -> None:
    query = _query(entry_parameter="item_id", item_id="string")
    bare = run(query, ROWS, parameters={"item_id": "wi-2"})
    qualified = run(query, ROWS, parameters={"item_id": f"Subject:{ITEM}/wi-2"})
    assert qualified.parameter_digest == bare.parameter_digest


def test_an_entry_parameter_naming_another_kind_refuses_with_its_repair() -> None:
    query = _query(entry_parameter="item_id", item_id="string")
    result = run(query, ROWS, parameters={"item_id": f"Subject:{PERSON}/ada"})
    assert result.verdict == "refused"
    assert result.refusal is not None
    assert result.refusal.code == PARAMETER_TYPE_MISMATCH
    assert ITEM in result.refusal.message and "Repair:" in result.refusal.message


@pytest.mark.parametrize("form", ["wi-3", f"{ITEM}/wi-3", f"Subject:{ITEM}/wi-3"])
def test_one_parameter_at_the_entry_and_in_a_comparison_matches_in_every_form(form: str) -> None:
    """Selected by its bare id, compared as Subject:kind/id, from one binding."""

    own = claim_fact(
        3,
        subject_row=subject(ITEM, "wi-3"),
        predicate=REVIEWER_PREDICATE,
        obj=SubjectClaimObject(address=SemanticAddress.whole_artifact(subject_path(ITEM, "wi-3"))),
    )
    rows = facts((reviewer_claim(1, "wi-1", "ada"), own))
    query = _query(
        where=_reviewer_is("item_id"), entry_parameter="item_id", item_id="subject_reference"
    )
    result = run(query, rows, parameters={"item_id": form})
    assert result.verdict == "completed", result.refusal
    assert [str(row.result_subject_identity) for row in result.rows] == [f"Subject:{ITEM}/wi-3"]
