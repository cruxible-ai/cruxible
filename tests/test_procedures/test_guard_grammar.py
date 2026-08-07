"""G10 -- every GuardSpec refusal branch fires, and the operand grammar closes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_core.procedure.guards import (
    DERIVED_ACCESSORS,
    PREDICATE_OPERAND_FORMS,
    GuardSpec,
    PredicateGrammarError,
    parse_predicate_operand,
)

_COMPARISON = {"left": "$steps.rows.count", "op": "gt", "right": 0}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "exactly one production"),
        ({"left": "$steps.rows.count"}, "left, op and right together"),
        ({"left": "$steps.rows.count", "op": "gt"}, "left, op and right together"),
        ({"op": "gt", "right": 0}, "left, op and right together"),
        ({**_COMPARISON, "all_of": [_COMPARISON]}, "exactly one production"),
        ({"all_of": [_COMPARISON], "not_of": _COMPARISON}, "exactly one production"),
        ({"any_of": [_COMPARISON], "all_of": [_COMPARISON]}, "exactly one production"),
        ({"all_of": []}, "at least 1 item"),
        ({"any_of": []}, "at least 1 item"),
        ({"all_of": [_COMPARISON], "value_type": "int"}, "value_type belongs to a comparison"),
        ({"not_of": _COMPARISON, "value_type": "int"}, "value_type belongs to a comparison"),
    ],
    ids=[
        "empty",
        "left-only",
        "left-op",
        "op-right",
        "comparison-and-all_of",
        "all_of-and-not_of",
        "any_of-and-all_of",
        "empty-all_of",
        "empty-any_of",
        "value_type-on-all_of",
        "value_type-on-not_of",
    ],
)
def test_g10_every_guard_refusal_branch_fires(payload: dict[str, Any], expected: str) -> None:
    with pytest.raises(ValidationError, match=expected):
        GuardSpec.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _COMPARISON,
        {**_COMPARISON, "value_type": "int"},
        {"all_of": [_COMPARISON]},
        {"any_of": [_COMPARISON, {"left": "truncated(rows)", "op": "eq", "right": False}]},
        {"not_of": _COMPARISON},
        {"left": "count(rows, total_results)", "op": "gte", "right": "@param:kev_threshold"},
        {"left": "exists($steps.rows)", "op": "eq", "right": True},
        {"left": "$input.severity", "op": "eq", "right": "high"},
    ],
    ids=[
        "comparison",
        "typed-comparison",
        "all_of",
        "any_of",
        "not_of",
        "count-vs-param",
        "exists",
        "input-path",
    ],
)
def test_each_bnf_alternative_parses(payload: dict[str, Any]) -> None:
    assert GuardSpec.model_validate(payload) is not None


@pytest.mark.parametrize(
    ("raw", "form"),
    [
        (3, "literal"),
        ("open", "literal"),
        (True, "literal"),
        ("$input", "input_path"),
        ("$input.severity", "input_path"),
        ("$steps.rows", "steps_path"),
        ("$steps.rows.count", "steps_path"),
        ("count(rows, returned_results)", "count"),
        ("exists($steps.rows)", "exists"),
        ("truncated(rows)", "truncated"),
        ("@param:kev_threshold", "param"),
    ],
)
def test_the_seven_operand_forms_parse(raw: Any, form: str) -> None:
    operand = parse_predicate_operand(raw)
    assert operand.form == form
    assert operand.form in PREDICATE_OPERAND_FORMS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$item.severity", "per-item payloads do not exist at branch time"),
        ("$unknown.thing", "only \\$input and \\$steps references exist"),
        ("length(rows)", "is not a derived accessor"),
        ("upper(rows)", "is not a derived accessor"),
        ("count(rows)", "count\\(<alias>, <selector>\\)"),
        ("count(rows, nonsense)", "is not a count selector"),
        ("truncated(a, b)", "truncated\\(<alias>\\)"),
        ("@param:Not_Snake", "does not name a governed parameter"),
    ],
)
def test_operands_outside_the_grammar_are_refused(raw: str, expected: str) -> None:
    with pytest.raises(PredicateGrammarError, match=expected):
        parse_predicate_operand(raw)


def test_a_guard_operand_outside_the_grammar_is_refused_at_parse() -> None:
    with pytest.raises(ValidationError):
        GuardSpec.model_validate({"left": "$item.severity", "op": "eq", "right": 1})


def test_operands_walks_connectives() -> None:
    guard = GuardSpec.model_validate(
        {
            "all_of": [
                _COMPARISON,
                {"not_of": {"left": "truncated(rows)", "op": "eq", "right": True}},
            ]
        }
    )
    forms = sorted({operand.form for operand in guard.operands()})
    assert forms == ["literal", "steps_path", "truncated"]


def test_the_accessor_set_is_the_pinned_three() -> None:
    assert DERIVED_ACCESSORS == frozenset({"count", "exists", "truncated"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("count(rows+1, items)", "not a bare name"),
        ("count(rows.total, items)", "not a bare name"),
        ("count($steps.rows, items)", "not a bare name"),
        ("truncated(rows-1)", "not a bare name"),
        ("truncated(rows.deep)", "not a bare name"),
    ],
)
def test_accessor_arguments_are_aliases_not_expressions(raw: str, expected: str) -> None:
    """The accessor regexes match a permissive class so the refusal can NAME the
    bad argument; the argument itself is held to the alias grammar."""
    with pytest.raises(PredicateGrammarError, match=expected):
        parse_predicate_operand(raw)


@pytest.mark.parametrize(
    "raw",
    ["exists(always_true)", "exists(1)", "exists(true)"],
)
def test_exists_must_test_a_reference_not_a_constant(raw: str) -> None:
    """`exists(always_true)` was a constant-true branch wearing the costume of a
    test: the bare word resolved to itself, and a literal is always present."""
    with pytest.raises(PredicateGrammarError, match="must test a \\$input or \\$steps"):
        parse_predicate_operand(raw)


def test_exists_carries_the_alias_it_tests_so_it_can_be_bound() -> None:
    operand = parse_predicate_operand("exists($steps.rows.items)")
    assert operand.form == "exists"
    assert operand.alias == "rows"


def test_a_nested_accessor_inside_exists_does_not_parse() -> None:
    """Composition beyond the three connectives stays out."""
    with pytest.raises(PredicateGrammarError, match="is not 'exists"):
        parse_predicate_operand("exists(count(rows, items))")
