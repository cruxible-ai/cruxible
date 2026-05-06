"""Tests for the primitive helpers shared by primitive type modules."""

from __future__ import annotations

import re

from cruxible_core.primitives import canonical_json, new_id


class TestCanonicalJson:
    def test_sorts_top_level_keys(self) -> None:
        assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_sorts_nested_keys(self) -> None:
        assert (
            canonical_json({"outer": {"z": 1, "a": 2}})
            == '{"outer":{"a":2,"z":1}}'
        )

    def test_byte_equal_across_insertion_orders(self) -> None:
        a = canonical_json({"x": 1, "y": 2, "z": 3})
        b = canonical_json({"z": 3, "y": 2, "x": 1})
        assert a == b

    def test_compact_separators(self) -> None:
        encoded = canonical_json({"a": 1, "b": [2, 3]})
        assert ", " not in encoded
        assert ": " not in encoded
        assert encoded == '{"a":1,"b":[2,3]}'

    def test_unicode_passthrough(self) -> None:
        encoded = canonical_json({"label": "café"})
        assert "café" in encoded
        assert "\\u" not in encoded

    def test_lists_preserve_order(self) -> None:
        assert canonical_json([3, 1, 2]) == "[3,1,2]"

    def test_primitives(self) -> None:
        assert canonical_json(None) == "null"
        assert canonical_json(True) == "true"
        assert canonical_json(42) == "42"
        assert canonical_json("x") == '"x"'

    def test_rejects_non_serializable(self) -> None:
        try:
            canonical_json({"k": object()})
        except TypeError:
            return
        raise AssertionError("expected TypeError on non-JSON-serializable input")

    def test_rejects_nan(self) -> None:
        try:
            canonical_json({"x": float("nan")})
        except ValueError:
            return
        raise AssertionError("expected ValueError on NaN (not RFC 7159 compliant)")

    def test_rejects_infinity(self) -> None:
        try:
            canonical_json({"x": float("inf")})
        except ValueError:
            return
        raise AssertionError("expected ValueError on Infinity")


class TestNewId:
    _ID_PATTERN = re.compile(r"^[A-Z]+-[0-9a-f]{12}$")

    def test_format_default_prefix(self) -> None:
        result = new_id("FB")
        assert result.startswith("FB-")
        assert len(result) == len("FB-") + 12

    def test_format_matches_canonical_pattern(self) -> None:
        for prefix in ("FB", "OUT", "DR", "DE", "TRC", "RCP", "GRP", "RES"):
            assert self._ID_PATTERN.match(new_id(prefix)), f"bad shape for {prefix}"

    def test_uniqueness(self) -> None:
        ids = {new_id("X") for _ in range(1000)}
        assert len(ids) == 1000

    def test_lowercase_hex_suffix(self) -> None:
        suffix = new_id("X").split("-", 1)[1]
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_accepts_arbitrary_prefix(self) -> None:
        # The helper does not enforce a prefix shape; callers own that convention.
        assert new_id("X").startswith("X-")
        assert new_id("ABCDE").startswith("ABCDE-")
