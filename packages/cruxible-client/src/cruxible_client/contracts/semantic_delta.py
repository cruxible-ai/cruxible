"""Pure semantic object-leaf delta shared by migration and proposal review."""

from __future__ import annotations

from typing import Final

from cruxible_client.contracts import (
    PlaybillSemanticFieldDelta,
    PlaybillSemanticFieldValue,
)
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical

_MISSING: Final = object()


def _pointer(parent: str, key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _side(value: CanonicalValue | object) -> PlaybillSemanticFieldValue:
    if value is _MISSING:
        return PlaybillSemanticFieldValue(state="absent", value=None)
    return PlaybillSemanticFieldValue(state="present", value=value)


def semantic_field_delta(
    before: dict[str, object],
    after: dict[str, object],
) -> tuple[PlaybillSemanticFieldDelta, ...]:
    """Return every changed object leaf in deterministic JSON-Pointer order.

    Objects recurse, including through wholly added or removed objects. Arrays are
    atomic. Empty containers and object/scalar type replacements are leaves.
    """

    before_value = normalize_canonical(before)
    after_value = normalize_canonical(after)
    if not isinstance(before_value, dict) or not isinstance(after_value, dict):
        raise ValueError("semantic field delta requires object roots")
    rows: list[PlaybillSemanticFieldDelta] = []

    def visit(path: str, left: CanonicalValue | object, right: CanonicalValue | object) -> None:
        if left is not _MISSING and right is not _MISSING and left == right:
            return
        left_object = isinstance(left, dict)
        right_object = isinstance(right, dict)
        if left_object and right_object:
            left_map = left
            right_map = right
            keys = sorted(set(left_map) | set(right_map), key=lambda item: item.encode("utf-8"))
            if not keys:
                return
            for key in keys:
                visit(
                    _pointer(path, key),
                    left_map.get(key, _MISSING),
                    right_map.get(key, _MISSING),
                )
            return
        if left is _MISSING and right_object and right:
            for key in sorted(right, key=lambda item: item.encode("utf-8")):
                visit(_pointer(path, key), _MISSING, right[key])
            return
        if right is _MISSING and left_object and left:
            for key in sorted(left, key=lambda item: item.encode("utf-8")):
                visit(_pointer(path, key), left[key], _MISSING)
            return
        rows.append(
            PlaybillSemanticFieldDelta(
                field_path=path,
                before=_side(left),
                after=_side(right),
            )
        )

    visit("", before_value, after_value)
    rows.sort(key=lambda item: item.field_path.encode("utf-8"))
    return tuple(rows)


__all__ = ["semantic_field_delta"]
