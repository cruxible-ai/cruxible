"""Timer arithmetic shared by internal triggers and Line admission.

No Trigger fires retroactively. A timer's instants all follow the acceptance
of the Trigger version that names them: a cadence's first instant is its
acceptance plus one interval, a cron schedule's first is its first calendar
instant after acceptance, and a successor schedule starts again from its own
acceptance. A Line occurrence chain then continues from the last occurrence its
Trigger fired; a floor is how a forward-only reader resumes, never catching up
on instants before it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

from cruxible_client.contracts.cron import parse_cron
from cruxible_client.contracts.triggers import CadenceSchedule, CronSchedule, TriggerSchedule


def timer_due(
    schedule: TriggerSchedule, *, last: datetime, not_before: datetime | None = None
) -> datetime:
    """The first instant after ``last`` (an acceptance or a fire), never before a floor.

    A cadence is due one interval after ``last``, or at the floor if that is later;
    a cron schedule at its first calendar instant after ``last`` and at or after
    the floor.
    """

    if isinstance(schedule, CadenceSchedule):
        due = last + timedelta(seconds=schedule.interval_seconds)
        return due if not_before is None else max(due, not_before)
    if isinstance(schedule, CronSchedule):
        spec = parse_cron(schedule.expression)
        floor = last if not_before is None else max(last, not_before - timedelta(microseconds=1))
        found = spec.next_after(floor)
        if found is None:
            raise ValueError(f"cron expression {schedule.expression!r} never fires")
        return found
    raise ValueError(f"Trigger schedule kind {schedule.kind!r} is not a timer")


def timer_instants(
    schedule: TriggerSchedule, *, accepted_at: datetime, after: datetime, through: datetime
) -> Iterator[datetime]:
    """Every instant of a timer in ``(after, through]``, in order.

    A cadence's instants sit on its own grid, one interval apart from its
    acceptance; skipping some never moves the ones after them.
    """

    if isinstance(schedule, CadenceSchedule):
        interval = timedelta(seconds=schedule.interval_seconds)
        steps = max(1, (after - accepted_at) // interval + 1)
        instant = accepted_at + steps * interval
        while instant <= through:
            yield instant
            instant += interval
        return
    if isinstance(schedule, CronSchedule):
        spec = parse_cron(schedule.expression)
        found = spec.next_after(max(after, accepted_at))
        while found is not None and found <= through:
            yield found
            found = spec.next_after(found)
        return
    raise ValueError(f"Trigger schedule kind {schedule.kind!r} is not a timer")
