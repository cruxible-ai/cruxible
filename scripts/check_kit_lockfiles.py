"""Verify committed kit lockfiles and the packaged kit distribution manifest are current.

Usage:
    uv run python scripts/check_kit_lockfiles.py

One command for devs, the release checklist, and CI. Three checks, all
read-only against the repo (fresh locks are built in memory via
``build_kit_root_lock`` — the same path ``cruxible lock --kit-dir`` writes
through — and fresh bundles go to a temporary directory, so a run never
dirties the working tree and is safe to repeat):

1. Every ``kits/<id>/`` with a ``cruxible-kit.yaml`` has a committed
   ``cruxible.lock.yaml``.
2. Each committed lock is current: its content digest (``compute_lock_digest``,
   which excludes the volatile ``generated_at`` timestamp by design) matches a
   fresh ``build_kit_root_lock`` of the kit directory, i.e. regenerating the
   lock would be a semantic no-op.
3. The committed ``src/cruxible_core/kit_distribution/manifest.json`` is
   byte-identical to a fresh ``scripts/build_kit_bundles.py`` run over the
   committed tree (bundle tarballs are byte-reproducible, so the manifest's
   tarball/dir digests pin the release assets exactly). Any same-version
   tarball already sitting in ``dist/kits/`` is also checked against the
   fresh build.

Exits nonzero with regeneration instructions when anything drifted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.workflow.compiler import (
    LOCK_FILE_NAME,
    build_kit_root_lock,
    compute_lock_digest,
    load_lock,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_KITS_ROOT = _REPO_ROOT / "kits"
_DEFAULT_MANIFEST_PATH = _REPO_ROOT / "src" / "cruxible_core" / "kit_distribution" / "manifest.json"
_DEFAULT_DIST_DIR = _REPO_ROOT / "dist" / "kits"

_LOCK_HINT = "regenerate with: uv run cruxible lock --kit-dir {kit_dir}"
_BUNDLE_HINT = "regenerate with: uv run python scripts/build_kit_bundles.py"


def _load_bundle_builder() -> ModuleType:
    """Import scripts/build_kit_bundles.py — the one real bundle/manifest pipeline."""
    path = Path(__file__).resolve().parent / "build_kit_bundles.py"
    spec = importlib.util.spec_from_file_location("build_kit_bundles", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_kit_locks(kits_root: Path) -> list[str]:
    """Return one failure line per kit whose lock is missing, edited, or stale."""
    builder = _load_bundle_builder()
    failures: list[str] = []
    for kit_dir in builder.iter_kit_dirs(kits_root):
        label = f"{kits_root.name}/{kit_dir.name}"
        hint = _LOCK_HINT.format(kit_dir=f"{kits_root.name}/{kit_dir.name}")
        lock_path = kit_dir / LOCK_FILE_NAME
        if not lock_path.exists():
            failures.append(f"{label}: missing {LOCK_FILE_NAME}; {hint}")
            continue
        committed = load_lock(lock_path)
        committed_digest = compute_lock_digest(committed)
        if committed.lock_digest != committed_digest:
            failures.append(
                f"{label}: {LOCK_FILE_NAME} lock_digest does not match its own content "
                f"(hand-edited?); {hint}"
            )
            continue
        try:
            fresh = build_kit_root_lock(kit_dir)
        except ConfigError as exc:
            failures.append(f"{label}: lock regeneration failed: {exc}")
            continue
        if fresh.lock_digest != committed_digest:
            failures.append(
                f"{label}: {LOCK_FILE_NAME} is stale (committed {committed_digest}, "
                f"fresh regen {fresh.lock_digest}); {hint}"
            )
    return failures


def check_distribution_manifest(kits_root: Path, manifest_path: Path, dist_dir: Path) -> list[str]:
    """Rebuild every bundle into a temp dir; fail on manifest or dist-tarball drift."""
    builder = _load_bundle_builder()
    if not manifest_path.exists():
        return [f"packaged manifest missing: {manifest_path}; {_BUNDLE_HINT}"]

    with tempfile.TemporaryDirectory(prefix="cruxible-kit-check-") as tmp:
        bundles = [
            builder.build_kit_bundle(kit_dir, Path(tmp), __version__)
            for kit_dir in builder.iter_kit_dirs(kits_root)
        ]

    failures: list[str] = []
    fresh_manifest = builder.build_manifest(bundles, __version__)
    fresh_text = json.dumps(fresh_manifest, indent=2, sort_keys=True) + "\n"
    committed_text = manifest_path.read_text(encoding="utf-8")
    if fresh_text != committed_text:
        committed_manifest = json.loads(committed_text)
        details: list[str] = []
        if committed_manifest.get("version") != fresh_manifest["version"]:
            details.append(
                f"version {committed_manifest.get('version')!r} != package {__version__!r}"
            )
        committed_kits = committed_manifest.get("kits", {})
        fresh_kits = fresh_manifest["kits"]
        assert isinstance(fresh_kits, dict)
        for kit_id in sorted(set(committed_kits) | set(fresh_kits)):
            if committed_kits.get(kit_id) != fresh_kits.get(kit_id):
                details.append(f"kit entry drifted: {kit_id}")
        shown = (
            manifest_path.relative_to(_REPO_ROOT)
            if manifest_path.is_relative_to(_REPO_ROOT)
            else manifest_path
        )
        failures.append(
            f"{shown} is stale ({'; '.join(details) or 'formatting drift'}); {_BUNDLE_HINT}"
        )

    for bundle in bundles:
        existing = dist_dir / bundle.asset
        if existing.exists():
            existing_sha = hashlib.sha256(existing.read_bytes()).hexdigest()
            if existing_sha != bundle.tarball_sha256:
                failures.append(
                    f"dist tarball is stale: {existing} (sha256 {existing_sha}, "
                    f"fresh build {bundle.tarball_sha256}); {_BUNDLE_HINT}"
                )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kits-root", type=Path, default=_DEFAULT_KITS_ROOT)
    parser.add_argument("--manifest-path", type=Path, default=_DEFAULT_MANIFEST_PATH)
    parser.add_argument("--dist-dir", type=Path, default=_DEFAULT_DIST_DIR)
    args = parser.parse_args(argv)

    failures = check_kit_locks(args.kits_root)
    failures += check_distribution_manifest(args.kits_root, args.manifest_path, args.dist_dir)
    if failures:
        for failure in failures:
            print(f"STALE: {failure}", file=sys.stderr)
        print(
            "\nKit artifacts drifted from the committed tree. To fix:\n"
            "  1. For each kit named above: uv run cruxible lock --kit-dir kits/<kit>\n"
            "  2. uv run python scripts/build_kit_bundles.py\n"
            "  3. Commit the updated kits/<kit>/cruxible.lock.yaml and "
            "src/cruxible_core/kit_distribution/manifest.json\n"
            "Then rerun: uv run python scripts/check_kit_lockfiles.py",
            file=sys.stderr,
        )
        return 1
    print(f"kit lockfiles + distribution manifest current ({__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
