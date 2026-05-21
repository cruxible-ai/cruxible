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
from typing import Any


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value to a deterministic, RFC-compliant string.

    Sorts keys, uses compact separators, preserves non-ASCII characters, and
    rejects NaN/Infinity (which have no representation in RFC 7159 JSON).
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def new_id(prefix: str) -> str:
    """Return a new record identifier of the form ``PFX-XXXXXXXXXXXX`` (12 hex chars)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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
