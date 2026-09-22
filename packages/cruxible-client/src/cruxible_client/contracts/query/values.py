"""Shared declared-query value semantics, including historical comparison forms."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from cruxible_client.contracts.query.grammar import QueryValueTypeV1


class QueryTypedValue(NamedTuple):
    ok: bool
    value: object


def _timestamp(value: object) -> QueryTypedValue:
    if not isinstance(value, str):
        return QueryTypedValue(False, None)
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return QueryTypedValue(False, None)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return QueryTypedValue(False, None)
    return QueryTypedValue(True, parsed)


def coerce_query_value(value: object, value_type: QueryValueTypeV1) -> QueryTypedValue:
    """Return the declared type's comparable form, or a typed mismatch."""

    if value_type == "boolean":
        return QueryTypedValue(isinstance(value, bool), value)
    if value_type == "integer":
        return QueryTypedValue(isinstance(value, int) and not isinstance(value, bool), value)
    if value_type in {"string", "subject_reference"}:
        if not isinstance(value, str):
            return QueryTypedValue(False, None)
        return QueryTypedValue(True, value.encode("utf-8"))
    if value_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return QueryTypedValue(False, None)
        try:
            return QueryTypedValue(True, Decimal(value))
        except (InvalidOperation, ValueError):
            return QueryTypedValue(False, None)
    return _timestamp(value)
