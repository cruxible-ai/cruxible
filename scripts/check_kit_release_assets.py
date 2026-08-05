"""Verify that tagged kit bundle URLs in the packaged manifest return HTTP 200.

Usage:
    uv run python scripts/check_kit_release_assets.py

The packaged manifest is stamped and committed before its release tag exists.
That pre-tag state is legitimate, so a missing exact ``v<manifest-version>``
tag on the configured git remote emits a notice and exits successfully. Once
the tag exists, every manifest asset is required to answer an HTTP HEAD request
with status 200. Remote lookup failures are errors, not evidence that the tag
is absent.

Tag existence and asset availability are NOT simultaneous. Pushing main and its
release tag together starts this job and the tag-triggered publish workflow at
the same moment: the tag is visible to ``git ls-remote`` immediately, while the
bundles only appear once publish's ``gh release upload`` finishes. A single HEAD
therefore races the upload (observed: a 404 four seconds ahead of it). Each URL
is retried with linear backoff so the check waits out that window instead of
failing the push, and only a URL still unavailable after the whole budget is a
real failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MANIFEST_PATH = _REPO_ROOT / "src" / "cruxible_core" / "kit_distribution" / "manifest.json"
# 5 attempts with a 10s base delay sleeps 10+20+30+40 = 100s of backoff, so an
# asset has roughly two minutes to appear before the check calls it missing.
_DEFAULT_ATTEMPTS = 5
_DEFAULT_RETRY_DELAY = 10.0


def remote_tag_exists(remote: str, tag: str, timeout: float) -> bool:
    """Return whether *remote* has the exact tag, distinguishing lookup errors."""
    try:
        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "--tags",
                remote,
                f"refs/tags/{tag}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not inspect remote {remote!r}: {exc}") from exc

    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
    raise RuntimeError(
        f"git ls-remote failed for {remote!r} with exit {result.returncode}: {detail}"
    )


def _load_manifest(path: Path) -> tuple[str, list[tuple[str, str]]]:
    try:
        manifest: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read manifest {path}: {exc}") from exc

    if not isinstance(manifest, dict):
        raise ValueError(f"manifest {path} must contain a JSON object")
    version = manifest.get("version")
    base_url = manifest.get("base_url")
    kits = manifest.get("kits")
    if not isinstance(version, str) or not version:
        raise ValueError(f"manifest {path} has no non-empty string version")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(f"manifest {path} has no non-empty string base_url")
    if not isinstance(kits, dict) or not kits:
        raise ValueError(f"manifest {path} has no kit entries")

    assets: list[tuple[str, str]] = []
    normalized_base_url = f"{base_url.rstrip('/')}/"
    for kit_id, entry in sorted(kits.items()):
        if not isinstance(kit_id, str) or not isinstance(entry, dict):
            raise ValueError(f"manifest {path} contains an invalid kit entry")
        asset = entry.get("asset")
        if not isinstance(asset, str) or not asset:
            raise ValueError(f"manifest {path} kit {kit_id!r} has no non-empty asset name")
        assets.append((kit_id, urllib.parse.urljoin(normalized_base_url, asset)))
    return version, assets


def _head_status(url: str, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "cruxible-kit-release-asset-check"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status


def check_asset_urls(
    assets: list[tuple[str, str]],
    timeout: float,
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    retry_delay: float = _DEFAULT_RETRY_DELAY,
) -> list[str]:
    """Return one failure per URL that does not resolve to HTTP 200.

    Each URL gets up to *attempts* tries with linear backoff between them, so a
    bundle that is still uploading is waited for rather than reported missing.
    Only the last attempt's outcome is reported, and no sleep follows it.
    """
    failures: list[str] = []
    for kit_id, url in assets:
        failure: str | None = None
        for attempt in range(1, attempts + 1):
            try:
                status = _head_status(url, timeout)
            except urllib.error.HTTPError as exc:
                status = exc.code
                failure = f"{kit_id}: HEAD {url} returned HTTP {status}, expected 200"
            except (OSError, urllib.error.URLError) as exc:
                status = None
                failure = f"{kit_id}: HEAD {url} failed: {exc}"
            else:
                if status == 200:
                    failure = None
                    print(f"ok {kit_id} <- {url} (HTTP 200)")
                    break
                failure = f"{kit_id}: HEAD {url} returned HTTP {status}, expected 200"

            if attempt < attempts:
                delay = retry_delay * attempt
                print(
                    f"retry {kit_id} in {delay:g}s "
                    f"(attempt {attempt}/{attempts} unavailable): {failure}"
                )
                time.sleep(delay)
        if failure is not None:
            failures.append(f"{failure} after {attempts} attempt(s)")
    return failures


def _notice(message: str) -> None:
    print(f"NOTICE: {message}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Kit release assets::{message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest-path", type=Path, default=_DEFAULT_MANIFEST_PATH)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--attempts",
        type=int,
        default=_DEFAULT_ATTEMPTS,
        help="HEAD attempts per asset before it counts as unavailable.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=_DEFAULT_RETRY_DELAY,
        help="Base backoff seconds; attempt N waits N * this before retrying.",
    )
    args = parser.parse_args(argv)

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.attempts < 1:
        parser.error("--attempts must be at least one")
    if args.retry_delay < 0:
        parser.error("--retry-delay must not be negative")

    try:
        version, assets = _load_manifest(args.manifest_path)
        tag = f"v{version}"
        if not remote_tag_exists(args.remote, tag, args.timeout):
            _notice(
                f"{tag} does not exist on remote {args.remote!r}; skipping release asset URL checks"
            )
            return 0
        failures = check_asset_urls(
            assets,
            args.timeout,
            attempts=args.attempts,
            retry_delay=args.retry_delay,
        )
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print(f"all kit release asset URLs are available for {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
