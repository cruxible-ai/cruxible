"""One nonblocking regular-file reader for workspace-controlled inputs.

Check the opened descriptor, not a pathname pre-check: replacing a regular file
with a FIFO must refuse promptly. Callers may anchor a basename to a held parent
and bound reads used for comparisons or processing budgets.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


class SafeFileReadError(OSError):
    """The opened input is not a regular file."""


def read_regular_file(
    path: str | Path, *, dir_fd: int | None = None, max_bytes: int | None = None
) -> bytes:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("read limit must not be negative")
    handle = os.open(path, _READ, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise SafeFileReadError(f"{path} is not a regular file")
        chunks = []
        remaining = max_bytes
        while remaining is None or remaining > 0:
            chunk = os.read(handle, (1 << 20) if remaining is None else min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            if remaining is not None:
                remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)


class ContainmentError(ValueError):
    """A path resolves outside the root a caller named (through any symlink on the way)."""


def _case_folding(root: Path) -> bool:
    """Whether ``root``'s volume folds case: the same directory answers to a swapped name."""

    text = str(root)
    swapped = text.swapcase()
    if swapped == text:
        return False
    try:
        return os.path.samefile(text, swapped)
    except OSError:
        return False


def contained_path(root: Path, path: Path) -> Path:
    """``path`` fully resolved, refused unless it lies beneath ``root`` resolved.

    Every component is resolved, so a symlinked parent directory that leads out
    of the root is refused as surely as a symlinked file; the comparison folds
    case on a case-folding volume. Raises ``ContainmentError`` (and
    ``FileNotFoundError`` when nothing is there).
    """

    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    inside = resolved.is_relative_to(resolved_root)
    if not inside and _case_folding(resolved_root):
        inside = Path(str(resolved).casefold()).is_relative_to(Path(str(resolved_root).casefold()))
    if not inside:
        raise ContainmentError(f"{path} resolves outside {root}")
    return resolved


def read_contained(root: Path, path: Path) -> bytes:
    """The regular file at ``path``, read only once it is contained beneath ``root``."""

    return read_regular_file(contained_path(root, path))
