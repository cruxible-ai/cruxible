"""Standard five-field cron, evaluated in UTC.

The grammar is the classic one and nothing more: ``minute hour day-of-month
month day-of-week``, each field ``*``, a number, a range ``a-b``, a step
``*/n`` or ``a-b/n``, or a comma list of those. Day-of-week is 0-7 with both 0
and 7 meaning Sunday. Names (``MON``, ``JAN``), ``?``, ``L``, ``W``, ``#`` and
``@`` macros are not part of it, so an expression means the same thing to every
reader. As in classic cron, when both day fields are restricted a day matches
if either does.

Instants are UTC times, on whole minutes. Governed schedules are UTC only:
an instant is a pure function of the expression, never of a host's timezone
database, so every daemon and reader agrees on it. Named timezones would need a
bundled, versioned ruleset of their own.

No dependency: the matcher walks calendar days and only the hours and minutes
the expression names, which keeps a search to a handful of candidates for any
expression that fires at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

#: How far a search walks before concluding an expression never fires. A
#: weekday-restricted 29 February recurs within 28 years except across a
#: skipped century leap day; four centuries covers every Gregorian cycle.
_HORIZON_DAYS = 400 * 366

_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


class CronExpressionError(ValueError):
    """A cron expression is not valid under the supported grammar."""


@dataclass(frozen=True)
class CronSpec:
    """One parsed expression: the values each field admits."""

    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_restricted: bool
    weekdays_restricted: bool

    def _day_matches(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        weekday = day.isoweekday() % 7
        by_day = day.day in self.days
        by_weekday = weekday in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return by_day or by_weekday
        return by_day and by_weekday

    def _instants(self, day: date, *, reverse: bool) -> Iterator[datetime]:
        hours = reversed(self.hours) if reverse else iter(self.hours)
        for hour in hours:
            minutes = reversed(self.minutes) if reverse else iter(self.minutes)
            for minute in minutes:
                yield datetime.combine(day, time(hour, minute), tzinfo=UTC)

    def next_after(self, moment: datetime) -> datetime | None:
        """The first instant strictly after ``moment``."""

        start = moment.astimezone(UTC).date()
        for offset in range(_HORIZON_DAYS):
            day = start + timedelta(days=offset)
            if self._day_matches(day):
                for instant in self._instants(day, reverse=False):
                    if instant > moment:
                        return instant
        return None

    def latest_at_or_before(self, moment: datetime) -> datetime | None:
        """The last instant at or before ``moment``."""

        start = moment.astimezone(UTC).date()
        for offset in range(_HORIZON_DAYS):
            day = start - timedelta(days=offset)
            if self._day_matches(day):
                for instant in self._instants(day, reverse=True):
                    if instant <= moment:
                        return instant
        return None


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronExpressionError(f"{name} step {step_text!r} is not a positive integer")
            step = int(step_text)
        if base == "*":
            first, last = low, high
        else:
            first_text, dash, last_text = base.partition("-")
            if not first_text.isdigit() or (dash and not last_text.isdigit()):
                raise CronExpressionError(f"{name} value {item!r} is not a number, range or *")
            first = int(first_text)
            last = int(last_text) if dash else (high if slash else first)
            if dash and first > last:
                raise CronExpressionError(f"{name} range {base!r} runs backwards")
        if first < low or last > high:
            raise CronExpressionError(f"{name} value {item!r} is outside {low}-{high}")
        values.update(range(first, last + 1, step))
    return frozenset(values)


@lru_cache(maxsize=256)
def parse_cron(expression: str) -> CronSpec:
    """Parse one five-field UTC expression, refusing anything else."""

    parts = expression.split()
    if len(parts) != 5 or " ".join(parts) != expression:
        raise CronExpressionError(
            "a cron expression is exactly five single-space-separated fields: "
            "minute hour day-of-month month day-of-week"
        )
    minutes, hours, days, months, weekdays = (
        _field(text, name, low, high)
        for text, (name, low, high) in zip(parts, _FIELDS, strict=True)
    )
    spec = CronSpec(
        minutes=tuple(sorted(minutes)),
        hours=tuple(sorted(hours)),
        days=days,
        months=months,
        weekdays=frozenset(value % 7 for value in weekdays),
        days_restricted=not parts[2].startswith("*"),
        weekdays_restricted=not parts[4].startswith("*"),
    )
    if spec.next_after(datetime(2000, 1, 1, tzinfo=UTC)) is None:
        raise CronExpressionError(f"cron expression {expression!r} never fires")
    return spec


__all__ = ["CronExpressionError", "CronSpec", "parse_cron"]
