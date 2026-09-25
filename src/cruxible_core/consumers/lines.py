"""Armed Lines as a consumer kind: forward-only, governed.

An arm follows its Line's trigger events and cadence from the moment it is
armed, and after a restart only from then on: what it did not match stays for
explicit dispatch. Its runs are governed, admitted under the arming credential
it rechecks before every admission (`runtime/line_arms.py`), inside the arm
boundary a disarm or rollover is ordered against.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerRepair,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)
from cruxible_core.exhaust.line_dispatch import dispatch_root
from cruxible_core.governance.actor_context import GovernedActorContext

#: How long an armed Line's own due work may wait before automation reads as stalled.
LINE_STALL_AFTER = timedelta(minutes=15)


class LineArmConsumers:
    name = "line"
    cursor_policy: CursorPolicy = "forward_only"
    effect_class: EffectClass = "governed"
    #: Concurrent automatic drains across all armed Lines.
    workers = 2

    def active(self, instance: Any) -> bool:
        # Opening a managed instance does not arm any Line.
        return dispatch_root(instance).exists()

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        from cruxible_core.service.procedures.line_dispatch import (
            service_match_listening_lines,
        )

        actor = GovernedActorContext(
            actor_type="system",
            actor_id="line-listener",
            org_id=instance.descriptor.instance_id,
            operation_id=daemon_id,
            timestamp=now,
        )
        service_match_listening_lines(instance, actor=actor, now=now, daemon_id=daemon_id)

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        from cruxible_core.service.procedures.line_dispatch import armed_work

        return (ConsumerWork(key=arm["line_id"], item=arm) for arm in armed_work(instance, now=now))

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.runtime import line_arms

        line_arms.dispatch_armed_line(manager, instance_id, work.item)

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        from cruxible_core.service.procedures.line_dispatch import line_arm_health

        return tuple(
            ConsumerHealth(
                kind=self.name,
                consumer_id=arm.line,
                state=state,
                repair=_repair(state, arm.line),
                detail={
                    "arm_id": arm.arm_id,
                    "arm_state": arm.state,
                    "stop_reason": arm.stop_reason,
                    "stopped_at": None if arm.stopped_at is None else arm.stopped_at.isoformat(),
                    "pending_automatic": arm.pending_automatic,
                    "pending_explicit": arm.pending_explicit,
                    "detail": arm.detail,
                },
            )
            for state, arm in line_arm_health(instance, now=now, stall_after=LINE_STALL_AFTER)
        )


def _repair(state: str, line: str) -> ConsumerRepair | None:
    # A stopped arm is resumed by rearming under authority that holds; a Line
    # that stopped draining shows its refusal when dispatched.
    if state == "stopped":
        return ConsumerRepair(
            operation="playbill.line.arm",
            required_change="rearm_the_line_under_a_current_credential_and_version",
            arguments={"line": line.removeprefix("Line:")},
        )
    if state == "stalled":
        return ConsumerRepair(
            operation="playbill.line.dispatch",
            required_change="dispatch_the_line_to_read_why_its_work_is_blocked",
            arguments={"line": line.removeprefix("Line:")},
        )
    return None


LINE_ARMS = LineArmConsumers()
