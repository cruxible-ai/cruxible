"""Exclusive daemon ownership of one state root.

Two daemons over the same state root hold the same SQLite files and the same
ledger: a restart that leaves the old process alive means a `/version` probe can
be answered by either image, and two writers race the accepted tree. The lock is
an ``flock`` on ``<state-root>/daemon/lock`` whose body names the holder's pid
and transport, taken BEFORE any store is opened so the second daemon refuses
without touching state.

``flock`` is the authority, not the recorded pid: the kernel releases it when
the holder dies however it died, so a stale file left by a killed daemon is
reclaimed on the next start. The recorded pid and socket exist so the refusal
can name who is holding the root, and so `server stop` can report it.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from cruxible_core.errors import ConfigError

LOCK_FILE_NAME = "lock"
_LOCK_TAG = "cruxible-server-state-lock-v1"


class ServerStateRootLocked(ConfigError):
    """Another live daemon already owns this state root."""

    error_code = "cruxible.server.state_root_locked"


@dataclass(frozen=True)
class StateRootLockRecord:
    """What the lock file says about its holder."""

    pid: int
    transport: str

    def as_dict(self) -> dict[str, object]:
        return {"tag": _LOCK_TAG, "pid": self.pid, "transport": self.transport}


def state_lock_path(state_root: Path) -> Path:
    """Return the lock path for one state root."""

    return state_root / "daemon" / LOCK_FILE_NAME


def read_state_lock(state_root: Path) -> StateRootLockRecord | None:
    """Return the recorded holder, or None when the file is absent or unreadable.

    A malformed or truncated file is not an error: it is a record of a daemon
    that died mid-write, and the flock below is what decides ownership.
    """

    path = state_lock_path(state_root)
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("tag") != _LOCK_TAG:
        return None
    pid = payload.get("pid")
    transport = payload.get("transport")
    if not isinstance(pid, int) or not isinstance(transport, str):
        return None
    return StateRootLockRecord(pid=pid, transport=transport)


def state_lock_holder_is_alive(state_root: Path) -> bool:
    """Return whether a live process currently holds this state root.

    Probed by trying the lock, so the answer is the kernel's and not a guess
    from a pid that may since have been reused.
    """

    path = state_lock_path(state_root)
    if not path.is_file():
        return False
    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


class StateRootLock:
    """Hold one state root exclusively for the daemon process's lifetime."""

    def __init__(self, state_root: Path, *, transport: str) -> None:
        self.path = state_lock_path(state_root)
        self.transport = transport
        self._descriptor: int | None = None

    def acquire(self) -> "StateRootLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            holder = read_state_lock(self.path.parent.parent)
            held_by = (
                f"pid {holder.pid} on {holder.transport}"
                if holder is not None
                else "another live daemon"
            )
            raise ServerStateRootLocked(
                f"state root {self.path.parent.parent} is already served by {held_by}; "
                "repair: stop it with `cruxible server stop`, or start this daemon on a "
                "different --state-root"
            ) from exc
        # Only now is the record ours to replace: a stale body from a dead
        # holder is overwritten, never merged.
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(
            descriptor,
            json.dumps(
                StateRootLockRecord(pid=os.getpid(), transport=self.transport).as_dict(),
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        )
        os.fsync(descriptor)
        self._descriptor = descriptor
        return self

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "StateRootLock":
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


__all__ = [
    "LOCK_FILE_NAME",
    "ServerStateRootLocked",
    "StateRootLock",
    "StateRootLockRecord",
    "read_state_lock",
    "state_lock_holder_is_alive",
    "state_lock_path",
]
