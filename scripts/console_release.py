"""The console a release ships: its pinned commit, and a check that the built archives carry it.

    python scripts/console_release.py pin           # repository=... and commit=... lines
    python scripts/console_release.py check DIST    # DIST holds the cruxible wheel and sdist

``scripts/console_release.json`` names the console repository and the exact
commit a release builds (``scripts/build_console.py --console CHECKOUT --commit
COMMIT``). ``pin`` validates it and prints it as ``key=value`` lines for
``$GITHUB_OUTPUT``. ``check`` opens the cruxible wheel and sdist in DIST and
refuses unless both carry the console manifest and every file it lists with
that file's hash, built from the pinned commit against this core
(``pin_matches_core`` true, the manifest's core version the wheel's version).
The publish workflow and CI run it after ``uv build``. Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import zipfile
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from cruxible_core.console.bundle import DIST_DIRNAME, MANIFEST_NAME, MANIFEST_TAG  # noqa: E402

PIN_PATH = REPO_ROOT / "scripts" / "console_release.json"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WHEEL_DIR = f"cruxible_core/{DIST_DIRNAME}"
SDIST_DIR = f"src/cruxible_core/{DIST_DIRNAME}"


class ReleaseRefused(Exception):
    """The pin or a built archive is not what a release ships; the message says why."""


def read_pin(path: Path = PIN_PATH) -> dict[str, str]:
    try:
        pin = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseRefused(f"{path} is unreadable: {exc}") from exc
    if not isinstance(pin, dict) or set(pin) != {"repository", "commit"}:
        raise ReleaseRefused(f"{path} must hold exactly repository and commit")
    if not isinstance(pin["repository"], str) or not _REPOSITORY.match(pin["repository"]):
        raise ReleaseRefused(f"{path}: repository must be owner/name")
    if not isinstance(pin["commit"], str) or not _COMMIT.match(pin["commit"]):
        raise ReleaseRefused(f"{path}: commit must be a full 40-character lowercase SHA")
    return {"repository": pin["repository"], "commit": pin["commit"]}


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _check_files(
    archive: str, manifest: dict[str, Any], read: Any, names: set[str], base: str
) -> None:
    listed = manifest.get("files")
    if not isinstance(listed, dict) or "index.html" not in listed:
        raise ReleaseRefused(f"{archive}: the console manifest lists no index.html")
    for name, digest in listed.items():
        member = f"{base}/{name}"
        if member not in names:
            raise ReleaseRefused(f"{archive}: {member} is listed in the manifest but missing")
        if _sha256(read(member)) != digest:
            raise ReleaseRefused(f"{archive}: {member} does not match its manifest hash")


def _check_manifest(
    archive: str, manifest: Any, *, version: str, commit: str | None
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("tag") != MANIFEST_TAG:
        raise ReleaseRefused(f"{archive}: the console manifest is not a {MANIFEST_TAG} manifest")
    if manifest.get("pin_matches_core") is not True:
        raise ReleaseRefused(
            f"{archive}: the console was built against another core (pin_matches_core is not "
            "true); re-pin the console and rebuild without --allow-pin-mismatch"
        )
    if manifest.get("core_version") != version:
        raise ReleaseRefused(
            f"{archive}: the console was built for cruxible {manifest.get('core_version')}, "
            f"the archive is {version}"
        )
    if commit is not None and manifest.get("console_commit") != commit:
        raise ReleaseRefused(
            f"{archive}: the console was built from {manifest.get('console_commit')}, "
            f"the release pins {commit}"
        )
    return manifest


def check_wheel(wheel: Path, *, commit: str | None) -> dict[str, Any]:
    """The wheel's console manifest, once the wheel carries every file it lists."""

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        [metadata] = [n for n in names if n.endswith(".dist-info/METADATA")]
        version = HeaderParser().parsestr(archive.read(metadata).decode())["Version"]
        member = f"{WHEEL_DIR}/{MANIFEST_NAME}"
        if member not in names:
            raise ReleaseRefused(f"{wheel.name}: no {member}; the wheel carries no console")
        manifest = _check_manifest(
            wheel.name, json.loads(archive.read(member)), version=version, commit=commit
        )
        _check_files(wheel.name, manifest, archive.read, names, WHEEL_DIR)
    return manifest


def check_sdist(sdist: Path, *, commit: str | None) -> dict[str, Any]:
    """The sdist's console manifest, once it carries every file listed (and no checkout)."""

    with tarfile.open(sdist) as archive:
        members = {m.name.split("/", 1)[1]: m for m in archive.getmembers() if "/" in m.name}
        stray = sorted(n for n in members if "node_modules/" in n or n.startswith("cruxible-app/"))
        if stray:
            raise ReleaseRefused(f"{sdist.name}: carries a console checkout ({stray[0]})")
        pkg_info = archive.extractfile(members["PKG-INFO"])
        assert pkg_info is not None
        version = HeaderParser().parsestr(pkg_info.read().decode())["Version"]

        def read(name: str) -> bytes:
            handle = archive.extractfile(members[name])
            assert handle is not None
            return handle.read()

        member = f"{SDIST_DIR}/{MANIFEST_NAME}"
        if member not in members:
            raise ReleaseRefused(f"{sdist.name}: no {member}; the sdist carries no console")
        manifest = _check_manifest(
            sdist.name, json.loads(read(member)), version=version, commit=commit
        )
        _check_files(sdist.name, manifest, read, set(members), SDIST_DIR)
    return manifest


def check_dist(dist: Path, *, commit: str | None) -> dict[str, Any]:
    """Check the one cruxible wheel and sdist in ``dist``; return the wheel's manifest."""

    wheels = sorted(dist.glob("cruxible-*.whl"))
    sdists = sorted(dist.glob("cruxible-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseRefused(
            f"{dist} must hold one cruxible wheel and one sdist, found "
            f"{[p.name for p in wheels + sdists]}"
        )
    manifest = check_wheel(wheels[0], commit=commit)
    check_sdist(sdists[0], commit=commit)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pin", help="print the pinned repository and commit")
    check = commands.add_parser("check", help="check the built wheel and sdist carry the console")
    check.add_argument("dist", type=Path)
    check.add_argument("--commit", help="expected console commit (default: the pin's)")
    args = parser.parse_args(argv)
    try:
        if args.command == "pin":
            pin = read_pin()
            print(f"repository={pin['repository']}\ncommit={pin['commit']}")
            return 0
        commit = args.commit or read_pin()["commit"]
        manifest = check_dist(args.dist, commit=commit)
    except ReleaseRefused as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    files = manifest["files"]
    print(
        f"console {manifest['console_commit'][:12]} in the wheel and sdist: {len(files)} files, "
        f"pinned to cruxible {manifest['core_version']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
