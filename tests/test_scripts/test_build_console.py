"""scripts/build_console.py refuses a checkout it cannot vouch for, and installs a manifest."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from cruxible_core.console.bundle import MANIFEST_NAME, load_bundle

REPO_ROOT = Path(__file__).resolve().parents[2]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_console", REPO_ROOT / "scripts" / "build_console.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _checkout(tmp_path: Path, pin: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "console"
    (repo / "api").mkdir(parents=True)
    (repo / "api" / "pin.json").write_text(json.dumps({"core_commit": "x", **pin}))
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_a_clean_checkout_pinned_to_this_core_passes(tmp_path: Path) -> None:
    script = _script()
    repo, head = _checkout(tmp_path, script.core_pin())
    identity = script.check_checkout(repo, head[:12], allow_pin_mismatch=False)
    assert identity["console_commit"] == head
    assert identity["pin_matches_core"] is True
    assert identity["console_pin"]["core_commit"] == "x"


def test_wrong_commit_dirty_tree_env_files_and_pin_mismatch_are_refused(tmp_path: Path) -> None:
    script = _script()
    pin = script.core_pin()
    repo, head = _checkout(tmp_path, {**pin, "sdk_contract_snapshot_digest": "sha256:other"})

    with pytest.raises(script.BuildRefused, match="pinned to cruxible"):
        script.check_checkout(repo, head, allow_pin_mismatch=False)
    loose = script.check_checkout(repo, head, allow_pin_mismatch=True)
    assert loose["pin_matches_core"] is False

    (repo / "later.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "later")
    with pytest.raises(script.BuildRefused, match="not the expected commit"):
        script.check_checkout(repo, head, allow_pin_mismatch=True)
    head = _git(repo, "rev-parse", "HEAD")

    (repo / "later.txt").write_text("changed")
    with pytest.raises(script.BuildRefused, match="uncommitted changes"):
        script.check_checkout(repo, head, allow_pin_mismatch=True)
    _git(repo, "checkout", "-q", "--", "later.txt")

    (repo / ".env.local").write_text("VITE_CRUXIBLE_API_TOKEN=secret\n")
    with pytest.raises(script.BuildRefused, match=r"\.env\.local"):
        script.check_checkout(repo, head, allow_pin_mismatch=True)


def test_install_writes_a_manifest_the_listener_loads_and_never_clobbers_other_files(
    tmp_path: Path,
) -> None:
    script = _script()
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>")
    (dist / "assets" / "a.js").write_text("1")
    out = tmp_path / "console_dist"

    manifest = script.install_bundle(dist, out, {"console_commit": "c" * 40})
    assert set(manifest["files"]) == {"index.html", "assets/a.js"}
    bundle = load_bundle(out)
    assert bundle.manifest["console_commit"] == "c" * 40
    # Reinstalling replaces a previous bundle.
    (dist / "assets" / "a.js").write_text("2")
    script.install_bundle(dist, out, {"console_commit": "d" * 40})
    assert load_bundle(out).manifest["console_commit"] == "d" * 40

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")
    with pytest.raises(script.BuildRefused, match=f"no {MANIFEST_NAME}"):
        script.install_bundle(dist, unrelated, {})
    assert (unrelated / "keep.txt").read_text() == "keep"
