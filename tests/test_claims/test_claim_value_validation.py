"""Claim values use the native grammar, including bounds and prose refusals."""

import pytest

from cruxible_client.contracts.shapes import claim_literal_problem, parse_type


@pytest.mark.parametrize(
    "type_name,value,problem",
    [
        ("string{max_len: 3}", "long", "out_of_range"),
        ("string{min_len: 3}", "ab", "out_of_range"),
        ('string{pattern: "a.*"}', "ba", "pattern_mismatch"),
        ("enum[x, y]", "z", "not_in_enum"),
        ("int", "5", "type_mismatch"),
        ("int", True, "type_mismatch"),
        ("int{1..3}", 4, "out_of_range"),
        ("int{1..3}", 0, "out_of_range"),
        (
            "string{max_len: 256}",
            "first\nsecond",
            "multiline_string: use a Document section for prose",
        ),
    ],
)
def test_native_constraint_failures(type_name, value, problem):
    assert claim_literal_problem(value, parse_type(type_name)) == problem


@pytest.mark.parametrize(
    "type_name,value",
    [
        ("string{max_len: 3}", "abc"),
        ("int{1..3}", 3),
        ("enum[x, y]", "x"),
        ("list<int>{max_items: 2}", [1, 2]),
    ],
)
def test_native_valid_values(type_name, value):
    assert claim_literal_problem(value, parse_type(type_name)) is None
