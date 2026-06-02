"""Deterministic helpers shared by primitive type modules.

Scope rule: only operations used by ``>=2`` primitive type modules
(``errors``, ``predicate``, ``graph/types``, ``feedback/types``,
``group/types``, ``decision/types``, ``snapshot/types``, ``provider/types``,
``receipt/types``) for stable identity or canonical serialization.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any, Callable

_JSON_TYPE_NAMES: dict[type[Any], str] = {
    type(None): "null",
    bool: "boolean",
    dict: "object",
    list: "array",
    str: "string",
    int: "number",
    float: "number",
}

def canonical_json(value: Any, *, default: Callable[[Any], Any] | None = None) -> str:
    """Serialize a JSON-compatible value to a deterministic, RFC-compliant string.

    Sorts keys, uses compact separators, preserves non-ASCII characters, and
    rejects NaN/Infinity (which have no representation in RFC 7159 JSON).
    """
    kwargs: dict[str, Any] = {}
    if default is not None:
        kwargs["default"] = default
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        **kwargs,
    )


def json_type_name(value: Any) -> str:
    """Return a JSON-schema-style type label for a runtime value."""
    return _JSON_TYPE_NAMES.get(type(value), type(value).__name__)


def new_id(prefix: str, *, length: int = 12, separator: str = "-") -> str:
    """Return a new identifier with a hex UUID suffix."""
    return f"{prefix}{separator}{uuid.uuid4().hex[:length]}"


def ordered_unique(values: Iterable[str]) -> list[str]:
    """Return unique string values preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
