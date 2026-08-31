"""Frozen semantic object-leaf delta behavior."""

from __future__ import annotations

from cruxible_client.contracts.semantic_delta import semantic_field_delta


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
