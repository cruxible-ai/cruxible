"""Integration coverage for real kit config composition."""

from __future__ import annotations

from pathlib import Path

from cruxible_core.config.composer import compose_config_sequence, resolve_config_layers
from cruxible_core.config.loader import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
KEV_TRIAGE_CONFIG = REPO_ROOT / "kits" / "kev-triage" / "config.yaml"


def _compose_kev(*, runtime: bool = False):
    config = load_config(KEV_TRIAGE_CONFIG)
    return compose_config_sequence(
        resolve_config_layers(config, config_path=KEV_TRIAGE_CONFIG),
        runtime=runtime,
    )


def test_kev_triage_composes_reference_and_overlay_configs() -> None:
    composed = _compose_kev()

    assert {"Vendor", "Product", "Vulnerability"}.issubset(composed.entity_types)
    assert {"Asset", "CompensatingControl", "VulnerabilityClass", "Exception"}.issubset(
        composed.entity_types
    )
    assert "Incident" not in composed.entity_types
    assert "Finding" not in composed.entity_types
    assert "vulnerability_affects_product" in {
        rel.name for rel in composed.relationships
    }
    assert "asset_runs_product" in {rel.name for rel in composed.relationships}
    assert "build_public_kev_reference" in composed.workflows
    assert "build_local_state" in composed.workflows
    assert "propose_asset_exposure" in composed.workflows


def test_kev_triage_runtime_composition_strips_reference_build_surface() -> None:
    composed = _compose_kev(runtime=True)

    assert {"Vendor", "Product", "Vulnerability"}.issubset(composed.entity_types)
    assert {"Asset", "CompensatingControl", "VulnerabilityClass", "Exception"}.issubset(
        composed.entity_types
    )
    assert "Incident" not in composed.entity_types
    assert "Finding" not in composed.entity_types
    assert "build_public_kev_reference" not in composed.workflows
    assert "parse_public_kev_bundle" not in composed.providers
    assert "normalize_public_kev_reference" not in composed.providers
    assert "build_local_state" in composed.workflows
    assert "propose_asset_products" in composed.workflows
    assert "propose_exposure_reconciliation" in composed.workflows
