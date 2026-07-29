"""Focused CLI tests for intentional materialized-kit runtime re-pinning."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.kits import compute_kit_runtime_digest, write_materialized_kit_metadata


def _write_kit(root: Path, *, materialized: bool) -> Path:
    root.mkdir()
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        "kit_id: demo\n"
        "version: 0.3.0\n"
        "role: standalone\n"
        "entry_config: config.yaml\n"
        "provider_paths:\n"
        "  - providers\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\nname: demo\nentity_types: {}\nrelationships: []\n"
    )
    providers = root / "providers"
    providers.mkdir()
    provider = providers / "main.py"
    provider.write_text("def run(_input, _context):\n    return {}\n")
    if materialized:
        write_materialized_kit_metadata(root, bundle_digest="sha256:bundle")
    return provider


def test_kit_repin_updates_runtime_digest_and_prints_transition(tmp_path: Path) -> None:
    kit_root = tmp_path / "demo"
    provider = _write_kit(kit_root, materialized=True)
    metadata_path = kit_root / ".cruxible" / "kit.json"
    old_digest = json.loads(metadata_path.read_text())["runtime_digest"]
    provider.write_text("def run(_input, _context):\n    return {'edited': True}\n")

    result = CliRunner().invoke(cli, ["kit", "repin", "--kit-dir", str(kit_root)])

    assert result.exit_code == 0, result.output
    new_digest = compute_kit_runtime_digest(kit_root)
    assert old_digest != new_digest
    assert f"{old_digest} -> {new_digest}" in result.output
    metadata = json.loads(metadata_path.read_text())
    assert metadata["runtime_digest"] == new_digest
    assert metadata["bundle_digest"] == "sha256:bundle"


def test_kit_repin_defaults_to_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kit_root = tmp_path / "demo"
    provider = _write_kit(kit_root, materialized=True)
    provider.write_text("def run(_input, _context):\n    return {'edited': True}\n")
    monkeypatch.chdir(kit_root)

    result = CliRunner().invoke(cli, ["kit", "repin"])

    assert result.exit_code == 0, result.output
    assert "Re-pinned kit runtime digest:" in result.output


def test_kit_repin_refuses_to_materialize_an_untracked_kit(tmp_path: Path) -> None:
    kit_root = tmp_path / "demo"
    _write_kit(kit_root, materialized=False)

    result = CliRunner().invoke(cli, ["kit", "repin", "--kit-dir", str(kit_root)])

    assert result.exit_code == 1
    assert "Kit materialization creates .cruxible/kit.json" in result.output
    assert not (kit_root / ".cruxible" / "kit.json").exists()
