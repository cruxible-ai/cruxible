"""Unit tests for declared entity-identity normalization."""

from __future__ import annotations

import pytest

from cruxible_core.graph.entity_identity import _identity_key, normalize_identity_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Bluest   Account  ", "bluest account"),
        ("BLUEST, Account!", "bluest account"),
        ("Blue-st_Account", "bluestaccount"),
        ("Straße", "strasse"),
        ("Cafe\N{COMBINING ACUTE ACCENT}", "café"),
        ("First\tSecond\nThird", "first second third"),
        ("¿Qué?", "qué"),
    ],
)
def test_normalize_identity_value(raw: str, expected: str) -> None:
    assert normalize_identity_value(raw) == expected


def test_normalize_identity_value_requires_a_string() -> None:
    with pytest.raises(TypeError, match="identity values must be strings"):
        normalize_identity_value(42)  # type: ignore[arg-type]


def test_identity_key_skips_when_every_normalized_component_is_empty() -> None:
    assert _identity_key({"name": "!!!", "family": "  —  "}, ["name", "family"]) is None
