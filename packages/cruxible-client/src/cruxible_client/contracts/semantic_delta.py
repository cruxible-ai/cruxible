"""Pure semantic object-leaf delta shared by migration and proposal review."""

from __future__ import annotations

from typing import Final, cast

from cruxible_client.contracts import (
    PlaybillSemanticFieldDelta,
    PlaybillSemanticFieldValue,
)
from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.errors import SemanticDeltaLimitError

_MISSING: Final = object()
MAX_SEMANTIC_DELTA_DEPTH: Final = 64
MAX_SEMANTIC_DELTA_ROWS: Final = 4096


def _pointer(parent: str, key: str) -> str:
    escaped = key.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _side(value: CanonicalValue | object) -> PlaybillSemanticFieldValue:
    if value is _MISSING:
        return PlaybillSemanticFieldValue(state="absent", value=None)
    return PlaybillSemanticFieldValue(state="present", value=cast(CanonicalValue, value))


def _require_bounded_depth(value: object, *, depth: int = 0) -> None:
    if depth > MAX_SEMANTIC_DELTA_DEPTH:
        raise SemanticDeltaLimitError(
            f"{SemanticDeltaLimitError.error_code}: maximum object depth is "
            f"{MAX_SEMANTIC_DELTA_DEPTH}"
        )
    if isinstance(value, dict):
        for item in value.values():
            _require_bounded_depth(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _require_bounded_depth(item, depth=depth + 1)


def _same_canonical_value(left: CanonicalValue, right: CanonicalValue) -> bool:
    """Compare JSON values without Python's bool/int numeric aliasing."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        right_map = cast(dict[str, CanonicalValue], right)
        return left.keys() == right_map.keys() and all(
            _same_canonical_value(value, right_map[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        right_list = cast(list[CanonicalValue], right)
        return len(left) == len(right_list) and all(
            _same_canonical_value(left_item, right_item)
            for left_item, right_item in zip(left, right_list, strict=True)
        )
    return left == right


def semantic_field_delta(
    before: dict[str, object],
    after: dict[str, object],
) -> tuple[PlaybillSemanticFieldDelta, ...]:
    """Return every changed object leaf in deterministic JSON-Pointer order.

    Objects recurse, including through wholly added or removed objects. Arrays are
    atomic. Empty containers and object/scalar type replacements are leaves.
    """

    _require_bounded_depth(before)
    _require_bounded_depth(after)
    before_value = normalize_canonical(before)
    after_value = normalize_canonical(after)
    if not isinstance(before_value, dict) or not isinstance(after_value, dict):
        raise ValueError("semantic field delta requires object roots")
    rows: list[PlaybillSemanticFieldDelta] = []

    def visit(path: str, left: CanonicalValue | object, right: CanonicalValue | object) -> None:
        if (
            left is not _MISSING
            and right is not _MISSING
            and _same_canonical_value(cast(CanonicalValue, left), cast(CanonicalValue, right))
        ):
            return
        left_object = isinstance(left, dict)
        right_object = isinstance(right, dict)
        if left_object and right_object:
            left_map = cast(dict[str, CanonicalValue], left)
            right_map = cast(dict[str, CanonicalValue], right)
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
            right_map = cast(dict[str, CanonicalValue], right)
            for key in sorted(right_map, key=lambda item: item.encode("utf-8")):
                visit(_pointer(path, key), _MISSING, right_map[key])
            return
        if right is _MISSING and left_object and left:
            left_map = cast(dict[str, CanonicalValue], left)
            for key in sorted(left_map, key=lambda item: item.encode("utf-8")):
                visit(_pointer(path, key), left_map[key], _MISSING)
            return
        if len(rows) >= MAX_SEMANTIC_DELTA_ROWS:
            raise SemanticDeltaLimitError(
                f"{SemanticDeltaLimitError.error_code}: maximum row count is "
                f"{MAX_SEMANTIC_DELTA_ROWS}"
            )
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


__all__ = [
    "MAX_SEMANTIC_DELTA_DEPTH",
    "MAX_SEMANTIC_DELTA_ROWS",
    "semantic_field_delta",
]
