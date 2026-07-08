"""Build deterministic kit bundles and the packaged kit distribution manifest.

Usage:
    uv run python scripts/build_kit_bundles.py

For each ``kits/<id>/`` with a ``cruxible-kit.yaml``, writes a byte-reproducible
``dist/kits/<id>-<version>.tar.gz`` (version = the core package version) and
regenerates ``src/cruxible_core/kit_distribution/manifest.json``, which ships in
the wheel and pins every bundle by tarball sha256 and extracted directory
digest. Tarballs are deterministic: bundle-filtered files only (same junk
filter as the kit cache), sorted member order, mtime=0, uid/gid=0, no
user/group names, mode 0644, gzip mtime pinned to 0 — building twice yields
byte-identical archives. Rerun after any kit change; the printed summary names
what changed. Upload ``dist/kits/*.tar.gz`` as release assets on the
``v<version>`` tag and commit the regenerated manifest before tagging.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from cruxible_core import __version__
from cruxible_core.errors import ConfigError
from cruxible_core.kits import _iter_bundle_files, load_kit_manifest

GITHUB_BASE_URL_TEMPLATE = "https://github.com/cruxible-ai/cruxible/releases/download/v{version}/"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT_DIR = _REPO_ROOT / "dist" / "kits"
_DEFAULT_MANIFEST_PATH = _REPO_ROOT / "src" / "cruxible_core" / "kit_distribution" / "manifest.json"


@dataclass(frozen=True)
class BuiltBundle:
    """One deterministic kit tarball with its pinned digests."""

    kit_id: str
    asset: str
    path: Path
    tarball_sha256: str
    dir_digest: str


def iter_kit_dirs(kits_root: Path) -> list[Path]:
    """Return every kit directory carrying a cruxible-kit.yaml, sorted by id."""
    return sorted(path.parent for path in kits_root.glob("*/cruxible-kit.yaml"))


def compute_kit_dir_digest(kit_dir: Path) -> str:
    """compute_path_sha256 discipline over the bundle-filtered kit file set.

    Identical to ``compute_path_sha256(kit_dir)`` on a clean tree; local junk
    (``__pycache__``, ``.DS_Store``, ...) is excluded exactly as the kit cache
    excludes it, so the digest always matches the extracted bundle.
    """
    digest = hashlib.sha256()
    for child in _iter_bundle_files(kit_dir):
        rel = child.relative_to(kit_dir).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(child.read_bytes()).hexdigest().encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def build_kit_bundle(kit_dir: Path, out_dir: Path, version: str) -> BuiltBundle:
    """Write one deterministic <kit_id>-<version>.tar.gz and return its digests."""
    manifest = load_kit_manifest(kit_dir)
    if manifest.kit_id != kit_dir.name:
        raise ConfigError(
            f"Kit directory {kit_dir} declares kit_id '{manifest.kit_id}', not '{kit_dir.name}'"
        )
    payload = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=payload, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for child in _iter_bundle_files(kit_dir):
            data = child.read_bytes()
            info = tarfile.TarInfo(name=child.relative_to(kit_dir).as_posix())
            info.size = len(data)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    archive = payload.getvalue()
    asset = f"{manifest.kit_id}-{version}.tar.gz"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / asset
    out_path.write_bytes(archive)
    return BuiltBundle(
        kit_id=manifest.kit_id,
        asset=asset,
        path=out_path,
        tarball_sha256=hashlib.sha256(archive).hexdigest(),
        dir_digest=compute_kit_dir_digest(kit_dir),
    )


def build_manifest(bundles: list[BuiltBundle], version: str) -> dict[str, object]:
    """Assemble the packaged kit distribution manifest payload."""
    return {
        "version": version,
        "base_url": GITHUB_BASE_URL_TEMPLATE.format(version=version),
        "kits": {
            bundle.kit_id: {
                "asset": bundle.asset,
                "tarball_sha256": bundle.tarball_sha256,
                "dir_digest": bundle.dir_digest,
            }
            for bundle in sorted(bundles, key=lambda bundle: bundle.kit_id)
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kits-root", type=Path, default=_REPO_ROOT / "kits")
    parser.add_argument("--out-dir", type=Path, default=_DEFAULT_OUT_DIR)
    parser.add_argument("--manifest-path", type=Path, default=_DEFAULT_MANIFEST_PATH)
    args = parser.parse_args()

    kit_dirs = iter_kit_dirs(args.kits_root)
    if not kit_dirs:
        raise ConfigError(f"No kit manifests found under {args.kits_root}")

    previous: dict[str, dict[str, str]] = {}
    if args.manifest_path.exists():
        previous = json.loads(args.manifest_path.read_text(encoding="utf-8")).get("kits", {})

    bundles = [build_kit_bundle(kit_dir, args.out_dir, __version__) for kit_dir in kit_dirs]
    for bundle in bundles:
        prior = previous.get(bundle.kit_id)
        if prior is None:
            status = "added"
        elif prior.get("dir_digest") != bundle.dir_digest:
            status = "updated"
        else:
            status = "unchanged"
        print(f"{status:>9}  {bundle.kit_id}  {bundle.dir_digest}  -> {bundle.path}")
    for stale_id in sorted(set(previous) - {bundle.kit_id for bundle in bundles}):
        print(f"  removed  {stale_id}")

    manifest = build_manifest(bundles, __version__)
    args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"manifest: {args.manifest_path} ({len(bundles)} kits, version {__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
