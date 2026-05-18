"""Shared comparison operators and evaluation helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal

from cruxible_core.temporal import ensure_utc, parse_datetime

ComparisonOp = Literal["eq", "ne", "gt", "gte", "lt", "lte"]
PredicateValueType = Literal[
    "string",
    "int",
    "integer",
    "float",
    "number",
    "bool",
    "date",
    "datetime",
]

COMPARISON_SYMBOL_PATTERN = (
    r"(on_or_before\b|on_or_after\b|before\b|after\b|>=|<=|==|!=|>|<)"
)
CONSTRAINT_RULE_SYNTAX = (
    "RELATIONSHIP.FROM.property <op> RELATIONSHIP.TO.property "
    "where <op> is one of ==, !=, >, >=, <, <=, "
    "before, on_or_before, after, on_or_after"
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
    "before": "lt",
    "on_or_before": "lte",
    "after": "gt",
    "on_or_after": "gte",
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
    """Evaluate an untyped comparison through the shared typed predicate path."""
    return evaluate_typed_comparison(left, op, right, value_type=None)


def evaluate_typed_comparison(
    left: Any,
    op: str,
    right: Any,
    *,
    value_type: PredicateValueType | None = None,
) -> bool:
    """Evaluate a comparison after optional type-aware coercion.

    Invalid typed coercions return False. Unsupported operators still raise
    ValueError through normalize_comparison_op, matching evaluate_comparison.
    """
    normalized = normalize_comparison_op(op)
    if value_type is None:
        return _compare_values(left, normalized, right)
    try:
        coerced_left, coerced_right = _coerce_pair(left, right, value_type)
    except (TypeError, ValueError):
        return False
    return _compare_values(coerced_left, normalized, coerced_right)


def _compare_values(left: Any, normalized: ComparisonOp, right: Any) -> bool:
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


def _coerce_pair(
    left: Any,
    right: Any,
    value_type: PredicateValueType,
) -> tuple[Any, Any]:
    if value_type == "string":
        return _coerce_string(left), _coerce_string(right)
    if value_type in {"int", "integer"}:
        return _coerce_int(left), _coerce_int(right)
    if value_type in {"float", "number"}:
        return _coerce_float(left), _coerce_float(right)
    if value_type == "bool":
        return _coerce_bool(left), _coerce_bool(right)
    if value_type == "date":
        return _coerce_date(left), _coerce_date(right)
    if value_type == "datetime":
        return _coerce_datetime(left), _coerce_datetime(right)
    raise ValueError(f"Unsupported predicate value type '{value_type}'")


def _coerce_string(value: Any) -> str:
    if value is None:
        raise TypeError("None is not a string value")
    return str(value)


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("bool is not an int value")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("float is not an integer value")
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise TypeError("value is not an int value")


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise TypeError("bool is not a float value")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value.strip())
    raise TypeError("value is not a float value")


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    raise TypeError("value is not a bool value")


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return ensure_utc(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            parsed = parse_datetime(value)
            if parsed is None:
                raise ValueError("value is not a date")
            return parsed.date()
    raise TypeError("value is not a date value")


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime | str):
        parsed = parse_datetime(value)
        if parsed is None:
            raise ValueError("value is not a datetime")
        return parsed
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    raise TypeError("value is not a datetime value")
