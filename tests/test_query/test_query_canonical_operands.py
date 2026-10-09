"""A QueryDefinition compares a literal or a parameter in the predicate's canonical form.

Stored values are canonical by law; an operand compared with them is
canonicalized by the same ClaimType's form first, so two spellings equal under
it compare equal and one distinct under it stays distinct.
"""

from __future__ import annotations

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.query.grammar import (
    QueryClaimValueRef,
    QueryComparisonFilter,
    QueryLiteralRef,
    QueryMembershipFilter,
)
from cruxible_core.query.engine import ClaimFactRowV1
from tests.test_claims.test_claim_query_engine import (
    facts,
    run,
    status_claim,
    typed_item_query,
)
from tests.test_query.test_query_definitions import STATUS_PREDICATE

FORM = ("trim", "casefold")


def _formed(row: ClaimFactRowV1, form: tuple[str, ...] = FORM) -> ClaimFactRowV1:
    return row.model_copy(update={"canonical_form": form})


def _eq(value: str) -> object:
    return typed_item_query(
        STATUS_PREDICATE,
        name="project.status_eq",
        where=QueryComparisonFilter(
            left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
            operator="eq",
            right=QueryLiteralRef(value=value),
            value_type="string",
        ),
    )


def _rows(query: object, *rows: ClaimFactRowV1) -> list[object]:
    return [row.fields[0].value for row in run(query, facts(rows)).rows]  # type: ignore[arg-type]


def test_a_literal_compares_in_the_predicates_canonical_form() -> None:
    stored = (_formed(status_claim(1, "wi-1", "ready")), _formed(status_claim(2, "wi-2", "done")))
    assert _rows(_eq("  READY "), *stored) == ["wi-1"]
    # Distinct under the form stays distinct.
    assert _rows(_eq("re-ady"), *stored) == []


def test_without_a_form_the_literal_compares_exactly() -> None:
    stored = (status_claim(1, "wi-1", "ready"),)
    assert _rows(_eq("ready"), *stored) == ["wi-1"]
    assert _rows(_eq("  READY "), *stored) == []


def test_membership_canonicalizes_every_listed_value() -> None:
    values = tuple(
        sorted(
            (QueryLiteralRef(value=" Blocked"), QueryLiteralRef(value="READY ")),
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    query = typed_item_query(
        STATUS_PREDICATE,
        name="project.status_in",
        where=QueryMembershipFilter(
            left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
            values=values,
            value_type="string",
        ),
    )
    stored = (_formed(status_claim(1, "wi-1", "ready")), _formed(status_claim(2, "wi-2", "done")))
    assert _rows(query, *stored) == ["wi-1"]


def test_a_fact_row_without_a_form_keeps_its_bytes() -> None:
    row = status_claim(1, "wi-1", "ready")
    assert "canonical_form" not in row.model_dump(mode="json")
    assert _formed(row).model_dump(mode="json")["canonical_form"] == list(FORM)
