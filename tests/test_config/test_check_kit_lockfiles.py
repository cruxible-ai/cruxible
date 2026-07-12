"""Tests for scripts/check_kit_lockfiles.py — the CI kit-lockfile freshness gate.

The script must pass on the committed tree without touching it, and fail with
an instructive, nonzero exit when a lock is missing/stale or the packaged
distribution manifest drifted. Staleness is simulated in tmp kit copies only;
the real ``kits/`` tree is never modified.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from cruxible_core import __version__
from cruxible_core.workflow.compiler import LOCK_FILE_NAME, build_kit_root_lock, write_lock

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED_MANIFEST_PATH = (
    _REPO_ROOT / "src" / "cruxible_core" / "kit_distribution" / "manifest.json"
)
_DEMO_KIT_ID = "demo-check"


def _load_script(name: str) -> ModuleType:
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_demo_kit(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.joinpath("cruxible-kit.yaml").write_text(
        "schema_version: cruxible.kit.v1\n"
        f"kit_id: {_DEMO_KIT_ID}\n"
        "version: 0.2.0\n"
        "role: standalone\n"
        "entry_config: config.yaml\n"
        "provider_paths: []\n"
        "copy_paths: []\n"
        "requires_extras: []\n"
    )
    # Minimal but loadable standalone config: build_kit_root_lock runs the real
    # load_config, which rejects empty entity_types outside overlay layers.
    root.joinpath("config.yaml").write_text(
        "version: '1.0'\n"
        "name: demo\n"
        "entity_types:\n"
        "  Widget:\n"
        "    properties:\n"
        "      widget_id:\n"
        "        type: string\n"
        "        primary_key: true\n"
        "relationships: []\n"
    )


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A consistent tmp kits tree: demo kit + real lock + fresh manifest."""
    builder = _load_script("build_kit_bundles")
    kits_root = tmp_path / "kits"
    kit_dir = kits_root / _DEMO_KIT_ID
    _write_demo_kit(kit_dir)
    write_lock(build_kit_root_lock(kit_dir), kit_dir / LOCK_FILE_NAME)
    bundles = [
        builder.build_kit_bundle(child, tmp_path / "dist", __version__)
        for child in builder.iter_kit_dirs(kits_root)
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(builder.build_manifest(bundles, __version__), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return kits_root, manifest_path, tmp_path / "dist"


def _run(script: ModuleType, kits_root: Path, manifest_path: Path, dist_dir: Path) -> int:
    return script.main(
        [
            "--kits-root",
            str(kits_root),
            "--manifest-path",
            str(manifest_path),
            "--dist-dir",
            str(dist_dir),
        ]
    )


def test_committed_tree_passes_without_modification() -> None:
    script = _load_script("check_kit_lockfiles")
    lock_paths = sorted(_REPO_ROOT.glob("kits/*/cruxible.lock.yaml"))
    assert lock_paths, "repo kits should carry committed locks"
    before = {path: path.read_bytes() for path in lock_paths}
    before[_PACKAGED_MANIFEST_PATH] = _PACKAGED_MANIFEST_PATH.read_bytes()

    assert script.main([]) == 0

    for path, content in before.items():
        assert path.read_bytes() == content, f"check dirtied committed file: {path}"


def test_fixture_tree_passes(tmp_path: Path) -> None:
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    assert _run(script, kits_root, manifest_path, dist_dir) == 0


def test_missing_lock_fails_with_instructive_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    (kits_root / _DEMO_KIT_ID / LOCK_FILE_NAME).unlink()

    assert _run(script, kits_root, manifest_path, dist_dir) == 1

    err = capsys.readouterr().err
    assert f"missing {LOCK_FILE_NAME}" in err
    assert f"cruxible lock --kit-dir kits/{_DEMO_KIT_ID}" in err
    assert "scripts/build_kit_bundles.py" in err


def test_stale_lock_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Config edited after the lock was generated: the committed lock's digest
    # no longer matches a fresh build_kit_root_lock.
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    config_path = kits_root / _DEMO_KIT_ID / "config.yaml"
    config_path.write_text(config_path.read_text().replace("name: demo\n", "name: demo-edited\n"))

    assert _run(script, kits_root, manifest_path, dist_dir) == 1

    err = capsys.readouterr().err
    assert f"{LOCK_FILE_NAME} is stale" in err
    assert f"cruxible lock --kit-dir kits/{_DEMO_KIT_ID}" in err


def test_stale_manifest_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Kit content edited after bundling (lock untouched — README is not lock
    # input) must trip the manifest check, the exact 0.2.1 stale-manifest gap.
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    (kits_root / _DEMO_KIT_ID / "README.md").write_text("edited after bundling\n")

    assert _run(script, kits_root, manifest_path, dist_dir) == 1

    err = capsys.readouterr().err
    assert "is stale" in err
    assert f"kit entry drifted: {_DEMO_KIT_ID}" in err
    assert "uv run python scripts/build_kit_bundles.py" in err


def test_stale_manifest_version_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The released-0.2.1 failure mode: versions bumped, bundles never rebuilt.
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    stale = json.loads(manifest_path.read_text(encoding="utf-8"))
    stale["version"] = "0.0.0-stale"
    manifest_path.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert _run(script, kits_root, manifest_path, dist_dir) == 1

    err = capsys.readouterr().err
    assert f"'0.0.0-stale' != package '{__version__}'" in err


def test_stale_dist_tarball_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    script = _load_script("check_kit_lockfiles")
    kits_root, manifest_path, dist_dir = _fixture_tree(tmp_path)
    asset = dist_dir / f"{_DEMO_KIT_ID}-{__version__}.tar.gz"
    asset.write_bytes(asset.read_bytes() + b"stale")

    assert _run(script, kits_root, manifest_path, dist_dir) == 1

    err = capsys.readouterr().err
    assert "dist tarball is stale" in err
