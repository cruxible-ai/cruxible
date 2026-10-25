"""Closed-language parser table and printer equivalence."""

import pytest

from cruxible_client.contracts.procedures.expressions import (
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
