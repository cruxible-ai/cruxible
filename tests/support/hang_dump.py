"""Dump every thread and end the worker when a test outlives its own timeout.

pytest-timeout fails a stalled test with every thread's stack, but its timer
thread needs the GIL to run. A stall that holds the GIL (a C call waiting on a
lock another thread holds while it waits for the GIL, as the scale guard's
progress-handler installs can) never lets it fire, and the run hangs until the
CI job is cancelled with no stack at all.

So each test also arms faulthandler's watchdog, which needs no GIL, for its own
timeout (the ``timeout`` mark, else ``--timeout``) plus a grace period: it can
only fire when pytest-timeout could not. It writes every thread's stack to the
real stderr and exits the worker, so xdist reports the test as crashed with that
stack and the shard finishes. Nothing is armed without a timeout (a local run
without ``--timeout``), or when ``faulthandler_timeout`` is set, which uses the
same watchdog.
"""

from __future__ import annotations

import faulthandler
import sys
from collections.abc import Generator

import pytest

#: Seconds past a test's own timeout before the watchdog fires.
GRACE_SECONDS = 60.0


def item_timeout(item: pytest.Item) -> float | None:
    """The test's pytest-timeout seconds, or None when it has none."""

    marker = item.get_closest_marker("timeout")
    value: object
    if marker is not None:
        value = marker.args[0] if marker.args else marker.kwargs.get("timeout")
    else:
        value = item.config.getoption("timeout", None)
        if value is None:
            try:
                value = item.config.getini("timeout")
            except ValueError:  # pytest-timeout is not installed
                value = None
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _stderr_descriptor(config: pytest.Config) -> int:
    """pytest's own duplicate of the real stderr (outside capture), as its faulthandler uses."""

    try:
        from _pytest.faulthandler import fault_handler_stderr_fd_key

        return config.stash[fault_handler_stderr_fd_key]
    except (ImportError, KeyError):
        assert sys.__stderr__ is not None
        return sys.__stderr__.fileno()


class HangDumpPlugin:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> Generator[None, object, object]:
        timeout = item_timeout(item)
        if timeout is None or float(item.config.getini("faulthandler_timeout") or 0) > 0:
            return (yield)
        faulthandler.dump_traceback_later(
            timeout + GRACE_SECONDS, exit=True, file=_stderr_descriptor(item.config)
        )
        try:
            return (yield)
        finally:
            faulthandler.cancel_dump_traceback_later()
