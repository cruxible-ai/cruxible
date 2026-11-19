"""Build the console from a checkout and install it as this package's console bundle.

    python scripts/build_console.py --console ~/src/cruxible-app --commit <sha>

Checks that the console checkout is clean and at ``--commit``, that its
``api/pin.json`` was generated against this core's served surface (same core
version and SDK contract digest as ``GET /version`` answers here), runs its
production build (``pnpm install --frozen-lockfile`` then ``pnpm run build``) and
copies ``dist/`` into ``src/cruxible_core/console_dist/`` with a manifest naming
the console commit, the pin and every file's SHA-256. ``cruxible console``
serves exactly the files that manifest lists. The directory is ignored by Git;
the wheel and sdist carry it when it exists at build time.

``--allow-pin-mismatch`` builds anyway and records ``pin_matches_core: false``
in the manifest (for trying a console against a core it was not pinned to;
never for a release).
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "packages/cruxible-client/src", REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cruxible_core.console.bundle import (  # noqa: E402
    MANIFEST_NAME,
    bundle_files,
    default_dist_dir,
    write_manifest,
)

#: Vite reads these into a production build; a token in one would ship in the bundle.
_ENV_FILES = (".env", ".env.local", ".env.production", ".env.production.local")


class BuildRefused(Exception):
    """The checkout cannot be built into a bundle; the message says why."""


def core_pin() -> dict[str, str]:
    """This core's served-surface pin: what ``GET /version`` answers (less the boot id)."""

    from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    from cruxible_core import __version__

    return {
        "core_version": __version__,
        "sdk_contract_snapshot_digest": AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    }


def _git(console: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(console), *args],
        capture_output=True,
        text=True,
        check=False,
        # Read-only: never refresh the checkout's index.
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if completed.returncode != 0:
        raise BuildRefused(f"git {' '.join(args)} failed in {console}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def check_checkout(console: Path, commit: str, *, allow_pin_mismatch: bool) -> dict[str, Any]:
    """The identity the manifest records, once the checkout passes every check."""

    head = _git(console, "rev-parse", "HEAD")
    wanted = _git(console, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if head != wanted:
        raise BuildRefused(f"{console} is at {head}, not the expected commit {wanted}")
    dirty = _git(console, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise BuildRefused(f"{console} has uncommitted changes; build a clean commit")
    present = [name for name in _ENV_FILES if (console / name).exists()]
    if present:
        raise BuildRefused(
            f"{console} has {', '.join(present)}; Vite would build its VITE_ values into "
            "the bundle. Move them aside first."
        )
    try:
        pin = json.loads((console / "api" / "pin.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildRefused(f"{console}/api/pin.json is unreadable: {exc}") from exc
    ours = core_pin()
    matches = all(pin.get(key) == value for key, value in ours.items())
    if not matches and not allow_pin_mismatch:
        raise BuildRefused(
            "the console is pinned to cruxible "
            f"{pin.get('core_version')} ({pin.get('sdk_contract_snapshot_digest')}), this "
            f"core serves {ours['core_version']} ({ours['sdk_contract_snapshot_digest']}). "
            "Re-pin the console (its scripts/pin-openapi.sh) against this core."
        )
    return {
        "console_commit": head,
        "console_pin": pin,
        "pin_matches_core": matches,
        **ours,
    }


def run_build(console: Path, *, install: bool) -> Path:
    """Run the console's production build; return its ``dist/``."""

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("VITE_") and key != "CRUXIBLE_SERVER_BEARER_TOKEN"
    }
    steps = [["pnpm", "run", "build"]]
    if install:
        steps.insert(0, ["pnpm", "install", "--frozen-lockfile"])
    for step in steps:
        completed = subprocess.run(step, cwd=console, env=env, check=False)
        if completed.returncode != 0:
            raise BuildRefused(f"`{' '.join(step)}` failed in {console}")
    dist = console / "dist"
    if not (dist / "index.html").is_file():
        raise BuildRefused(f"the build left no {dist}/index.html")
    return dist


def install_bundle(dist: Path, out: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """Replace ``out`` with a copy of ``dist`` and its manifest; return the manifest."""

    if out.exists():
        if any(out.iterdir()) and not (out / MANIFEST_NAME).is_file():
            raise BuildRefused(f"{out} holds files but no {MANIFEST_NAME}; not replacing it")
        shutil.rmtree(out)
    shutil.copytree(dist, out)
    return write_manifest(out, **identity)


def bundle_sizes(out: Path) -> dict[str, int]:
    """Files, bytes and gzip-9 bytes of a bundle (the manifest included)."""

    files = [*bundle_files(out).values(), out / MANIFEST_NAME]
    raw = sum(path.stat().st_size for path in files)
    zipped = sum(len(gzip.compress(path.read_bytes(), 9)) for path in files)
    return {"files": len(files), "bytes": raw, "gzip_bytes": zipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--console", required=True, type=Path, help="console checkout")
    parser.add_argument("--commit", required=True, help="commit the checkout must be at")
    parser.add_argument("--out", type=Path, default=default_dist_dir(), help=argparse.SUPPRESS)
    parser.add_argument("--allow-pin-mismatch", action="store_true")
    parser.add_argument(
        "--skip-install", action="store_true", help="reuse the checkout's node_modules"
    )
    args = parser.parse_args(argv)
    console = args.console.expanduser().resolve()
    try:
        identity = check_checkout(console, args.commit, allow_pin_mismatch=args.allow_pin_mismatch)
        dist = run_build(console, install=not args.skip_install)
        install_bundle(dist, args.out, identity)
    except BuildRefused as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1
    sizes = bundle_sizes(args.out)
    print(
        f"installed console {identity['console_commit'][:12]} into {args.out}: "
        f"{sizes['files']} files, {sizes['bytes']} bytes ({sizes['gzip_bytes']} gzip)"
        + ("" if identity["pin_matches_core"] else "; PIN DOES NOT MATCH THIS CORE")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
