"""Out-of-band changes to proposal evidence files, made the way a later change would be."""

from __future__ import annotations

import os
from pathlib import Path


def remove_later(path: Path) -> None:
    """Delete an evidence file as a later, separate change would.

    The proposal index trusts its evidence directories' timestamps. Linux stamps
    them from a coarse clock, so a delete within milliseconds of the write can
    leave the directory's timestamps unchanged; moving its mtime forward models
    a deletion that happens after the write, as it does outside a test.
    """

    path.unlink()
    directory = path.parent.stat()
    os.utime(path.parent, ns=(directory.st_atime_ns, directory.st_mtime_ns + 1_000_000_000))
