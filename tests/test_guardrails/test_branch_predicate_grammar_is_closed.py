"""The predicate grammar is CLOSED, and this is what keeps it closed.

Total static analyzability is the differentiator. A predicate language that
grows an operator here and an accessor there stops being analysable long before
anyone notices, because each addition looks locally reasonable and none of them
fails a test. This file makes every widening fail one.

Every pinned set carries the decision that authorised its members. Changing a
set is therefore a governed act, not a diff.
"""

from __future__ import annotations

import typing

import pytest
from pydantic import BaseModel

from cruxible_core.config.schema import CountSelector, FilterComparisonOp
from cruxible_core.predicate import PredicateValueType
from cruxible_core.procedure.guards import (
    DERIVED_ACCESSORS,
    PREDICATE_OPERAND_FORMS,
    GuardSpec,
    PredicateGrammarError,
    PredicateOperand,
    parse_predicate_operand,
)

# Authorised by dd-procedures-as-graphs-first: typed predicates only, never
# embedded code.
PINNED_COMPARISON_OPS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "gte",
        "lt",
        "lte",
        "==",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "before",
        "on_or_before",
        "after",
        "on_or_after",
    }
)
PINNED_VALUE_TYPES = frozenset(
    {"string", "int", "integer", "float", "number", "bool", "date", "datetime"}
)
PINNED_COUNT_SELECTORS = frozenset({"returned_results", "total_results", "items", "results"})
PINNED_DERIVED_ACCESSORS = frozenset({"count", "exists", "truncated"})
PINNED_OPERAND_FORMS = frozenset(
    {"literal", "input_path", "steps_path", "count", "exists", "truncated", "param"}
)
PINNED_GUARD_FIELDS = frozenset({"left", "op", "right", "value_type", "all_of", "any_of", "not_of"})
PINNED_OPERAND_FIELDS = frozenset(
    {"form", "literal", "path", "alias", "selector", "ref", "parameter_name"}
)


def _members(annotation: object) -> frozenset[str]:
    return frozenset(str(member) for member in typing.get_args(annotation))


def test_the_comparison_operator_set_is_pinned() -> None:
    assert _members(FilterComparisonOp) == PINNED_COMPARISON_OPS


def test_the_value_type_set_is_pinned() -> None:
    assert _members(PredicateValueType) == PINNED_VALUE_TYPES


def test_the_count_selector_set_is_pinned() -> None:
    assert _members(CountSelector) == PINNED_COUNT_SELECTORS


def test_the_derived_accessor_set_is_pinned() -> None:
    """Adding an accessor requires a decision record.

    The three exist because they read metadata the engine ALREADY builds. A
    fourth that computed something new would be the first step back toward
    embedded code.
    """
    assert DERIVED_ACCESSORS == PINNED_DERIVED_ACCESSORS


def test_the_operand_form_set_is_pinned_and_no_eighth_is_reachable() -> None:
    assert PREDICATE_OPERAND_FORMS == PINNED_OPERAND_FORMS
    assert _members(PredicateOperand.model_fields["form"].annotation) == PINNED_OPERAND_FORMS


def test_the_guard_model_gains_no_field_outside_the_pinned_list() -> None:
    assert frozenset(GuardSpec.model_fields) == PINNED_GUARD_FIELDS


def test_the_operand_model_gains_no_field_outside_the_pinned_list() -> None:
    assert frozenset(PredicateOperand.model_fields) == PINNED_OPERAND_FIELDS


def test_both_grammar_models_forbid_extra_keys() -> None:
    for model in (GuardSpec, PredicateOperand):
        assert issubclass(model, BaseModel)
        assert model.model_config["extra"] == "forbid"


@pytest.mark.parametrize(
    "candidate",
    [
        "len(rows)",
        "sum(rows, items)",
        "lower($input.severity)",
        "now()",
        "$steps.rows.count + 1",
        "regex($input.name, '.*')",
    ],
)
def test_an_operand_form_outside_the_seven_is_unreachable(candidate: str) -> None:
    """Arithmetic, string functions and user-defined predicates stay out.

    Each of these is what "just this one helper" looks like on the day it is
    proposed.
    """
    if any(candidate.startswith(f"{name}(") for name in DERIVED_ACCESSORS):
        pytest.fail("the probe must not name an authorised accessor")
    with pytest.raises(PredicateGrammarError):
        parse_predicate_operand(candidate)


def test_composition_beyond_the_three_connectives_does_not_parse() -> None:
    with pytest.raises(Exception):
        GuardSpec.model_validate(
            {"xor_of": [{"left": 1, "op": "eq", "right": 1}]}  # a fourth connective
        )


def test_item_references_are_refused_by_name() -> None:
    """Per-item payloads do not exist at branch time.

    A predicate over one could be neither evaluated nor analysed, so this is a
    named refusal rather than an accidental parse failure.
    """
    with pytest.raises(PredicateGrammarError, match="do not exist at branch time"):
        parse_predicate_operand("$item.severity")
