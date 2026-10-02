"""Snapshot every store a change can write, to prove a preview wrote nothing (R12).

A dry run must write nothing ANYWHERE: the ledger, ``state.db`` and the other
instance stores, the CAS, the runtime credential DB, the registry, provider and
runtime directories, and any workspace or custody directory the caller names.
``snapshot_stores`` records each root itself and everything under it:

* a regular file by its permission bits and the sha256 of its bytes, so a
  rewrite or a ``chmod`` shows, whether the file is a root or a descendant;
* a directory by its permission bits, so a created or removed directory and a
  ``chmod`` of one show;
* a symlink by its target, never followed: a link out of a watched tree is not
  silently watched (or not) through it. A root that is itself a symlink is
  recorded as a link, and its resolved target is watched as a root of its own.

Only SQLite's shared-memory index (``-shm``) is skipped: it holds no data and
any reader maps it. A ``-wal`` file is data and is compared.
"""

from __future__ import annotations

import gc
import hashlib
import os
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar

ResultT = TypeVar("ResultT")


def _entry(path: Path) -> str | None:
    """One path's recorded state, or None for an entry that is never compared."""

    if path.name.endswith("-shm"):
        return None
    mode = path.lstat().st_mode
    bits = f"{stat.S_IMODE(mode):o}"
    if stat.S_ISLNK(mode):
        return f"link:{os.readlink(path)}"
    if stat.S_ISDIR(mode):
        return f"dir:{bits}"
    if stat.S_ISREG(mode):
        return f"file:{bits}:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return f"other:{stat.S_IFMT(mode):o}:{bits}"


def _walk(root: Path, found: dict[str, str]) -> None:
    if not os.path.lexists(root):
        found[str(root)] = "absent"
        return
    entry = _entry(root)
    if entry is not None:
        found[str(root)] = entry
    if root.is_symlink():
        _walk(root.resolve(), found)
        return
    if not root.is_dir():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        # With followlinks=False a symlink to a directory is listed among
        # ``dirnames`` but never descended into; it is recorded as a link.
        for name in sorted([*dirnames, *filenames]):
            path = Path(directory) / name
            entry = _entry(path)
            if entry is not None:
                found[str(path)] = entry


def snapshot_stores(*roots: Path) -> dict[str, str]:
    """Each root and every file, directory and link under it, keyed by path."""

    gc.collect()
    found: dict[str, str] = {}
    for root in roots:
        _walk(root, found)
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
