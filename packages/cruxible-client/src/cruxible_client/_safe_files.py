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
