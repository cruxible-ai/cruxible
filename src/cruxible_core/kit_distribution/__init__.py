"""Fetch-by-digest resolution for published kit bundles.

Installed distributions carry ``manifest.json`` next to this module (generated
by ``scripts/build_kit_bundles.py`` and committed before tagging): a map of kit
ids to release tarball assets, each pinned by a tarball sha256 and by the
extracted directory digest (``compute_path_sha256`` — the same digest
discipline kit locks use). ``resolve_published_kit`` downloads the asset from
the release ``base_url``, verifies the tarball sha256 before extraction,
safe-extracts to a temp dir, verifies the directory digest, and atomically
installs the kit into the local kit cache keyed by that digest. A cache hit
skips the network entirely.

Alias resolution order (see ``cruxible_core.kits.resolve_kit_ref``): local
source-checkout ``kits/`` directories always win; published bundles from this
module are the fetch fallback for installed distributions; shipped ``oci://``
refs cover kits absent from the packaged manifest.

Set ``CRUXIBLE_KIT_MANIFEST_URL_BASE`` to override ``base_url`` — used by the
test suite and the pre-publish smoke run against a local file server. Asset
names and digests still come from the packaged manifest; the packaged
``base_url`` itself must be https, only the explicit override may be http.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import zlib
from importlib import resources
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from cruxible_core.errors import ConfigError
from cruxible_core.kits import _file_lock, _kit_cache_dir
from cruxible_core.workflow.compiler import compute_path_sha256

MANIFEST_URL_BASE_ENV = "CRUXIBLE_KIT_MANIFEST_URL_BASE"

_MANIFEST_RESOURCE = "manifest.json"
_PUBLISHED_CACHE_DIR = "published"
_MAX_BUNDLE_BYTES = 200 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60.0


class PublishedKitEntry(BaseModel):
    """One release-asset entry in the packaged kit distribution manifest."""

    asset: str
    tarball_sha256: str
    dir_digest: str


class PublishedKitManifest(BaseModel):
    """Packaged manifest pinning every published kit bundle for this version."""

    version: str
    base_url: str
    kits: dict[str, PublishedKitEntry]


def load_published_manifest() -> PublishedKitManifest | None:
    """Load the packaged kit distribution manifest, or None when not shipped."""
    resource = resources.files(__package__).joinpath(_MANIFEST_RESOURCE)
    if not resource.is_file():
        return None
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
        return PublishedKitManifest.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        raise ConfigError(f"Packaged kit distribution manifest is invalid: {exc}") from exc


def published_kit_ids() -> frozenset[str]:
    """Return the kit ids resolvable from the packaged manifest."""
    manifest = load_published_manifest()
    return frozenset(manifest.kits) if manifest is not None else frozenset()


def resolve_published_kit(kit_id: str) -> Path:
    """Resolve a published kit id to a verified, digest-keyed cache directory."""
    manifest = load_published_manifest()
    if manifest is None or kit_id not in manifest.kits:
        raise ConfigError(
            f"Kit '{kit_id}' is not in the packaged kit distribution manifest. "
            + _clone_hint(kit_id)
        )
    entry = manifest.kits[kit_id]
    if not entry.dir_digest.startswith("sha256:"):
        raise ConfigError(
            f"Published kit '{kit_id}' has a malformed dir_digest in the packaged "
            f"manifest: {entry.dir_digest!r}"
        )
    digest_key = entry.dir_digest.removeprefix("sha256:")
    cache_root = _kit_cache_dir() / _PUBLISHED_CACHE_DIR
    target = cache_root / digest_key
    # The digest key is publicly predictable (it ships in the wheel manifest),
    # so a cache hit is never trusted: re-verify the content digest before
    # returning, and self-heal a poisoned/corrupted entry by re-downloading.
    if target.is_dir() and compute_path_sha256(target) == entry.dir_digest:
        return target

    url = _bundle_url(manifest, entry, kit_id=kit_id)
    cache_root.mkdir(parents=True, exist_ok=True)
    with _file_lock(cache_root / f"{digest_key}.lock"):
        if target.is_dir():
            if compute_path_sha256(target) == entry.dir_digest:
                return target
            shutil.rmtree(target)
        data = _download_bundle(kit_id, url)
        actual_tarball = hashlib.sha256(data).hexdigest()
        if actual_tarball != entry.tarball_sha256:
            raise ConfigError(
                f"Published kit '{kit_id}' tarball digest mismatch for {url}: expected "
                f"sha256:{entry.tarball_sha256}, got sha256:{actual_tarball}. Refusing to "
                "extract. " + _clone_hint(kit_id)
            )
        temp_dir = Path(tempfile.mkdtemp(prefix=f"{digest_key}.", dir=cache_root))
        try:
            _safe_extract(data, temp_dir, kit_id=kit_id, url=url)
            actual_dir = compute_path_sha256(temp_dir)
            if actual_dir != entry.dir_digest:
                raise ConfigError(
                    f"Published kit '{kit_id}' directory digest mismatch after extracting "
                    f"{url}: expected {entry.dir_digest}, got {actual_dir}. Refusing to "
                    "install. " + _clone_hint(kit_id)
                )
            os.replace(temp_dir, target)
        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
    return target


def _bundle_url(manifest: PublishedKitManifest, entry: PublishedKitEntry, *, kit_id: str) -> str:
    override = os.environ.get(MANIFEST_URL_BASE_ENV)
    base_url = override or manifest.base_url
    scheme = urlparse(base_url).scheme
    allowed = {"https", "http"} if override else {"https"}
    if scheme not in allowed:
        raise ConfigError(
            f"Published kit '{kit_id}' base URL must be https, got {base_url!r}"
            + ("" if override else f" (set {MANIFEST_URL_BASE_ENV} to override)")
        )
    if not base_url.endswith("/"):
        base_url += "/"
    return base_url + entry.asset


def _download_bundle(kit_id: str, url: str) -> bytes:
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > _MAX_BUNDLE_BYTES:
                raise ConfigError(
                    f"Published kit '{kit_id}' bundle at {url} declares {declared} bytes, "
                    f"over the {_MAX_BUNDLE_BYTES}-byte cap. " + _clone_hint(kit_id)
                )
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > _MAX_BUNDLE_BYTES:
                    raise ConfigError(
                        f"Published kit '{kit_id}' bundle at {url} exceeds the "
                        f"{_MAX_BUNDLE_BYTES}-byte cap. " + _clone_hint(kit_id)
                    )
    except httpx.HTTPError as exc:
        raise ConfigError(
            f"Could not download published kit '{kit_id}' from {url}: {exc}. "
            "Check network access. " + _clone_hint(kit_id)
        ) from exc
    return bytes(data)


def _safe_extract(data: bytes, dest: Path, *, kit_id: str, url: str) -> None:
    inflated = _inflate_with_cap(data, kit_id=kit_id, url=url)
    try:
        with tarfile.open(fileobj=io.BytesIO(inflated), mode="r:") as tar:
            members = tar.getmembers()
            total_size = 0
            for member in members:
                _validate_member(member, kit_id=kit_id, url=url)
                total_size += member.size
            if total_size > _MAX_EXTRACTED_BYTES:
                # Sparse members can claim logical sizes beyond the archive
                # bytes already bounded by _inflate_with_cap.
                raise ConfigError(
                    f"Published kit '{kit_id}' bundle at {url} would extract {total_size} "
                    f"bytes, over the {_MAX_EXTRACTED_BYTES}-byte cap. " + _clone_hint(kit_id)
                )
            if hasattr(tarfile, "data_filter"):
                tar.extractall(dest, members=members, filter="data")
            else:
                # Pre-PEP-706 interpreter (< 3.11.4): the manual member
                # validation above is the extraction guard.
                tar.extractall(dest, members=members)  # noqa: S202
    except tarfile.TarError as exc:
        raise ConfigError(
            f"Published kit '{kit_id}' bundle at {url} is not a valid tar.gz: {exc}. "
            + _clone_hint(kit_id)
        ) from exc
    _normalize_extracted_modes(dest)


def _inflate_with_cap(data: bytes, *, kit_id: str, url: str) -> bytes:
    """Gunzip the bundle, bounding decompressed bytes before any tar parsing."""
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        inflated = decompressor.decompress(data, _MAX_EXTRACTED_BYTES + 1)
    except zlib.error as exc:
        raise ConfigError(
            f"Published kit '{kit_id}' bundle at {url} is not a valid tar.gz: {exc}. "
            + _clone_hint(kit_id)
        ) from exc
    if len(inflated) > _MAX_EXTRACTED_BYTES:
        raise ConfigError(
            f"Published kit '{kit_id}' bundle at {url} inflates over the "
            f"{_MAX_EXTRACTED_BYTES}-byte cap. " + _clone_hint(kit_id)
        )
    return inflated


def _normalize_extracted_modes(dest: Path) -> None:
    """Clamp extracted modes on every interpreter: no setuid/setgid/sticky bits.

    The directory digest is mode-blind and pre-PEP-706 interpreters extract
    without the data filter, so tar-carried mode bits must never survive.
    """
    for path in dest.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def _validate_member(member: tarfile.TarInfo, *, kit_id: str, url: str) -> None:
    if not (member.isreg() or member.isdir()):
        raise ConfigError(
            f"Published kit '{kit_id}' bundle at {url} contains a non-file member "
            f"{member.name!r} (links and special files are refused). " + _clone_hint(kit_id)
        )
    path = PurePosixPath(member.name)
    if not member.name or "\\" in member.name or path.is_absolute() or ".." in path.parts:
        # Backslashes are rejected outright: they are real separators on
        # Windows, where PurePosixPath would miss ``..\\..`` traversal.
        raise ConfigError(
            f"Published kit '{kit_id}' bundle at {url} contains an unsafe member path "
            f"{member.name!r}. " + _clone_hint(kit_id)
        )


def _clone_hint(kit_id: str) -> str:
    return (
        "As a fallback, clone https://github.com/cruxible-ai/cruxible and use "
        f"--kit file://<clone>/kits/{kit_id}"
    )
