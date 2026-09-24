"""Owner facts are compiled once per version and equal a fresh compile."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.captures import foreign_source_capture_contract
from cruxible_core.indexes.typed_state import TypedStateReader
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface


def test_memoized_facts_equal_a_fresh_compile_and_are_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance, owner, contract=foreign_source_capture_contract("repo.work-items")
    )
    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        rows = projection.typed.envelopes()
        assert rows
        first = projection.typed.facts_for(rows)
        assert first == projection.typed._compile_facts(rows)

    def refuse(self, paths):  # type: ignore[no-untyped-def]
        raise AssertionError(f"recompiled {paths}")

    monkeypatch.setattr(TypedStateReader, "_compile_paths", refuse)
    with instance.bind_accepted_projection(coordinate) as projection:
        assert projection.typed.facts_for(rows) == first


def test_a_caller_changing_returned_facts_never_changes_a_later_read(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(
        instance, owner, contract=foreign_source_capture_contract("repo.work-items")
    )
    coordinate = instance.accepted_coordinate()

    def read():
        with instance.bind_accepted_projection(coordinate) as projection:
            rows = projection.typed.envelopes()
            return rows, projection.typed.facts_for(rows)

    rows, first = read()
    original = {
        identity: [fact.model_dump() for fact in facts] for identity, facts in first.items()
    }
    mutated = 0
    for facts in first.values():
        for fact in facts:
            if isinstance(fact.value, dict):
                fact.value["tampered"] = True
                mutated += 1
            elif isinstance(fact.value, list):
                fact.value.append("tampered")
                mutated += 1
    assert mutated
    for reread in (read()[1], read()[1]):
        assert {
            identity: [fact.model_dump() for fact in facts] for identity, facts in reread.items()
        } == original
