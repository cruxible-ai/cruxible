"""Storage package root.

PC-F deleted the SQLite graph repository and the storage protocols it
implemented, so the lazy compatibility export map they backed is gone with
them. :mod:`cruxible_core.indexes.sqlite` is the only backend
left and is imported directly by its callers.
"""

from __future__ import annotations

import os
from pathlib import Path


def fsync_directory(path: Path) -> None:
    """Flush ``path``'s directory entries, so a rename or create inside it is durable."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["fsync_directory"]
