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
