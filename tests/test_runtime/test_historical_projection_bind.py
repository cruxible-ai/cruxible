"""Binding a projection behind the head costs the same at any history length."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface


def test_a_historical_bind_builds_no_coordinate_per_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    behind = instance.accepted_coordinate()
    _seed_claim_surface(instance, owner, contract=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT)
    assert instance.accepted_coordinate() != behind

    built = 0
    replayed = instance._accepted_coordinates_by_sequence

    def counted():  # type: ignore[no-untyped-def]
        nonlocal built
        built += 1
        return replayed()

    monkeypatch.setattr(instance, "_accepted_coordinates_by_sequence", counted)
    # A lowering at a stale base binds it once or more per Claim. Its manifest
    # was published when it was accepted: naming it needs no history.
    for _ in range(3):
        with instance.bind_accepted_projection(behind) as projection:
            assert projection.accepted.git_oid == behind.git_oid
    assert built == 0
