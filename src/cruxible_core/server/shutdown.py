"""Graceful daemon shutdown for the `server stop` verb.

The stop contract is "exit this daemon cleanly, releasing its state-root lock".
Operators previously reached for `screen -X quit` or `kill`, which killed the
launching shell and orphaned the daemon: five live trees ended up serving one
state root, and a `/version` probe after a restart could still be answered by
the old image.

``SIGTERM`` to our own pid is what uvicorn already treats as a graceful
shutdown, so the response that acknowledges the stop is flushed and connections
are closed before the process leaves. The signal is deferred a short beat for
exactly that reason, mirroring the restart verb.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable

# Grace period before the signal lands, leaving uvicorn time to flush the stop
# acknowledgement response and close the connection.
_STOP_DELAY_SECONDS = 0.25

# Injection seam: tests replace this so a stop never signals the test runner.
_signal_self: Callable[[], None]


def _default_signal_self() -> None:
    """Ask this process to shut down the way a service manager would."""
    os.kill(os.getpid(), signal.SIGTERM)


_signal_self = _default_signal_self


def schedule_server_stop() -> None:
    """Schedule a graceful shutdown after the current response is flushed."""
    timer = threading.Timer(_STOP_DELAY_SECONDS, _signal_self)
    timer.daemon = True
    timer.start()


def set_signal_self(func: Callable[[], None]) -> None:
    """Override the shutdown callback (test seam)."""
    global _signal_self
    _signal_self = func


def reset_signal_self() -> None:
    """Restore the default shutdown callback (test cleanup)."""
    global _signal_self
    _signal_self = _default_signal_self
