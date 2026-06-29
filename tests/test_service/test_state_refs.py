"""Tests for state alias resolution helpers."""

from __future__ import annotations

import pytest

from cruxible_core.errors import ConfigError
from cruxible_core.kits.state_refs import StateCatalogEntry, resolve_state_source


def test_transport_ref_passthrough() -> None:
    resolved = resolve_state_source(transport_ref="file:///tmp/releases/current")

    assert resolved.source_ref == "file:///tmp/releases/current"
    assert resolved.pull_transport_ref == "file:///tmp/releases/current"
    assert resolved.tracking_transport_ref == "file:///tmp/releases/current"
    assert resolved.default_kit is None
    assert resolved.alias is None


def test_state_ref_latest_uses_tracking_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
                alias="case-law",
                base_transport_ref="file:///tmp/releases",
                latest_release="current",
                default_kit="case-law-review",
            )
        },
    )

    resolved = resolve_state_source(state_ref="case-law")

    assert resolved.source_ref == "case-law"
    assert resolved.pull_transport_ref == "file:///tmp/releases/current"
    assert resolved.tracking_transport_ref == "file:///tmp/releases/current"
    assert resolved.default_kit == "case-law-review"
    assert resolved.alias == "case-law"
    assert resolved.requested_release is None


def test_state_ref_specific_release_still_tracks_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "kev-reference": StateCatalogEntry(
                alias="kev-reference",
                base_transport_ref="oci://ghcr.io/cruxible-ai/models/kev-reference",
            )
        },
    )

    resolved = resolve_state_source(state_ref="kev-reference@2026-03-27")

    assert resolved.source_ref == "kev-reference@2026-03-27"
    assert (
        resolved.pull_transport_ref == "oci://ghcr.io/cruxible-ai/models/kev-reference:2026-03-27"
    )
    assert (
        resolved.tracking_transport_ref == "oci://ghcr.io/cruxible-ai/models/kev-reference:latest"
    )
    assert resolved.requested_release == "2026-03-27"


def test_state_ref_requires_known_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cruxible_core.kits.state_refs.get_state_catalog", lambda: {})

    with pytest.raises(ConfigError, match="Unknown state_ref alias"):
        resolve_state_source(state_ref="missing")


@pytest.mark.parametrize(
    ("state_ref", "message"),
    [
        ("@v1", "alias must not be empty"),
        ("case-law@", "release must not be empty"),
        ("case law", "alias must match"),
        ("case-law@../../escape", "release must match"),
        ("case/law", "alias must match"),
    ],
)
def test_state_ref_rejects_malformed_parts(
    state_ref: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.kits.state_refs.get_state_catalog",
        lambda: {
            "case-law": StateCatalogEntry(
                alias="case-law",
                base_transport_ref="file:///tmp/releases",
                latest_release="current",
            )
        },
    )

    with pytest.raises(ConfigError, match=message):
        resolve_state_source(state_ref=state_ref)
