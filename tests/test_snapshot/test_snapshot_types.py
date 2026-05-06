"""Tests for snapshot and published-world metadata invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.snapshot.types import PublishedWorldManifest


def _manifest(*, world_id: str = "case-law", release_id: str = "v1.0.0") -> PublishedWorldManifest:
    return PublishedWorldManifest(
        world_id=world_id,
        release_id=release_id,
        snapshot_id="snap_1",
        compatibility="data_only",
    )


@pytest.mark.parametrize("value", ["", ".", "..", ".hidden"])
def test_world_id_rejects_dot_relative_values(value: str) -> None:
    with pytest.raises(ValidationError, match="world_id"):
        _manifest(world_id=value)


@pytest.mark.parametrize("value", ["", ".", "..", ".hidden"])
def test_release_id_rejects_dot_relative_values(value: str) -> None:
    with pytest.raises(ValidationError, match="release_id"):
        _manifest(release_id=value)


@pytest.mark.parametrize("world_id", ["case-law", "acme-2025-q1"])
def test_world_id_accepts_normal_identifiers(world_id: str) -> None:
    assert _manifest(world_id=world_id).world_id == world_id


@pytest.mark.parametrize("release_id", ["v1.0.0", "acme-2025-q1"])
def test_release_id_accepts_normal_identifiers(release_id: str) -> None:
    assert _manifest(release_id=release_id).release_id == release_id
