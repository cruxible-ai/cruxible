"""Timer arithmetic shared by internal triggers and Line admission.

A cadence is due one interval after its last completion; a cron schedule at
its next calendar instant after its last completion. Neither back-fills: a cron
schedule whose next instant has already passed is due once, at the latest
instant that has passed, and a cadence restarts from whoever fires it. Neither
is due before a floor. The floor is how a forward-only consumer resumes: an
armed Line that starts or restarts later than its chain's next tick ticks from
its own start, never catching up on what it missed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from cruxible_client.contracts.cron import parse_cron
from cruxible_client.contracts.triggers import CadenceScheduleV1, CronScheduleV1, TriggerScheduleV1


def cadence_due(
    interval: timedelta, *, last: datetime | None, not_before: datetime | None = None
) -> datetime | None:
    """The next due instant, or None when the cadence has never completed (due now)."""

    if last is None:
        return None
    due = last + interval
    return due if not_before is None else max(due, not_before)


def cron_due(
    schedule: CronScheduleV1,
    *,
    last: datetime | None,
    now: datetime,
    not_before: datetime | None = None,
) -> datetime | None:
    """The cron instant due next: a future instant, or the latest one already passed.

    With no completion and no floor the latest instant at or before ``now`` is
    due, so a new schedule fires its most recent instant once. None only for an
    expression with no instant in the search horizon, which the Trigger law
    refuses at acceptance.
    """

    spec = parse_cron(schedule.expression, schedule.timezone)
    candidates = []
    if last is not None:
        candidates.append(spec.next_after(last))
    if not_before is not None:
        candidates.append(spec.next_after(not_before - timedelta(microseconds=1)))
    floor = max((item for item in candidates if item is not None), default=None)
    if floor is not None and floor > now:
        return floor
    latest = spec.latest_at_or_before(now)
    if floor is not None and (latest is None or latest < floor):
        return floor
    return latest


def timer_due(
    schedule: TriggerScheduleV1,
    *,
    last: datetime | None,
    now: datetime,
    not_before: datetime | None = None,
) -> datetime | None:
    """When a timed schedule is next due; None when a cadence never completed (due now)."""

    if isinstance(schedule, CadenceScheduleV1):
        return cadence_due(
            timedelta(seconds=schedule.interval_seconds), last=last, not_before=not_before
        )
    if isinstance(schedule, CronScheduleV1):
        return cron_due(schedule, last=last, now=now, not_before=not_before)
    raise ValueError(f"Trigger schedule kind {schedule.kind!r} is not a timer")
