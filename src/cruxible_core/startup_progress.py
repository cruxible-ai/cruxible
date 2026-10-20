"""A start line and a done line for each slow step a daemon start can run.

Some steps an instance runs when it opens, or a built-in worker runs on its
first pass, take minutes on a large instance: rebuilding the head's projection,
evicting projection pieces past retention, compacting a loose ledger, building
the next facts from nothing. Each logs ``startup_step_started`` before it
begins and ``startup_step_finished`` (or ``startup_step_failed``) after, with
its counts and elapsed seconds, so a daemon that is migrating does not look
hung. `cruxible server restart` and `cruxible server info` read these lines
from the daemon's log; a foreground `cruxible server start` also prints them.

Only the steps that actually do the slow work log: an instance whose
projection is already built, whose ledger is packed and whose retention has
nothing to evict logs nothing here.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

_log = structlog.get_logger("cruxible.startup")

STARTED = "startup_step_started"
FINISHED = "startup_step_finished"
FAILED = "startup_step_failed"
EVENTS = frozenset({STARTED, FINISHED, FAILED})

#: What each step is, in the words its progress line uses.
STEP_LABELS = {
    "daemon_start": "opening instances and running start recovery",
    "projection_rebuild": "rebuilding the head projection",
    "projection_eviction": "evicting projection pieces past retention",
    "ledger_compaction": "compacting the ledger",
    "next_facts_build": "building the next facts",
}

_boot_id: str | None = None
_echo = False


def configure(*, boot_id: str, echo: bool) -> None:
    """Called once by the daemon process: tag lines with its boot id, echo them to stderr."""

    global _boot_id, _echo
    _boot_id = boot_id
    _echo = echo


class StepCounts:
    """The counts a step reports when it is done."""

    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}

    def set(self, **values: int | str) -> None:
        self.values.update(values)


def describe(step: str, instance_id: str | None, counts: dict[str, object]) -> str:
    """One human line for a step: what it is, for which instance, with its counts."""

    label = STEP_LABELS.get(step, step.replace("_", " "))
    where = f" for {instance_id}" if instance_id else ""
    shown = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in counts.items())
    return f"{label}{where}" + (f" ({shown})" if shown else "")


@contextmanager
def slow_step(step: str, *, instance_id: str | None, **known: int | str) -> Iterator[StepCounts]:
    """Log the start of `step`, run it, and log its counts and elapsed seconds."""

    fields: dict[str, object] = {"step": step, "instance_id": instance_id, **known}
    if _boot_id is not None:
        fields["boot_id"] = _boot_id
    _log.info(STARTED, **fields)
    if _echo:
        print(f"cruxible: {describe(step, instance_id, dict(known))}...", file=sys.stderr)
    counts = StepCounts()
    started = time.monotonic()
    try:
        yield counts
    except BaseException as exc:
        elapsed = round(time.monotonic() - started, 1)
        _log.warning(FAILED, elapsed_s=elapsed, reason=type(exc).__name__, **fields)
        if _echo:
            print(
                f"cruxible: {describe(step, instance_id, {})} failed after {elapsed} s",
                file=sys.stderr,
            )
        raise
    elapsed = round(time.monotonic() - started, 1)
    _log.info(FINISHED, elapsed_s=elapsed, **fields, **counts.values)
    if _echo:
        print(
            f"cruxible: {describe(step, instance_id, dict(counts.values))} done in {elapsed} s",
            file=sys.stderr,
        )


__all__ = [
    "EVENTS",
    "FAILED",
    "FINISHED",
    "STARTED",
    "STEP_LABELS",
    "StepCounts",
    "configure",
    "describe",
    "slow_step",
]
