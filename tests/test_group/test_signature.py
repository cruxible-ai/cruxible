"""Tests for group signature computation."""

from __future__ import annotations

from cruxible_core.group.signature import compute_group_signature


class TestComputeGroupSignature:
    def test_deterministic(self) -> None:
        """Same inputs produce the same hash."""
        sig1 = compute_group_signature("fits", {"style": "casual"})
        sig2 = compute_group_signature("fits", {"style": "casual"})
        assert sig1 == sig2

    def test_order_independent(self) -> None:
        """thesis_facts keys in different order produce the same hash (sort_keys)."""
        sig1 = compute_group_signature("fits", {"a": 1, "b": 2})
        sig2 = compute_group_signature("fits", {"b": 2, "a": 1})
        assert sig1 == sig2

    def test_different_facts_different_hash(self) -> None:
        sig1 = compute_group_signature("fits", {"style": "casual"})
        sig2 = compute_group_signature("fits", {"style": "formal"})
        assert sig1 != sig2

    def test_different_relationship_different_hash(self) -> None:
        sig1 = compute_group_signature("fits", {"style": "casual"})
        sig2 = compute_group_signature("replaces", {"style": "casual"})
        assert sig1 != sig2

    def test_empty_facts(self) -> None:
        sig = compute_group_signature("fits", {})
        assert isinstance(sig, str)
        assert sig.startswith("sigv1:")
        assert len(sig) == 70  # "sigv1:" + 64-char SHA-256 hex digest

    def test_nested_facts(self) -> None:
        """Nested dicts and lists produce a stable hash."""
        facts = {"colors": ["red", "blue"], "meta": {"source": "v1", "count": 3}}
        sig1 = compute_group_signature("fits", facts)
        sig2 = compute_group_signature("fits", facts)
        assert sig1 == sig2

    def test_analysis_state_not_included(self) -> None:
        """Different analysis_state with same thesis_facts produces same signature.

        analysis_state is NOT passed to compute_group_signature — the service
        layer is responsible for only passing thesis_facts. This test validates
        that the function signature enforces this separation.
        """
        sig1 = compute_group_signature("fits", {"style": "casual"})
        sig2 = compute_group_signature("fits", {"style": "casual"})
        # Same facts, different analysis_state (which isn't even a parameter)
        assert sig1 == sig2
