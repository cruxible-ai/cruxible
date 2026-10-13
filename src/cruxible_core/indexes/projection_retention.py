"""Accepted projection pieces past retention are evicted; an evicted one is rebuilt on a miss.

A piece is a full copy of one generation's projection, so pieces are kept only
for the coordinates reads keep asking for: the serving head, the last
``RetentionSettings.generations`` accepted generations, the bases of open
proposals, and any coordinate a handle in this process holds. Every other
accepted coordinate is rebuilt from the ledger tree and the body store the
next time it is bound. No signed record cites a piece; logical digests are
recomputed from accepted sources.

Eviction deletes the manifest before its piece, so a reader never finds a
manifest naming a missing piece. It runs under the directory's retention lock,
which a rebuild on a miss also takes, so the two never interleave.
"""

from __future__ import annotations

import fcntl
import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.sqlite import load_projection_manifest

RETENTION_LOCK = ".projection-retention.lock"
_MANIFEST_NAME = re.compile(r"^projection-[0-9a-f]{64}\.json$")

_HOLDS: Counter[tuple[str, str]] = Counter()
_HOLDS_LOCK = threading.Lock()
# (directory, manifest name) -> (git oid, piece names). Manifests are immutable
# and named by their coordinate, so each is parsed once per process.
_MANIFESTS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
_MANIFESTS_LOCK = threading.Lock()


@contextmanager
def retention_lock(directory: Path) -> Iterator[None]:
    """Exclude eviction and rebuild-on-miss in one publication directory, across processes."""

    descriptor = os.open(
        directory / RETENTION_LOCK,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _key(directory: Path) -> str:
    return os.path.realpath(directory)


def hold_projection(directory: Path, manifest_name: str) -> None:
    """Keep one manifest and its piece from eviction while a handle reads them."""

    key = (_key(directory), manifest_name)
    with _HOLDS_LOCK:
        _HOLDS[key] += 1


def release_projection(directory: Path, manifest_name: str) -> None:
    key = (_key(directory), manifest_name)
    with _HOLDS_LOCK:
        _HOLDS[key] -= 1
        if _HOLDS[key] <= 0:
            del _HOLDS[key]


def held_projections(directory: Path) -> frozenset[str]:
    key = _key(directory)
    with _HOLDS_LOCK:
        return frozenset(name for held, name in _HOLDS if held == key)


def _manifest(directory: Path, name: str) -> tuple[str, tuple[str, ...]] | None:
    key = (str(directory), name)
    with _MANIFESTS_LOCK:
        known = _MANIFESTS.get(key)
    if known is not None:
        return known
    try:
        manifest = load_projection_manifest(directory / name)
    except (OSError, ProjectionIntegrityError):
        # Malformed or older-schema manifests are recovery's to sweep at open.
        return None
    found = (manifest.git_oid, tuple(piece.name for piece in manifest.pieces))
    with _MANIFESTS_LOCK:
        _MANIFESTS[key] = found
    return found


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def evict_projections(directory: Path, *, evictable: Callable[[str], bool]) -> tuple[str, ...]:
    """Delete every projection whose git oid ``evictable`` names and no handle holds.

    ``evictable`` answers only for accepted coordinates outside retention: a
    build for a generation not (yet) on main -- a prebuilt activation or
    upgrade in flight, a lost race -- is never evictable here; recovery
    retires those. Returns the git oids evicted. The directory listing is
    bounded by what retention keeps, and each manifest is parsed once per
    process.
    """

    evicted: list[str] = []
    with retention_lock(directory):
        held = held_projections(directory)
        for name in sorted(os.listdir(directory)):
            if not _MANIFEST_NAME.fullmatch(name):
                continue
            found = _manifest(directory, name)
            if found is None or name in held:
                continue
            oid, pieces = found
            if not evictable(oid):
                continue
            # The manifest first: a manifest naming a missing piece refuses binds
            # and recovery, while a piece no manifest names is swept as an orphan.
            (directory / name).unlink(missing_ok=True)
            _fsync_directory(directory)
            for piece in pieces:
                (directory / piece).unlink(missing_ok=True)
            _fsync_directory(directory)
            with _MANIFESTS_LOCK:
                _MANIFESTS.pop((str(directory), name), None)
            evicted.append(oid)
    return tuple(evicted)


__all__ = [
    "RETENTION_LOCK",
    "evict_projections",
    "hold_projection",
    "held_projections",
    "release_projection",
    "retention_lock",
]
