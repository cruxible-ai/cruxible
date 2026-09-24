"""Single-daemon admission exclusion, shared by explicit runs and dispatch.

Journal fencing still guards writes. This lock covers the earlier occurrence
lookup through admission; it is deliberately not a distributed lease.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path

_LOCKS: dict[tuple[Path, str], threading.RLock] = {}
_MUTEX = threading.Lock()


@contextmanager
def line_admission_guard(root: Path, identity: str) -> Iterator[None]:
    with _MUTEX:
        lock = _LOCKS.setdefault((root.resolve(), identity), threading.RLock())
    with lock:
        yield


_ARM_LOCKS: dict[tuple[Path, str], threading.RLock] = {}


@contextmanager
def line_arm_boundary(root: Path, identity: str) -> Iterator[None]:
    """Serialize arming, disarming and stopping with an automatic admission.

    Held only around the admission record itself, never a whole run, so a
    disarm waits for at most one admission append: it lands either before the
    admission (nothing runs) or after it (the admitted run finishes normally).
    """

    with _MUTEX:
        lock = _ARM_LOCKS.setdefault((root.resolve(), identity), threading.RLock())
    with lock:
        yield


#: Set by automatic dispatch for the duration of one Line run: the executor
#: enters it around recording the run's admission, which is the boundary a
#: disarm must be ordered against.
LINE_ARM_ADMISSION_GATE: ContextVar[Callable[[], AbstractContextManager[None]] | None] = ContextVar(
    "line_arm_admission_gate", default=None
)
