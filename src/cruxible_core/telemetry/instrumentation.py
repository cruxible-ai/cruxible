"""Best-effort instrumentation at Cruxible-owned protocol boundaries."""

from __future__ import annotations

import contextvars
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar, cast

CallableT = TypeVar("CallableT", bound=Callable[..., Any])


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
    started_ns: int = field(default_factory=time.perf_counter_ns)


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
                    collector.events.append(
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
    response_bytes: int,
    command_failed: bool,
) -> None:
    """Persist collected calls after stdout/stderr emission has completed.

    A CLI command normally invokes one service verb. When a command invokes
    several sequential verbs, only the final verb owns the combined rendered
    response; earlier calls still receive count/duration observations with zero
    response bytes.
    """
    final_index = len(collector.events) - 1
    cli_duration_ms = (time.perf_counter_ns() - collector.started_ns) / 1_000_000
    for index, event in enumerate(collector.events):
        record_boundary(
            event.instance,
            event.surface_name,
            response_bytes=response_bytes if index == final_index else 0,
            duration_ms=cli_duration_ms if index == final_index else event.duration_ms,
            error=event.error or (command_failed and index == final_index),
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
    "CliBoundaryCollector",
    "collect_cli_boundaries",
    "finish_cli_boundaries",
    "instrument_cli_service",
    "record_boundary",
]
