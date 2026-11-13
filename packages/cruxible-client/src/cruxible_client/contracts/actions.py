"""On-demand manual fire of one registered internal action.

An internal action normally fires on a Trigger's schedule, through the
trigger journal every Trigger-aimed fire and every internal deadline shares
(``cruxible_core.triggers.journal``). This surface lets an operator or agent
append one more fire to that same journal by hand, outside any schedule: the
event records that it was manual and who asked for it, and the owning
consumer picks it up exactly as it would a scheduled fire -- no Trigger need
exist for the action at all.

``wait`` additionally observes the owning part's own recorded progress,
bounded by ``wait_timeout_seconds``, until it shows this fire processed,
instead of leaving pickup to the consumer's own next pass. It starts, admits
and runs no work itself: the daemon's normal consumer runner is what
executes the fire, exactly as it would a scheduled one; see
``ActionRunOutcome`` for the statuses waiting can report.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.triggers import InternalActionName


class _StrictActionRunModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionRunRequest(_StrictActionRunModel):
    tag: Literal["playbill-action-run-request-v1"] = "playbill-action-run-request-v1"
    action: InternalActionName
    #: Observe the owning part's own progress until it covers this fire,
    #: instead of leaving pickup to the consumer's own next pass.
    wait: bool = False
    wait_timeout_seconds: float = Field(default=60.0, gt=0, le=300)


#: ``completed``: the owning part's own recorded progress now covers this
#: fire; the detail names the sequence it covers through. ``timed_out``: it
#: did not, by ``wait_timeout_seconds`` -- the fire stays queued and the
#: daemon's normal consumer runner will still process it on its own tick.
#: ``not_waited``: the caller did not ask to wait. ``not_supported``: this
#: action's owning part exposes no completion cursor the wait path can
#: observe (the fire is still recorded and still runs normally).
ActionRunOutcomeStatus = Literal["completed", "timed_out", "not_waited", "not_supported"]


class ActionRunOutcome(_StrictActionRunModel):
    status: ActionRunOutcomeStatus
    detail: dict[str, Any] = Field(default_factory=dict)


class ActionRunResult(_StrictActionRunModel):
    tag: Literal["playbill-action-run-result-v1"] = "playbill-action-run-result-v1"
    action: str
    #: The fired event's sequence in the trigger journal.
    event_id: int
    fired_at: datetime
    outcome: ActionRunOutcome


__all__ = [
    "ActionRunOutcome",
    "ActionRunOutcomeStatus",
    "ActionRunRequest",
    "ActionRunResult",
]
