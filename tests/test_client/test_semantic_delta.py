"""Frozen semantic object-leaf delta behavior."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import SemanticDeltaLimitError
from cruxible_client.contracts.semantic_delta import (
    MAX_SEMANTIC_DELTA_DEPTH,
    MAX_SEMANTIC_DELTA_ROWS,
    semantic_field_delta,
)


def test_semantic_delta_is_pointer_sorted_and_distinguishes_absent_from_null() -> None:
    rows = semantic_field_delta(
        {"same": 1, "object": {"a/b": None, "drop": True}, "array": [1, 2]},
        {"same": 1, "object": {"a/b": "value", "new~key": None}, "array": [2, 1]},
    )

    assert [row.field_path for row in rows] == [
        "/array",
        "/object/a~1b",
        "/object/drop",
        "/object/new~0key",
    ]
    assert rows[1].before.model_dump(mode="json") == {"state": "present", "value": None}
    assert rows[2].after.model_dump(mode="json") == {"state": "absent", "value": None}
    assert rows[3].after.model_dump(mode="json") == {"state": "present", "value": None}


def test_semantic_delta_expands_added_objects_but_keeps_arrays_atomic() -> None:
    rows = semantic_field_delta({}, {"new": {"empty": {}, "leaf": 3}, "values": [1, 2]})

    assert [row.field_path for row in rows] == ["/new/empty", "/new/leaf", "/values"]
    assert all(row.before.state == "absent" for row in rows)


@pytest.mark.parametrize(
    ("before", "after", "expected_path"),
    [
        ({"value": True}, {"value": 1}, "/value"),
        ({"value": 1}, {"value": True}, "/value"),
        ({"nested": {"value": False}}, {"nested": {"value": 0}}, "/nested/value"),
        ({"values": [True]}, {"values": [1]}, "/values"),
        (
            {"literal_schema": {"exclusiveMinimum": 0}},
            {"literal_schema": {"exclusiveMinimum": False}},
            "/literal_schema/exclusiveMinimum",
        ),
    ],
)
def test_semantic_delta_never_aliases_boolean_and_integer_values(
    before: dict[str, object],
    after: dict[str, object],
    expected_path: str,
) -> None:
    assert canonical_bytes(before) != canonical_bytes(after)
    assert [row.field_path for row in semantic_field_delta(before, after)] == [expected_path]


def test_different_canonical_objects_always_produce_a_delta() -> None:
    values: tuple[object, ...] = (
        None,
        False,
        True,
        0,
        1,
        "0",
        [],
        [False],
        [0],
        {},
        {"nested": False},
        {"nested": 0},
    )
    for left in values:
        for right in values:
            before = {"value": left}
            after = {"value": right}
            if canonical_bytes(before) != canonical_bytes(after):
                assert semantic_field_delta(before, after), (left, right)


def test_semantic_delta_refuses_excessive_depth_with_a_typed_error() -> None:
    value: object = 1
    for _ in range(MAX_SEMANTIC_DELTA_DEPTH + 1):
        value = {"nested": value}

    with pytest.raises(SemanticDeltaLimitError, match="maximum object depth"):
        semantic_field_delta({}, {"value": value})


def test_semantic_delta_refuses_excessive_rows_with_a_typed_error() -> None:
    after = {f"field-{index:05d}": index for index in range(MAX_SEMANTIC_DELTA_ROWS + 1)}

    with pytest.raises(SemanticDeltaLimitError, match="maximum row count"):
        semantic_field_delta({}, after)
