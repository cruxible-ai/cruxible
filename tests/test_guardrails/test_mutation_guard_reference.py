"""Keep documented mutation-guard condition types synchronized with config schema."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import TypeAdapter

from cruxible_core.config.schema import MutationGuardConditionSchema


def _documented_condition_types(path: str | Path) -> set[str]:
    """Extract condition discriminators from the mutation-guard type table."""
    text = Path(path).read_text()
    marker = "The `condition.type` discriminator selects the condition variant:"
    _, separator, tail = text.partition(marker)
    assert separator, "config-reference.md is missing the mutation-guard condition table"
    table_region = tail.split("\n### ", maxsplit=1)[0]
    condition_types = set(re.findall(r"^\| `(?P<type>[a-z_]+)` \|", table_region, re.MULTILINE))
    condition_types.discard("type")
    return condition_types


def _implemented_condition_types() -> set[str]:
    """Read the discriminator mapping from the authoritative Pydantic union."""
    schema = TypeAdapter(MutationGuardConditionSchema).json_schema()
    discriminator = schema.get("discriminator")
    assert isinstance(discriminator, dict), "mutation-guard union has no discriminator schema"
    mapping = discriminator.get("mapping")
    assert isinstance(mapping, dict), "mutation-guard discriminator has no mapping"
    return set(mapping)


def test_config_reference_condition_types_set_equal_schema_union() -> None:
    """Every implemented mutation-guard condition has exactly one table row."""
    documented = _documented_condition_types("docs/config-reference.md")
    implemented = _implemented_condition_types()

    code_not_docs = sorted(implemented - documented)
    docs_not_code = sorted(documented - implemented)
    assert code_not_docs == [], (
        "Mutation-guard condition types missing from docs/config-reference.md "
        f"(in code, not docs): {code_not_docs}"
    )
    assert docs_not_code == [], (
        "docs/config-reference.md documents mutation-guard condition types absent "
        f"from the config schema (in docs, not code): {docs_not_code}"
    )
