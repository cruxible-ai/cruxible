"""On-demand manual fire of one registered internal action.

An internal action normally fires on a Trigger's schedule, through the
trigger journal every Trigger-aimed fire and every internal deadline shares
(``cruxible_core.triggers.journal``). This surface lets an operator or agent
append one more fire to that same journal by hand, outside any schedule: the
event records that it was manual and who asked for it, and the owning
consumer picks it up exactly as it would a scheduled fire -- no Trigger need
exist for the action at all.

``wait`` additionally runs the owning consumer's due work inline and reports
what happened, bounded by ``wait_timeout_seconds``, instead of leaving pickup
to the consumer's own next pass. Not every action's consumer admits a safe
inline run yet; see ``ActionRunOutcome`` for what each one reports.
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
    #: Run the owning consumer's due work inline and report its outcome,
    #: rather than leaving pickup to the consumer's own next pass.
    wait: bool = False
    wait_timeout_seconds: float = Field(default=30.0, gt=0, le=300)


#: ``completed``: the owning consumer ran its due work inline and the detail
#: below is what it did. ``timed_out``: the consumer was still running at
#: ``wait_timeout_seconds``. ``not_waited``: the caller did not ask to wait,
#: or (``floor.refresh`` only) an inline run is not available on this action
#: yet -- its own admission discipline keeps a floor render from overlapping
#: another, so firing it manually here only records the fire; the daemon's
#: own tick, or ``cruxible floor deliver-now``, carries out the delivery.
ActionRunOutcomeStatus = Literal["completed", "timed_out", "not_waited"]


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
