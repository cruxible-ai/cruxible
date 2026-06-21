"""In-place daemon re-exec for the dev-loop restart command.

The restart contract is "re-exec the daemon's own process image in place,
preserving state dir, port, and env". ``os.execv`` replaces the current
process while keeping the environment, so the launch env vars
(``CRUXIBLE_PORT``, ``CRUXIBLE_HOST``, ``CRUXIBLE_SERVER_SOCKET``,
``CRUXIBLE_MODE``, credentials) all carry across automatically and the new
image binds the same transport. Re-using the original ``sys.argv`` means the
exact launch command is reproduced rather than reinvented.

The exec is deferred a short beat so uvicorn can flush the HTTP response that
confirms the restart before the process image is replaced; otherwise the
client would see a dropped connection instead of an acknowledgement.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable

# Grace period before the process re-execs, leaving uvicorn time to flush the
# restart acknowledgement response and close the connection.
_RESTART_DELAY_SECONDS = 0.25

# Injection seam: tests replace this so a restart never re-execs the test runner.
_exec_self: Callable[[], None]


def _default_exec_self() -> None:
    """Replace the current process image with a faithful copy of the launch."""
    os.execv(sys.executable, [sys.executable, *sys.argv])


_exec_self = _default_exec_self


def restart_command() -> list[str]:
    """Return the argv the daemon will re-exec with (for diagnostics/echo)."""
    return [sys.executable, *sys.argv]


def schedule_server_restart() -> None:
    """Schedule an in-place re-exec after the current response is flushed."""
    timer = threading.Timer(_RESTART_DELAY_SECONDS, _exec_self)
    timer.daemon = True
    timer.start()


def set_exec_self(func: Callable[[], None]) -> None:
    """Override the re-exec callback (test seam)."""
    global _exec_self
    _exec_self = func


def reset_exec_self() -> None:
    """Restore the default re-exec callback (test cleanup)."""
    global _exec_self
    _exec_self = _default_exec_self
