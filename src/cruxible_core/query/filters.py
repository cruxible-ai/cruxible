"""Shared exact-match helpers for query traversal and decision policies."""

from __future__ import annotations

from typing import Any


def matches_exact_filter(
    actual_values: dict[str, Any],
    filter_spec: dict[str, Any],
) -> bool:
    """Check whether a dict satisfies a scalar-or-membership filter spec."""
    for key, expected in filter_spec.items():
        actual = actual_values.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True
