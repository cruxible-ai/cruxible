"""Closed-language parser table and printer equivalence."""

import pytest

from cruxible_client.contracts.procedures.expressions import (
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_LENGTH,
    ProcedureExpressionError,
    parse_procedure_expression,
    render_procedure_expression,
)


@pytest.mark.parametrize(
    "expression",
    [
        "parse.mine > 0",
        'input.name == "a\\"b"',
        "param.limit >= -3",
        "not truncated(held)",
        "exists($input.value)",
        "exists($parse.mine)",
        "count(parse) == 0",
        "true",
        "false == null",
        "not input.a and input.b or input.c",
        "(input.a or input.b) and not input.c",
        "input.a before input.b",
        "input.a on_or_after input.b",
    ],
)
def test_parse_print_roundtrip(expression):
    predicate = parse_procedure_expression(expression)
    assert parse_procedure_expression(render_procedure_expression(predicate)) == predicate


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "x >> 0",
        "parse.mine >> 0",
        "input.a ==",
        "f()",
        "input.a + 1",
        "input.a == 1.5",
        "input.a == 01",
        "input.a == True",
        'input.a == "unterminated',
        "input.a and",
        '__import__("os")',
        "count(parse.x) == 1",
        "truncated($parse)",
        "input.a == 1 == 2",
        "param.a.b == 1",
        "(input.a",
    ],
)
def test_reject_non_language(expression):
    with pytest.raises((ProcedureExpressionError, ValueError)):
        parse_procedure_expression(expression)


def test_precedence():
    predicate = parse_procedure_expression("not input.a and input.b or input.c")
    assert predicate.any_of is not None
    assert predicate.any_of[0].all_of is not None
    assert predicate.any_of[0].all_of[0].not_of is not None


@pytest.mark.parametrize("expression", ["not " * 2000 + "true", "(" * 2000 + "true" + ")" * 2000])
def test_excessive_nesting_has_a_typed_column_refusal(expression):
    with pytest.raises(ProcedureExpressionError, match="nesting depth") as caught:
        parse_procedure_expression(expression)
    assert caught.value.column > 1
    assert parse_procedure_expression("not " * MAX_EXPRESSION_DEPTH + "true")


def test_oversized_expression_refuses_before_tokenization():
    # The first character is not a token; if the tokenizer ran first the
    # refusal would name column 1, not the length bound.
    oversized = "\u00a7" + "true or " * 150_000 + "true"
    with pytest.raises(ProcedureExpressionError, match="characters") as caught:
        parse_procedure_expression(oversized)
    assert caught.value.column == MAX_EXPRESSION_LENGTH + 1
    with pytest.raises(ProcedureExpressionError) as token_refusal:
        parse_procedure_expression(oversized[:MAX_EXPRESSION_LENGTH])
    assert token_refusal.value.column == 1


def test_trailing_whitespace_and_many_flat_terms_remain_valid():
    expression = " or ".join(["true"] * 1000) + " \t\n"
    assert len(parse_procedure_expression(expression).any_of) == 1000
