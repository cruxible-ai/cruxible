"""One log line per write, settle, propose or activation, with its phase durations.

An operation opened with ``operation_timing`` collects the time spent in each
named ``phase`` on its own thread: plan, lower, evaluate, admission,
settlement, projection, publish, post_accept, and git (every git subprocess).
Phases nest and are timed exclusively, so an evaluation inside admission or a
git call inside settlement counts once, in the innermost phase; ``other_ms``
is what no phase claimed. Operations nest too: a write that proposes and
activates logs one line, as the outermost operation. Work handed to another
thread (the advisory ref refresh, consumers) is not part of the line.

The line is the ``write_phases`` event on the ``cruxible.write_phases``
logger, so a slow write in a dogfood log names where its time went without a
profiler. Nothing is served.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import ParamSpec, TypeVar

import structlog

PHASES = (
    "plan",
    "lower",
    "evaluate",
    "admission",
    "settlement",
    "projection",
    "publish",
    "post_accept",
    "git",
)

_log = structlog.get_logger("cruxible.write_phases")


@dataclass
class _Timer:
    operation: str
    started: int
    phases: dict[str, int] = field(default_factory=dict)
    # The open phases, innermost last, each with the instant it last resumed.
    stack: list[tuple[str, int]] = field(default_factory=list)


_CURRENT: ContextVar[_Timer | None] = ContextVar("cruxible_write_phases", default=None)


@contextmanager
def operation_timing(operation: str, **fields: object) -> Iterator[None]:
    """Time one write-side operation and log its phases when the outermost one ends."""

    if _CURRENT.get() is not None:
        yield
        return
    timer = _Timer(operation, time.perf_counter_ns())
    token = _CURRENT.set(timer)
    status = "ok"
    try:
        yield
    except BaseException as exc:
        status = type(exc).__name__
        raise
    finally:
        _CURRENT.reset(token)
        total = time.perf_counter_ns() - timer.started
        claimed = sum(timer.phases.values())
        try:
            _log.info(
                "write_phases",
                operation=operation,
                status=status,
                total_ms=_ms(total),
                **{f"{name}_ms": _ms(timer.phases.get(name, 0)) for name in PHASES},
                other_ms=_ms(max(0, total - claimed)),
                **fields,
            )
        except Exception:  # pragma: no cover - a broken log sink never fails a write
            pass


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Attribute the time inside to ``name``, pausing the enclosing phase."""

    timer = _CURRENT.get()
    if timer is None:
        yield
        return
    now = time.perf_counter_ns()
    if timer.stack:
        outer, since = timer.stack[-1]
        timer.phases[outer] = timer.phases.get(outer, 0) + now - since
    timer.stack.append((name, now))
    try:
        yield
    finally:
        end = time.perf_counter_ns()
        _name, since = timer.stack.pop()
        timer.phases[name] = timer.phases.get(name, 0) + end - since
        if timer.stack:
            timer.stack[-1] = (timer.stack[-1][0], end)


def _ms(nanoseconds: int) -> float:
    return round(nanoseconds / 1_000_000, 3)


_P = ParamSpec("_P")
_R = TypeVar("_R")


def timed_operation(operation: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Run the decorated call as one timed operation (nested calls join the outer one)."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(function)
        def timed(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with operation_timing(operation):
                return function(*args, **kwargs)

        return timed

    return decorate


def timed_phase(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Attribute the decorated call's time to phase ``name``."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(function)
        def timed(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with phase(name):
                return function(*args, **kwargs)

        return timed

    return decorate


__all__ = ["PHASES", "operation_timing", "phase", "timed_operation", "timed_phase"]
