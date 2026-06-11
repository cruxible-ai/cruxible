"""Tests for snapshot and published-state metadata invariants."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.snapshot.types import PublishedStateManifest


def _manifest(*, state_id: str = "case-law", release_id: str = "v1.0.0") -> PublishedStateManifest:
    return PublishedStateManifest(
        state_id=state_id,
        release_id=release_id,
        snapshot_id="snap_1",
        compatibility="data_only",
    )


@pytest.mark.parametrize("value", ["", ".", "..", ".hidden"])
def test_state_id_rejects_dot_relative_values(value: str) -> None:
    with pytest.raises(ValidationError, match="state_id"):
        _manifest(state_id=value)


@pytest.mark.parametrize("value", ["", ".", "..", ".hidden"])
def test_release_id_rejects_dot_relative_values(value: str) -> None:
    with pytest.raises(ValidationError, match="release_id"):
        _manifest(release_id=value)


@pytest.mark.parametrize("state_id", ["case-law", "acme-2025-q1"])
def test_state_id_accepts_normal_identifiers(state_id: str) -> None:
    assert _manifest(state_id=state_id).state_id == state_id


@pytest.mark.parametrize("release_id", ["v1.0.0", "acme-2025-q1"])
def test_release_id_accepts_normal_identifiers(release_id: str) -> None:
    assert _manifest(release_id=release_id).release_id == release_id
