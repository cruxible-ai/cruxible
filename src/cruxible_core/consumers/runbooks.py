"""Trigger-fired Runbooks as a consumer kind: resuming, governed (D-R36).

A live Trigger aimed at a Runbook fires into the trigger journal under the
Runbook's fire key, on a time schedule or generation acceptance, exactly as an
internal action's Trigger does. This kind follows those fires: every pending
fire of one Runbook folds into one run at the head (a burst is one run), and
its cursor moves only once that run is recorded.

A Trigger-fired run has no caller. Each Line step runs under that Line's own
enablement, as a Trigger aimed at the Line would: the arming credential is
rechecked (``arm_authority``) and the run pins the Line version it armed. A
Line that is not enabled, or whose enablement no longer holds, stops the run
with ``line_not_enabled``. So a Runbook Trigger never runs a Line that no one
has enabled, and never under authority someone else holds.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.triggers import RUNBOOK_FIRE_PREFIX
from cruxible_core.consumers.protocol import (
    ConsumerHealth,
    ConsumerWork,
    CursorPolicy,
    EffectClass,
)


def _fire_keys(instance: Any) -> frozenset[str]:
    from cruxible_core.triggers.journal import internal_triggers

    return frozenset(
        item.action
        for item in internal_triggers(instance)
        if item.action.startswith(RUNBOOK_FIRE_PREFIX)
    )


def _active_arm(instance: Any, line: str) -> dict[str, Any] | None:
    """The Line's active enablement segment, or None: one keyed read."""

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
    from cruxible_core.service.procedures.line_dispatch import _active_session

    if not dispatch_root(instance).exists():
        return None
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        return _active_session(conn, line_identity_digest(ArtifactIdentity(kind="Line", name=line)))


def run_fired_runbook(
    manager: Any,
    instance_id: str,
    *,
    name: str,
    trigger: str | None,
    now: datetime,
) -> Any:
    """One Trigger-fired run: every Line step under its own Line's enablement."""

    from cruxible_core.documents.workspace_file import WorkspaceFileReadRefused
    from cruxible_core.runtime.line_arms import arm_authority
    from cruxible_core.service.procedures.line_dispatch import LineArmAuthorityLost
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from cruxible_core.service.runbooks import (
        LineStepNotRun,
        accepted_runbook,
        execute_runbook,
        record_runbook_run,
    )
    from cruxible_core.triggers.journal import fire_manual

    instance = manager.get(instance_id)
    accepted = accepted_runbook(instance, name)
    try:
        reader = manager.workspace_file_reader(instance_id)
    except WorkspaceFileReadRefused:
        reader = None

    def run_line(line: str) -> Any:
        arm = _active_arm(instance, line)
        if arm is None:
            return LineStepNotRun(
                "not_enabled",
                "line_not_enabled",
                f"Line {line!r} is not enabled; a Trigger-fired Runbook runs a Line only "
                "under its own enablement.",
            )
        principal = LineEnablementPrincipal.model_validate(arm["armed_by"])
        try:
            actor, rung = arm_authority(instance, principal, now=now)
        except LineArmAuthorityLost as lost:
            return LineStepNotRun("not_enabled", lost.reason, lost.detail)
        return service_run_playbill_line(
            instance,
            path_identity_digest=line,
            request=LineRunRequest(line=line),
            actor_context=actor,
            caller_rung=rung,
            provider_runtime_operator=manager.provider_runtime_operator(),
            workspace_file_reader=reader,
            evaluation_instant_skew=manager.procedure_run_config().evaluation_instant_skew,
            expected_line_artifact_digest=arm["line_artifact_digest"],
        )

    def fire_action(action: str) -> int:
        return fire_manual(instance, action=action, principal=f"Runbook:{name}", now=now).sequence

    result = execute_runbook(
        accepted,
        mode="trigger",
        trigger=trigger,
        run_line=run_line,
        fire_action=fire_action,
        clock=lambda: now,
    )
    record_runbook_run(instance, result)
    return result


class RunbookConsumers:
    name = "runbook"
    #: Pending fires survive a restart: they fold into one run at the head.
    cursor_policy: CursorPolicy = "resume"
    effect_class: EffectClass = "governed"
    workers = 1

    def active(self, instance: Any) -> bool:
        return bool(_fire_keys(instance))

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        # The trigger journal is the subscription; nothing to record here.
        return None

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        from cruxible_core.triggers.journal import pending_runbook_fires

        keys = _fire_keys(instance)
        for progress in pending_runbook_fires(instance):
            if progress.fire_key in keys:
                yield ConsumerWork(
                    key=progress.fire_key,
                    item={"fire_key": progress.fire_key, "through": progress.latest},
                )

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        from cruxible_core.service.runbooks import RunbookNotFoundError
        from cruxible_core.triggers.journal import finish_runbook_fire, trigger_events

        instance = manager.get(instance_id)
        key = str(work.item["fire_key"])
        through = int(work.item["through"])
        fired = trigger_events(instance, after=through - 1, action=key, limit=1)
        trigger = fired[0].trigger if fired else None
        try:
            run_fired_runbook(
                manager,
                instance_id,
                name=key.removeprefix(RUNBOOK_FIRE_PREFIX),
                trigger=trigger,
                now=now,
            )
        except RunbookNotFoundError:
            pass  # retired since it fired: nothing runs, the fires are covered
        finish_runbook_fire(instance, key, through)

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        from cruxible_core.service.runbooks import latest_runbook_run

        healths = []
        for key in sorted(_fire_keys(instance)):
            name = key.removeprefix(RUNBOOK_FIRE_PREFIX)
            last = latest_runbook_run(instance, name)
            healths.append(
                ConsumerHealth(
                    kind=self.name,
                    consumer_id=f"Runbook:{name}",
                    state="running",
                    detail={}
                    if last is None
                    else {
                        "last_run_id": last.run_id,
                        "last_status": last.status,
                        "last_stop_reason": last.stop_reason,
                        "last_finished_at": last.finished_at.isoformat(),
                    },
                )
            )
        return tuple(healths)


RUNBOOKS = RunbookConsumers()

__all__ = ["RUNBOOKS", "RunbookConsumers", "run_fired_runbook"]
