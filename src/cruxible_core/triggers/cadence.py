"""Timer arithmetic shared by internal triggers and Line admission.

No Trigger fires retroactively. A timer's instants all follow the acceptance
of the Trigger version that names them: a cadence's instants sit on its own
grid, one interval apart from its acceptance (the first one interval after it),
a cron schedule's are its calendar instants after acceptance, and a successor
schedule starts again from its own acceptance. A tick is its scheduled
instant, never when it ran. A forward-only reader resumes at the timer's first
instant at or after its resume, never the resume itself and never an instant
before it; the instants it skipped are left for explicit evaluation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from cruxible_client.contracts.cron import parse_cron
from cruxible_client.contracts.triggers import CadenceSchedule, CronSchedule, TriggerSchedule

_END_OF_TIME = datetime.max.replace(tzinfo=UTC)


def timer_due(schedule: TriggerSchedule, *, accepted_at: datetime, last: datetime) -> datetime:
    """The timer's first instant after ``last`` (an acceptance or a fire).

    The instant is one `timer_instants` yields: a cadence's grid instant from
    its acceptance, a cron schedule's calendar instant.
    """

    found = next(
        timer_instants(schedule, accepted_at=accepted_at, after=last, through=_END_OF_TIME),
        None,
    )
    if found is None:
        assert isinstance(schedule, CronSchedule)
        raise ValueError(f"cron expression {schedule.expression!r} never fires")
    return found


def timer_instants(
    schedule: TriggerSchedule, *, accepted_at: datetime, after: datetime, through: datetime
) -> Iterator[datetime]:
    """Every instant of a timer in ``(after, through]``, in order, none at or before acceptance.

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
