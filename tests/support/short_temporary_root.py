"""Create scratch directories short enough to hold an AF_UNIX socket path."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["short_temporary_directory"]


def short_temporary_directory(prefix: str) -> Path:
    """Create a fresh directory under the shortest writable system temporary root.

    Tests that assert a socket stays in place need a root inside the 103-byte
    AF_UNIX budget, and the platform default (`TMPDIR` on macOS, or a sandbox's
    per-session directory) can already be most of it. The standard library's
    own candidate list names the platform's temporary roots without a test
    hard-coding one; the working directory is excluded so scratch never lands in
    the checkout. The caller owns removal.
    """

    working_directory = os.path.realpath(os.getcwd())
    candidates = sorted(
        {
            candidate
            for candidate in tempfile._candidate_tempdir_list()
            if os.path.realpath(candidate) != working_directory
        },
        key=len,
    )
    for candidate in candidates:
        try:
            return Path(tempfile.mkdtemp(prefix=prefix, dir=candidate))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix=prefix))
