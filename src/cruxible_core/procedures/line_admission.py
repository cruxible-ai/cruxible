"""Single-daemon admission exclusion, shared by explicit runs and dispatch.

Journal fencing still guards writes. This lock covers the earlier occurrence
lookup through admission; it is deliberately not a distributed lease.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCKS: dict[tuple[Path, str], threading.RLock] = {}
_MUTEX = threading.Lock()


@contextmanager
def line_admission_guard(root: Path, identity: str) -> Iterator[None]:
    with _MUTEX:
        lock = _LOCKS.setdefault((root.resolve(), identity), threading.RLock())
    with lock:
        yield
