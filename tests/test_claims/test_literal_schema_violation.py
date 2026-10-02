"""literal_schema_violation names the first constraint a literal breaks, with what it got,
and reads exactly the keywords the Claim law reads plus the declared numeric bounds."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.claims import literal_satisfies_schema, literal_schema_violation


@pytest.mark.parametrize(
    ("schema", "value", "named"),
    [
        ({"type": "string", "maxLength": 200}, "x" * 230, "maxLength 200, got 230"),
        ({"type": "string", "minLength": 3}, "ab", "minLength 3, got 2"),
        ({"type": "string", "pattern": "^a"}, "ba", 'pattern "^a", got "ba"'),
        ({"enum": ["x", "y"]}, "z", 'enum ["x", "y"], got "z"'),
        ({"const": 4}, 5, "const 4, got 5"),
        ({"type": "integer"}, "5", 'type integer, got string "5"'),
        ({"type": "integer"}, True, "type integer, got boolean true"),
        ({"type": "number", "minimum": 0.5}, 0, "minimum 0.5, got 0"),
        ({"type": "integer", "maximum": 10}, 11, "maximum 10, got 11"),
        ({"type": "integer", "exclusiveMinimum": 1}, 1, "exclusiveMinimum 1, got 1"),
        ({"type": "integer", "exclusiveMaximum": 1}, 1, "exclusiveMaximum 1, got 1"),
        (
            {"type": "object", "required": ["a", "b"]},
            {"a": 1},
            'required ["b"], missing "b"',
        ),
        (
            {"type": "object", "properties": {"n": {"type": "string", "maxLength": 2}}},
            {"n": "abc"},
            "at n: maxLength 2, got 3",
        ),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"extra": 1},
            'additionalProperties false, got "extra"',
        ),
        (
            {"type": "array", "items": {"type": "integer", "maximum": 3}},
            [1, 2, 9],
            "at [2]: maximum 3, got 9",
        ),
    ],
)
def test_each_constraint_kind_is_named_with_the_actual_value(
    schema: dict[str, object], value: object, named: str
) -> None:
    assert literal_schema_violation(value, schema) == named


def test_a_long_value_is_quoted_short() -> None:
    named = literal_schema_violation("q" * 300, {"type": "string", "pattern": "^a"})
    assert named is not None and len(named) < 120 and named.endswith("…")


@pytest.mark.parametrize(
    ("schema", "value"),
    [
        ({"type": "string", "maxLength": 3}, "abc"),
        ({"type": "integer", "minimum": 1, "maximum": 3}, 3),
        ({"enum": [1, 2]}, 2),
        ({"type": "array", "items": {"type": "string"}}, ["a"]),
    ],
)
def test_a_satisfying_value_names_nothing(schema: dict[str, object], value: object) -> None:
    assert literal_schema_violation(value, schema) is None
    assert literal_satisfies_schema(value, schema)


def test_the_claim_law_still_leaves_numeric_bounds_to_the_writers() -> None:
    """The law's reading is unchanged: it does not read minimum or maximum."""

    schema = {"type": "integer", "minimum": 1}
    assert literal_satisfies_schema(0, schema)
    assert literal_schema_violation(0, schema) == "minimum 1, got 0"
