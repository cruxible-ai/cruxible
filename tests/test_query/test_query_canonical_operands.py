"""A QueryDefinition compares a literal or a parameter in the predicate's canonical form.

Stored values are canonical by law; an operand compared with them is
canonicalized by the same ClaimType's form first, so two spellings equal under
it compare equal and one distinct under it stays distinct.
"""

from __future__ import annotations

import pytest

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


def _keyed(row: ClaimFactRowV1) -> ClaimFactRowV1:
    from cruxible_client.contracts.identity_normalizers import Pep503Normalizer

    return row.model_copy(
        update={"canonical_form": ("trim",), "key_normalizer": Pep503Normalizer(version=1)}
    )


def test_a_key_predicates_operand_takes_its_normalizer_after_the_form() -> None:
    """R12: a key part's stored value is form-then-normalizer; so is a compared operand."""

    stored = (_keyed(status_claim(1, "wi-1", "django-rest")), _keyed(status_claim(2, "wi-2", "x")))
    for spelled in (" Django_REST ", "django.rest", "DJANGO--rest"):
        assert _rows(_eq(spelled), *stored) == ["wi-1"]
    values = tuple(
        sorted(
            (QueryLiteralRef(value="X"), QueryLiteralRef(value="Django_Rest")),
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    membership = typed_item_query(
        STATUS_PREDICATE,
        name="project.status_in",
        where=QueryMembershipFilter(
            left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
            values=values,
            value_type="string",
        ),
    )
    assert sorted(_rows(membership, *stored)) == ["wi-1", "wi-2"]  # type: ignore[type-var]
    # An operand that is no key of the field refuses rather than silently missing.
    refused = run(_eq("_private"), facts(stored))  # type: ignore[arg-type]
    assert refused.verdict == "refused"
    # Without the row's normalizer the spelling stays distinct.
    plain = (_formed(status_claim(1, "wi-1", "django-rest"), ("trim",)),)
    assert _rows(_eq("Django_REST"), *plain) == []


def test_a_fact_row_without_a_key_normalizer_keeps_its_bytes() -> None:
    row = status_claim(1, "wi-1", "ready")
    assert "key_normalizer" not in row.model_dump(mode="json")


# -- F-001: operands take the one shared key canonicalization, both checks ------------


def _rule(spelled: dict[str, object]) -> object:
    from pydantic import TypeAdapter

    from cruxible_client.contracts.identity_normalizers import KeyNormalizer

    return TypeAdapter(KeyNormalizer).validate_python(spelled)


_LOWER_TRIM = (("trim",), {"normalizer": "lowercase", "version": 1})
_PURL_CASEFOLD = (("casefold",), {"normalizer": "purl-type", "version": 1, "identity": "package"})


def _ruled(row: ClaimFactRowV1, form: tuple[str, ...], rule: dict[str, object]) -> ClaimFactRowV1:
    return row.model_copy(update={"canonical_form": form, "key_normalizer": _rule(rule)})


def _param_query(operator: str) -> object:
    from cruxible_client.contracts.query.definitions import QueryDefinition
    from cruxible_client.contracts.query.grammar import (
        QueryParameterDeclaration,
        QueryParameterRef,
    )

    base = typed_item_query(STATUS_PREDICATE, name="project.status_param")
    return QueryDefinition.model_validate(
        {
            **dict(base),
            "where": QueryComparisonFilter(
                left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
                operator=operator,  # type: ignore[arg-type]
                right=QueryParameterRef(parameter="value"),
                value_type="string",
            ),
            "parameters": (QueryParameterDeclaration(name="value", value_type="string"),),
        }
    )


def _membership(*values: str) -> object:
    refs = tuple(
        sorted(
            (QueryLiteralRef(value=value) for value in values),
            key=lambda item: canonical_bytes(item.model_dump(mode="json")),
        )
    )
    return typed_item_query(
        STATUS_PREDICATE,
        name="project.status_in",
        where=QueryMembershipFilter(
            left=QueryClaimValueRef(binding="item", predicate=STATUS_PREDICATE),
            values=refs,
            value_type="string",
        ),
    )


@pytest.mark.parametrize(
    ("ruling", "stored", "bad"),
    [
        # trim + lowercase@1: an all-spaces operand settles to "", which is no key.
        (_LOWER_TRIM, "ready", "   "),
        # casefold + purl-type@1: the second form pass re-spells the escapes, so no
        # value is canonical under both rules.
        (_PURL_CASEFOLD, "pkg:generic/x", "pkg:generic/%C3%A9"),
    ],
    ids=["empty-after-form", "form-respells-normalized"],
)
def test_an_operand_no_key_can_equal_refuses_in_every_operator(
    ruling: tuple[tuple[str, ...], dict[str, object]], stored: str, bad: str
) -> None:
    form, rule = ruling
    rows = facts((_ruled(status_claim(1, "wi-1", stored), form, rule),))
    for operator in ("eq", "ne"):
        for query, parameters in (
            (_param_query(operator), {"value": bad}),
            (_eq(bad) if operator == "eq" else None, None),
        ):
            if query is None:
                continue
            result = run(query, rows, parameters=parameters)  # type: ignore[arg-type]
            assert result.verdict == "refused", (operator, result)
            assert result.refusal is not None
            assert result.refusal.code == "cruxible.query.value_type_mismatch"
    refused = run(_membership(stored, bad), rows)  # type: ignore[arg-type]
    assert refused.verdict == "refused"
    assert refused.refusal is not None
    assert refused.refusal.code == "cruxible.query.value_type_mismatch"
    # A canonical operand still matches through the parameter path.
    matched = run(_param_query("eq"), rows, parameters={"value": stored})  # type: ignore[arg-type]
    assert [row.fields[0].value for row in matched.rows] == ["wi-1"]


def test_key_operands_apply_the_form_only_through_the_shared_canonicalizer(monkeypatch):
    from cruxible_core.query import engine

    def redundant_form(*args):
        pytest.fail("the key canonicalizer owns both form passes")

    monkeypatch.setattr(engine, "canonicalize_literal", redundant_form)
    stored = _ruled(status_claim(1, "wi-1", "ready"), *_LOWER_TRIM)
    assert _rows(_eq(" READY "), stored) == ["wi-1"]
