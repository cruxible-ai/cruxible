"""Snapshot every store a change can write, to prove a preview wrote nothing (R12).

A dry run must write nothing ANYWHERE: the ledger, ``state.db`` and the other
instance stores, the CAS, the runtime credential DB, the registry, provider and
runtime directories, and any workspace or custody directory the caller names.
``snapshot_stores`` hashes every regular file under each root (bytes and mode)
and lists every directory, so a created, removed or rewritten file or a new
directory all show as a difference.

Only SQLite's shared-memory index (``-shm``) is skipped: it holds no data and
any reader maps it. A ``-wal`` file is data and is compared.
"""

from __future__ import annotations

import gc
import hashlib
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

ResultT = TypeVar("ResultT")


def snapshot_stores(*roots: Path) -> dict[str, str]:
    """Every file and directory under ``roots``, keyed by path."""

    gc.collect()
    found: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            found[str(root)] = "absent"
            continue
        for path in sorted(root.rglob("*")):
            if path.name.endswith("-shm"):
                continue
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                found[str(path)] = "dir"
            elif stat.S_ISLNK(mode):
                found[str(path)] = f"link:{path.readlink()}"
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                found[str(path)] = f"{stat.S_IMODE(mode):o}:{digest}"
    return found


def assert_writes_nothing(
    roots: Iterable[Path], call: Callable[[], ResultT], *, warm: Callable[[], object] | None = None
) -> ResultT:
    """Run ``call`` (a preview) and fail if any store under ``roots`` changed.

    ``warm`` runs first and outside the comparison: an ordinary read that lets a
    derived index catch up on disk, so what is compared is the preview alone.
    """

    if warm is not None:
        warm()
    watched = tuple(roots)
    before = snapshot_stores(*watched)
    result = call()
    after = snapshot_stores(*watched)
    changed = sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
    )
    assert not changed, f"a preview wrote: {changed[:20]}"
    return result


__all__ = ["assert_writes_nothing", "snapshot_stores"]
