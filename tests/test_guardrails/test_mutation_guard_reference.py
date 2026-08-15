"""Freeze the legacy mutation-guard donor vocabulary until its parity batch."""

from __future__ import annotations

from pydantic import TypeAdapter

from cruxible_core.config.schema import MutationGuardConditionSchema


def _implemented_condition_types() -> set[str]:
    """Read the discriminator mapping from the authoritative Pydantic union."""
    schema = TypeAdapter(MutationGuardConditionSchema).json_schema()
    discriminator = schema.get("discriminator")
    assert isinstance(discriminator, dict), "mutation-guard union has no discriminator schema"
    mapping = discriminator.get("mapping")
    assert isinstance(mapping, dict), "mutation-guard discriminator has no mapping"
    return set(mapping)


def test_mutation_guard_donor_condition_vocabulary_is_frozen() -> None:
    """Destructive doc removal must not silently change donor semantics."""
    assert _implemented_condition_types() == {
        "actor",
        "co_write",
        "evidence",
        "frozen",
        "query",
        "requires_resolution_contract",
    }
