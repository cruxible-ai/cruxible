"""Accepted projection pieces past retention are evicted; an evicted one is rebuilt on a miss.

A piece is a full copy of one generation's projection, so pieces are kept only
for the coordinates reads keep asking for: the serving head, the last
``RetentionSettings.generations`` accepted generations (never fewer than the
head and its parent, which a read binds while the next serving pointer is being
published), and any coordinate a reader holds. Every other accepted coordinate
is rebuilt from the ledger tree and the body store the next time it is bound. No
signed record cites a piece; logical digests are recomputed from accepted
sources.

A reader holds a coordinate by holding a shared ``flock`` on its piece for the
life of its handle (``lock_piece_shared``), in any process. Eviction takes the
exclusive lock without waiting, so a held piece is skipped; while eviction
holds it, a reader that opened the piece waits, then finds its path gone and
binds again (``ProjectionEvicted``), which rebuilds the coordinate. A reader
that finds the manifest gone before it reads it, or while it reads it, binds
again the same way, even if another reader has rebuilt it before the failed
read is classified: the failure is classified from its own ENOENT. Eviction
deletes the manifest before its piece, so no reader finds a manifest naming a
missing piece. It runs under the directory's retention lock, which a rebuild
on a miss also takes, so the two never interleave.

A piece skipped because a handle in this process holds it is tried again
once that hold is released (``evict_or_wait``, ``released_pieces``), never
on a schedule; one another process holds is left for the next open's pass,
which reads the directory once.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.indexes.sqlite import (
    ProjectionEvicted,
    held_here,
    load_projection_manifest,
    lock_piece_shared,
    released_pieces,
)

RETENTION_LOCK = ".projection-retention.lock"
_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def evict_projection(directory: Path, manifest_name: str) -> bool | None:
    """Delete one manifest and its piece unless a reader holds it.

    True: deleted. False: held by a reader (try again later). None: there is
    no valid manifest of that name (absent, or recovery's to sweep). The
    caller holds ``retention_lock``.
    """

    manifest_path = directory / manifest_name
    if not manifest_path.exists():
        return None
    try:
        manifest = load_projection_manifest(manifest_path)
    except ProjectionIntegrityError:
        return None
    held: list[int] = []
    pieces = [directory / piece.name for piece in manifest.pieces]
    try:
        for piece in pieces:
            try:
                descriptor = os.open(piece, _OPEN)
            except FileNotFoundError:
                continue
            held.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        # The manifest first: a manifest naming a missing piece refuses binds
        # and recovery, while a piece no manifest names is swept as an orphan.
        manifest_path.unlink(missing_ok=True)
        _fsync_directory(directory)
        for piece in pieces:
            piece.unlink(missing_ok=True)
        _fsync_directory(directory)
        return True
    finally:
        for descriptor in held:
            os.close(descriptor)


EvictionOutcome = Literal["evicted", "absent", "waiting", "held_elsewhere"]


def evict_or_wait(directory: Path, manifest_name: str, sequence: int) -> EvictionOutcome:
    """Evict one manifest and its piece, or say what it waits for.

    ``waiting``: a handle in this process holds it; its release puts it in
    ``released_pieces``. ``held_elsewhere``: another process holds it.
    ``absent``: no valid manifest of that name. The caller holds
    ``retention_lock``.
    """

    outcome = evict_projection(directory, manifest_name)
    if outcome is False:
        with held_here(directory / manifest_name, sequence) as here:
            if here:
                return "waiting"
            # No hold here can come or go now: one released just before the
            # registry was read is evicted, and one still held is elsewhere.
            outcome = evict_projection(directory, manifest_name)
        if outcome is False:
            return "held_elsewhere"
    return "evicted" if outcome else "absent"


__all__ = [
    "RETENTION_LOCK",
    "EvictionOutcome",
    "ProjectionEvicted",
    "evict_or_wait",
    "evict_projection",
    "held_here",
    "lock_piece_shared",
    "released_pieces",
    "retention_lock",
]
