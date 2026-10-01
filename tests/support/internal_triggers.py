"""Fire the live internal Triggers from a chosen instant, for tests of the workers that follow them.

A worker test needs a sweep or a retry fired at the instant it names, not the
schedule arithmetic that would bring one about. The first call for an instance
anchors each live Trigger's grid one interval before it, so that instant fires
every Trigger once; later calls follow the same grids, and each listens from its
own instant, so a call fires exactly the Triggers with an instant there and the
time between calls is downtime. Whether a Trigger fires on time from its real
acceptance is the trigger journal's own tests' to prove.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from cruxible_client.contracts.triggers import CadenceScheduleV1
from cruxible_core.triggers.journal import TriggerEvent, evaluate_triggers, internal_triggers

# Per instance root: the instant its first test fire anchors every grid to.
_STARTED: dict[str, datetime] = {}


def fire_internal_triggers(instance: Any, *, now: datetime) -> tuple[TriggerEvent, ...]:
    started = _STARTED.setdefault(str(instance.root), now)
    triggers = tuple(
        replace(item, accepted_at=started - timedelta(seconds=item.schedule.interval_seconds))
        for item in internal_triggers(instance)
        if isinstance(item.schedule, CadenceScheduleV1)
    )
    return evaluate_triggers(instance, now=now, listening_since=now, triggers=triggers)
