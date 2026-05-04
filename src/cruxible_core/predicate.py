"""Shared comparison operators and evaluation helpers."""

from __future__ import annotations

from typing import Any, Literal

ComparisonOp = Literal["eq", "ne", "gt", "gte", "lt", "lte"]

COMPARISON_SYMBOL_PATTERN = r"(>=|<=|==|!=|>|<)"
CONSTRAINT_RULE_SYNTAX = (
    "RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property "
    "where <op> is one of ==, !=, >, >=, <, <="
)

_SYMBOL_TO_OP: dict[str, ComparisonOp] = {
    "==": "eq",
    "!=": "ne",
    ">": "gt",
    ">=": "gte",
    "<": "lt",
    "<=": "lte",
}
_OP_TO_SYMBOL: dict[ComparisonOp, str] = {value: key for key, value in _SYMBOL_TO_OP.items()}
_ALIAS_TO_OP: dict[str, ComparisonOp] = {
    **_SYMBOL_TO_OP,
    "eq": "eq",
    "ne": "ne",
    "gt": "gt",
    "gte": "gte",
    "lt": "lt",
    "lte": "lte",
}


def normalize_comparison_op(op: str) -> ComparisonOp:
    """Normalize symbolic or semantic operator names to a ComparisonOp."""
    normalized = _ALIAS_TO_OP.get(op)
    if normalized is None:
        raise ValueError(f"Unsupported comparison operator '{op}'")
    return normalized


def comparison_symbol(op: str) -> str:
    """Return the symbolic form for a normalized comparison operator."""
    normalized = normalize_comparison_op(op)
    return _OP_TO_SYMBOL[normalized]


def evaluate_comparison(left: Any, op: str, right: Any) -> bool:
    """Evaluate a comparison, treating incomparable ordered values as False."""
    normalized = normalize_comparison_op(op)
    if normalized == "eq":
        return bool(left == right)
    if normalized == "ne":
        return bool(left != right)

    try:
        if normalized == "gt":
            return bool(left > right)
        if normalized == "gte":
            return bool(left >= right)
        if normalized == "lt":
            return bool(left < right)
        # normalized == "lte"
        return bool(left <= right)
    except TypeError:
        return False
