"""The daemon clock every consumer kind shares: when a cadence is next due.

A cadence is due one interval after its last completion, never before a
floor. The floor is how a forward-only consumer resumes: an armed Line that
starts or restarts later than its chain's next tick ticks from its own start,
never catching up on what it missed.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def cadence_due(
    interval: timedelta, *, last: datetime | None, not_before: datetime | None = None
) -> datetime | None:
    """The next due instant, or None when the cadence has never completed (due now)."""

    if last is None:
        return None
    due = last + interval
    return due if not_before is None else max(due, not_before)
