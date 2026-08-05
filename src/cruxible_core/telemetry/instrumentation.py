"""Best-effort instrumentation at Cruxible-owned protocol boundaries."""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar, cast

CallableT = TypeVar("CallableT", bound=Callable[..., Any])

# Prefix distinguishing a whole-CLI-command surface row from the service verbs
# it invoked: ``cli:stats``, ``cli:telemetry summary``.
CLI_SURFACE_PREFIX = "cli:"

# A CLI command normally invokes one service verb, and a handful at most. The
# cap exists so a command that does NOT terminate promptly cannot turn this list
# into an unbounded leak; such a command should instead opt out of collection
# entirely (see ``long_running_command`` in ``cli/main.py``).
MAX_CLI_BOUNDARY_EVENTS = 512


@dataclass
class CliBoundaryEvent:
    """One service invocation made while a local CLI command is active."""

    instance: Any
    surface_name: str
    duration_ms: float
    error: bool


@dataclass
class CliBoundaryCollector:
    """Events collected until the CLI has emitted its already-formatted output."""

    events: list[CliBoundaryEvent] = field(default_factory=list)
    depth: int = 0
    dropped_events: int = 0
    started_ns: int = field(default_factory=time.perf_counter_ns)

    def add(self, event: CliBoundaryEvent) -> None:
        """Append within the hard cap, counting anything past it as dropped."""
        if len(self.events) >= MAX_CLI_BOUNDARY_EVENTS:
            self.dropped_events += 1
            return
        self.events.append(event)


_CLI_COLLECTOR: contextvars.ContextVar[CliBoundaryCollector | None] = contextvars.ContextVar(
    "cruxible_cli_boundary_collector",
    default=None,
)


@contextmanager
def collect_cli_boundaries() -> Iterator[CliBoundaryCollector]:
    """Collect top-level service verbs invoked by one local CLI command."""
    collector = CliBoundaryCollector()
    token = _CLI_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _CLI_COLLECTOR.reset(token)


def instrument_cli_service(function: CallableT) -> CallableT:
    """Wrap an exported service function for no-op-unless-CLI observation."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        collector = _CLI_COLLECTOR.get()
        if collector is None:
            return function(*args, **kwargs)

        outermost = collector.depth == 0
        collector.depth += 1
        started = time.perf_counter_ns()
        failed = False
        try:
            return function(*args, **kwargs)
        except BaseException:
            failed = True
            raise
        finally:
            collector.depth -= 1
            if outermost:
                instance = _instance_argument(args, kwargs)
                if instance is not None:
                    collector.add(
                        CliBoundaryEvent(
                            instance=instance,
                            surface_name=function.__name__,
                            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
                            error=failed,
                        )
                    )

    return cast(CallableT, wrapped)


def finish_cli_boundaries(
    collector: CliBoundaryCollector,
    *,
    command_path: Sequence[str],
    response_bytes: int,
    command_failed: bool,
) -> None:
    """Persist collected calls after stdout/stderr emission has completed.

    Two distinct surfaces, because they measure two distinct things:

    - each service verb keeps ITS OWN measured duration and contributes zero
      response bytes. A command may invoke several verbs, and the rendered
      output belongs to none of them individually; assigning the whole
      command's wall time and every emitted byte to whichever verb happened to
      run last made that verb's counters describe the command, not the verb.
    - the emitted bytes and the command's wall time are recorded once under a
      ``cli:<command path>`` row, which is the surface that actually owns them.

    The command row is attributed to the last instance a verb touched: one CLI
    command addresses one instance, and were a future command to span several,
    the per-verb rows would still attribute each verb correctly.
    """
    for event in collector.events:
        record_boundary(
            event.instance,
            event.surface_name,
            response_bytes=0,
            duration_ms=event.duration_ms,
            error=event.error,
        )
    if not collector.events or not command_path:
        return
    record_boundary(
        collector.events[-1].instance,
        CLI_SURFACE_PREFIX + " ".join(command_path),
        response_bytes=response_bytes,
        duration_ms=(time.perf_counter_ns() - collector.started_ns) / 1_000_000,
        error=command_failed,
    )


def record_boundary(
    instance: Any,
    surface_name: str,
    *,
    response_bytes: int,
    duration_ms: float,
    error: bool,
) -> None:
    """Record one observation without allowing telemetry to affect the call."""
    try:
        instance.record_boundary_telemetry(
            surface_name,
            response_bytes=response_bytes,
            duration_ms=duration_ms,
            error=error,
        )
    except Exception:
        pass


def _instance_argument(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any | None:
    for value in (*args, *kwargs.values()):
        if callable(getattr(value, "record_boundary_telemetry", None)):
            return value
    return None


__all__ = [
    "CLI_SURFACE_PREFIX",
    "MAX_CLI_BOUNDARY_EVENTS",
    "CliBoundaryCollector",
    "collect_cli_boundaries",
    "finish_cli_boundaries",
    "instrument_cli_service",
    "record_boundary",
]
