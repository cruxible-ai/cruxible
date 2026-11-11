"""The console's built files as the package carries them, and their manifest.

``scripts/build_console.py`` builds a console checkout and copies its ``dist/``
into ``cruxible_core/console_dist/`` with a manifest naming the console commit,
the pin it was built against and every file's SHA-256. The directory is not in
Git; a release wheel carries it when the release job built it first.

The listener serves exactly the files the manifest lists, after checking their
hashes once at start, so a served path is a table lookup: nothing outside the
table (no parent directory, no symlink target, no stray file) can be served.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DIST_DIRNAME = "console_dist"
MANIFEST_NAME = "cruxible-console-manifest.json"
MANIFEST_TAG = "cruxible-console-bundle-v1"

_SAFE_RELATIVE = re.compile(r"^(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9._@+\-][A-Za-z0-9._@+\-/]*$")


#: What every surface says when the install carries no console build (``{root}``
#: is where it looked).
NO_BUNDLE_MESSAGE = (
    "This install has no built console (nothing in {root}). Release wheels include "
    "it. From a source checkout, build it into the package with `python "
    "scripts/build_console.py --console PATH --commit SHA`, or run the console's dev "
    "server instead (`pnpm dev` in a cruxible-app checkout, as its README says)."
)


class ConsoleBundleMissing(Exception):
    """No built console is installed where the listener looks."""


class ConsoleBundleInvalid(Exception):
    """A console build is present but its manifest or files do not check out."""


@dataclass(frozen=True)
class ConsoleBundle:
    """One console build: the files to serve, keyed by served path, and its manifest."""

    root: Path
    #: Served path without the leading slash (``index.html``, ``assets/x.js``) -> file.
    files: Mapping[str, Path]
    manifest: Mapping[str, Any]


def default_dist_dir() -> Path:
    """Where the installed package keeps the console build (``cruxible_core/console_dist``)."""

    return Path(__file__).resolve().parent.parent / DIST_DIRNAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def bundle_files(root: Path) -> dict[str, Path]:
    """Every regular, non-hidden file under ``root`` (symlinks skipped), by relative path."""

    found: dict[str, Path] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith(".") or name == MANIFEST_NAME:
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            found[path.relative_to(root).as_posix()] = path
    return found


def write_manifest(root: Path, **identity: Any) -> dict[str, Any]:
    """Write the manifest for the build under ``root``: ``identity`` plus every file's hash."""

    files = bundle_files(root)
    if "index.html" not in files:
        raise ConsoleBundleInvalid(f"{root} has no index.html; is it a console build?")
    manifest: dict[str, Any] = {
        "tag": MANIFEST_TAG,
        **identity,
        "files": {name: _sha256(path) for name, path in files.items()},
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_bundle(root: Path | None = None) -> ConsoleBundle:
    """The console build under ``root`` (default: the package's), checked against its manifest."""

    root = (root or default_dist_dir()).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ConsoleBundleMissing(str(root))
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsoleBundleInvalid(f"{manifest_path} is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("tag") != MANIFEST_TAG:
        raise ConsoleBundleInvalid(f"{manifest_path} is not a {MANIFEST_TAG} manifest")
    listed = manifest.get("files")
    if not isinstance(listed, dict) or "index.html" not in listed:
        raise ConsoleBundleInvalid(f"{manifest_path} lists no index.html")
    files: dict[str, Path] = {}
    for name, digest in listed.items():
        if not isinstance(name, str) or not _SAFE_RELATIVE.match(name) or "//" in name:
            raise ConsoleBundleInvalid(f"{manifest_path} lists an unsafe path {name!r}")
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ConsoleBundleInvalid(f"{name} is listed in {manifest_path} but not present")
        if _sha256(path) != digest:
            raise ConsoleBundleInvalid(
                f"{name} does not match its hash in {manifest_path}; rebuild the console bundle"
            )
        files[name] = path
    return ConsoleBundle(root=root, files=files, manifest=manifest)
