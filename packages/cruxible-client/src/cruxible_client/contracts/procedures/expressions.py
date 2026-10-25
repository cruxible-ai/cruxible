"""Parser/printer for the closed Procedure predicate language. No evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from cruxible_client.contracts.procedures.models import GuardPredicate, PredicateOperand

MAX_EXPRESSION_LENGTH = 16_384
MAX_EXPRESSION_DEPTH = 64


@dataclass(eq=False)
class ProcedureExpressionError(ValueError):
    column: int
    expected: str

    def __str__(self) -> str:
        return f"column {self.column}: expected {self.expected}"


_TOKEN = re.compile(
    r'\s*(?:(?P<string>"(?:[^"\\\x00-\x1f]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*")|(?P<int>-?(?:0|[1-9][0-9]*))|(?P<op>==|!=|>=|<=|>|<)|(?P<name>\$?[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)|(?P<punct>[(),]))'
)
_TRAILING_WHITESPACE = re.compile(r"\s*\Z")
_OPERATORS = {
    "==": "eq",
    "!=": "ne",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
    "before": "before",
    "after": "after",
    "on_or_before": "on_or_before",
    "on_or_after": "on_or_after",
}


class _Parser:
    def __init__(self, text: str):
        if len(text) > MAX_EXPRESSION_LENGTH:
            raise ProcedureExpressionError(
                MAX_EXPRESSION_LENGTH + 1, f"at most {MAX_EXPRESSION_LENGTH} characters"
            )
        self.tokens: list[tuple[str, str, int]] = []
        pos = 0
        while pos < len(text):
            if _TRAILING_WHITESPACE.match(text, pos):
                break
            match = _TOKEN.match(text, pos)
            if match is None:
                raise ProcedureExpressionError(pos + 1, "an operand or operator")
            kind = match.lastgroup or ""
            self.tokens.append((kind, match[kind], match.start(kind) + 1))
            pos = match.end()
        self.tokens.append(("end", "", len(text) + 1))
        self.index = 0

    def peek(self) -> str:
        return self.tokens[self.index][1]

    def take(self, value: str) -> bool:
        if self.peek() == value:
            self.index += 1
            return True
        return False

    def require(self, value: str) -> None:
        if not self.take(value):
            raise ProcedureExpressionError(self.tokens[self.index][2], repr(value))

    def expression(self, depth: int = 0) -> GuardPredicate:
        children = [self.conjunction(depth)]
        while self.take("or"):
            children.append(self.conjunction(depth))
        return children[0] if len(children) == 1 else GuardPredicate(any_of=tuple(children))

    def conjunction(self, depth: int) -> GuardPredicate:
        children = [self.unary(depth)]
        while self.take("and"):
            children.append(self.unary(depth))
        return children[0] if len(children) == 1 else GuardPredicate(all_of=tuple(children))

    def unary(self, depth: int) -> GuardPredicate:
        if depth > MAX_EXPRESSION_DEPTH:
            raise ProcedureExpressionError(
                self.tokens[self.index][2], f"nesting depth at most {MAX_EXPRESSION_DEPTH}"
            )
        if self.take("not"):
            return GuardPredicate(not_of=self.unary(depth + 1))
        if self.take("("):
            value = self.expression(depth + 1)
            self.require(")")
            return value
        left = self.operand()
        operator = _OPERATORS.get(self.peek())
        if operator is None:
            return GuardPredicate(
                left=left, operator="eq", right=PredicateOperand(kind="literal", value=True)
            )
        self.index += 1
        right = self.operand()
        return GuardPredicate(left=left, operator=operator, right=right)  # type: ignore[arg-type]

    def operand(self) -> PredicateOperand:
        kind, value, column = self.tokens[self.index]
        self.index += 1
        if kind == "string":
            return PredicateOperand(kind="literal", value=json.loads(value))
        if kind == "int":
            return PredicateOperand(kind="literal", value=int(value))
        if value in {"true", "false", "null"}:
            return PredicateOperand(
                kind="literal", value={"true": True, "false": False, "null": None}[value]
            )
        if kind != "name" or value in {"and", "or", "not"}:
            raise ProcedureExpressionError(
                column,
                'an operand (integer, "text", true, false, null, input.x, param.x, <step>.x)',
            )
        if value in {"count", "exists", "truncated"}:
            self.require("(")
            ref_kind, ref, ref_column = self.tokens[self.index]
            if ref_kind != "name":
                raise ProcedureExpressionError(ref_column, "a reference")
            self.index += 1
            self.require(")")
            parts = ref.removeprefix("$").split(".")
            if value == "exists":
                if parts[0] == "input" and len(parts) > 1:
                    return PredicateOperand(
                        kind="exists", input_name=parts[1], path=tuple(parts[2:])
                    )
                return PredicateOperand(kind="exists", alias=parts[0], path=tuple(parts[1:]))
            if len(parts) != 1 or ref.startswith("$"):
                raise ProcedureExpressionError(ref_column, "a step id")
            return PredicateOperand(kind=value, alias=ref)  # type: ignore[arg-type]
        parts = value.split(".")
        if len(parts) < 2 or value.startswith("$"):
            raise ProcedureExpressionError(column, "input.PATH, param.NAME or ID.PATH")
        if parts[0] == "input":
            return PredicateOperand(kind="input", input_name=parts[1], path=tuple(parts[2:]))
        if parts[0] == "param":
            if len(parts) != 2:
                raise ProcedureExpressionError(column, "param.NAME")
            return PredicateOperand(kind="parameter", parameter_name=parts[1])
        return PredicateOperand(kind="step", alias=parts[0], path=tuple(parts[1:]))


def parse_procedure_expression(text: str) -> GuardPredicate:
    parser = _Parser(text)
    result = parser.expression()
    if parser.tokens[parser.index][0] != "end":
        raise ProcedureExpressionError(parser.tokens[parser.index][2], "end of expression")
    return result


def render_procedure_expression(predicate: GuardPredicate) -> str:
    """Parenthesize connectives so printing is lossless even across precedence."""
    if predicate.not_of is not None:
        return f"not ({render_procedure_expression(predicate.not_of)})"
    children = predicate.all_of or predicate.any_of
    if children is not None:
        joiner = " and " if predicate.all_of else " or "
        return joiner.join(f"({render_procedure_expression(child)})" for child in children)
    assert predicate.left is not None and predicate.right is not None
    operator = next(key for key, value in _OPERATORS.items() if value == predicate.operator)
    return f"{_operand_text(predicate.left)} {operator} {_operand_text(predicate.right)}"


def _operand_text(value: PredicateOperand) -> str:
    if value.kind == "literal":
        return json.dumps(value.value, ensure_ascii=False)
    suffix = ".".join(value.path)
    if value.kind == "parameter":
        return f"param.{value.parameter_name}"
    root = f"input.{value.input_name}" if value.input_name else str(value.alias)
    ref = root + ("." + suffix if suffix else "")
    if value.kind == "exists":
        return f"exists(${ref})"
    if value.kind in {"count", "truncated"}:
        return f"{value.kind}({value.alias})"
    return ref
