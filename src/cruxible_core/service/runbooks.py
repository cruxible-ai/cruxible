"""Run an accepted Runbook: its steps in order, stopping on a failure or on review (D-R36).

Each Line step is that Line's ordinary manual occurrence
(``service_run_playbill_line``): its own parameters, budgets, authority
ceiling and mandate, recorded on the Line's history. Each action step fires
the registered internal action into the trigger journal exactly as
``action run`` does (its effect is findings or workspace output, never
governed state, so the run does not wait for it).

The run stops at the first step that

- fails: the Line run refused or failed, or a terminal did not deliver; or
- sends writes to review: a terminal delivered a proposal, or kept the Line's
  open proposal current. A later step would read state the review has not
  decided, so it does not run.

A manual run (``runbook run``) runs every step under the caller's authority.
A Trigger-fired run runs each Line step under that Line's own enablement, as a
Trigger aimed at the Line would: a Line that is not enabled stops the run.
Either way a Runbook never widens anyone's authority.

Overhead per step is constant: one accepted-Runbook read per run, and per step
one Line run (whose own cost is the Line's), or one journal append.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.runbooks import (
    AcceptedRunbook,
    Runbook,
    RunbookRunResult,
    RunbookStepOutcome,
    RunbookStopReason,
    runbook_attention_steps,
    runbook_digest,
    runbook_path,
)
from cruxible_client.contracts.triggers import ActionTarget, LineTarget
from cruxible_core.consumers.state import DisposableState
from cruxible_core.errors import CoreError
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.service.procedures.procedure_runs import ProcedureSurfaceError

if TYPE_CHECKING:
    from cruxible_core.runtime.instance import PlaybillInstance
    from cruxible_core.service.procedures.procedure_runs import ProcedureRunStateV2


class RunbookNotFoundError(ProcedureSurfaceError):
    """No live accepted Runbook has this name at the accepted head."""

    code = "cruxible.runbook.not_found"
    http_status = 404


#: Retained run records: derived bookkeeping, never authority. Every Line run is
#: on its Line's history, and a Trigger-fired run's progress is in the trigger
#: journal, so a lost record is rebuilt by replaying its fire (nothing reruns).
RUNBOOK_STATE = DisposableState(
    "runbooks",
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, runbook TEXT NOT NULL, "
    "finished_at TEXT NOT NULL, payload TEXT NOT NULL) STRICT;"
    "CREATE INDEX runs_by_runbook ON runs(runbook, finished_at);",
)


@dataclass(frozen=True)
class LineStepNotRun:
    """A step the runner could not start, and why (a Trigger-fired run only)."""

    status: Literal["not_enabled", "unavailable"]
    code: str
    message: str


#: Each runner takes the target and the step's 1-based position.
LineStepRunner = Callable[[str, int], "ProcedureRunStateV2 | LineStepNotRun"]
ActionStepFirer = Callable[[str, int], "int | LineStepNotRun"]


def fired_run_id(name: str, fire: int) -> str:
    """A Trigger-fired run's id: the Runbook and the fire it serves, stable on replay."""

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest

    digest = typed_digest(
        Sha256Value, "cruxible-runbook-fired-run-v1", {"fire": fire, "runbook": name}
    ).tagged.removeprefix("sha256:")
    return f"RBR-{digest[:32]}"


def accepted_runbook(instance: PlaybillInstance, name: str) -> AcceptedRunbook:
    """The live accepted Runbook ``name`` at the head: one typed source read."""

    coordinate = instance.accepted_coordinate()
    with instance.bind_accepted_projection(coordinate) as projection:
        source = projection.typed.source(f"Runbook:{name}")
    if not isinstance(source, Runbook) or source.lifecycle.state != "live":
        raise RunbookNotFoundError(
            f"No live accepted Runbook named {name!r}; author one with a runbook input."
        )
    return AcceptedRunbook(
        path=runbook_path(name), runbook=source, artifact_digest=runbook_digest(source).tagged
    )


def _error_code(exc: BaseException) -> str:
    for attribute in ("error_code", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, str) and value:
            return value
    return type(exc).__name__


def _terminal_code(state: Any) -> str | None:
    terminal = getattr(state, "terminal", None)
    code = getattr(terminal, "code", None)
    if isinstance(code, str):
        return code
    classification = getattr(terminal, "classification", None)
    return classification if isinstance(classification, str) else None


def _line_outcome(step: int, target: LineTarget, state: Any) -> RunbookStepOutcome:
    """One Line run read as a step: succeeded, proposed (stops) or failed (stops)."""

    base: dict[str, Any] = {
        "step": step,
        "target": target,
        "run_id": state.run_id,
        "run_status": state.status,
        "counts": getattr(state, "counts", None),
    }
    if state.status != "succeeded":
        terminal = getattr(state, "terminal", None)
        return RunbookStepOutcome(
            **base,
            status="failed",
            code=_terminal_code(state) or state.status,
            message=getattr(terminal, "message", None),
        )
    proposals: list[str] = []
    for egress in state.terminal_egress:
        if egress.verdict != "delivered":
            return RunbookStepOutcome(
                **base,
                status="failed",
                code=egress.refusal_code or f"egress_{egress.verdict}",
                message=f"Terminal {egress.node_id!r} did not deliver ({egress.verdict}).",
            )
        if egress.settle_outcome == "settled":
            continue  # accepted in the run: nothing waits for review
        if egress.proposal_id is not None:
            proposals.append(egress.proposal_id)
        elif egress.line_open_proposal is not None:
            # No new proposal: the Line's open one is still current, so its
            # change still waits for review.
            proposals.append(egress.line_open_proposal.proposal_id)
    if proposals:
        return RunbookStepOutcome(
            **base, status="proposed", proposal_ids=tuple(dict.fromkeys(proposals))
        )
    return RunbookStepOutcome(**base, status="succeeded")


_STOPS: dict[str, RunbookStopReason] = {
    "failed": "step_failed",
    "proposed": "step_proposed",
    "not_enabled": "line_not_enabled",
    "unavailable": "target_unavailable",
}


def execute_runbook(
    accepted: AcceptedRunbook,
    *,
    mode: Literal["manual", "trigger"],
    run_line: LineStepRunner,
    fire_action: ActionStepFirer,
    clock: Callable[[], datetime],
    trigger: str | None = None,
    run_id: str | None = None,
) -> RunbookRunResult:
    """Run the steps in order; the first failure or review stops the rest."""

    from cruxible_client.contracts.primitives import new_id

    started = clock()
    outcomes: list[RunbookStepOutcome] = []
    stop: RunbookStopReason | None = None
    stopped_at: int | None = None
    steps = accepted.runbook.steps
    for step, target in enumerate(steps, start=1):
        if stop is not None:
            outcomes.append(RunbookStepOutcome(step=step, target=target, status="not_run"))
            continue
        if isinstance(target, ActionTarget):
            try:
                event = fire_action(target.action, step)
            except (CoreError, CruxibleError) as exc:
                outcome = RunbookStepOutcome(
                    step=step,
                    target=target,
                    status="failed",
                    code=_error_code(exc),
                    message=str(exc),
                )
            else:
                outcome = (
                    RunbookStepOutcome(
                        step=step,
                        target=target,
                        status=event.status,
                        code=event.code,
                        message=event.message,
                    )
                    if isinstance(event, LineStepNotRun)
                    else RunbookStepOutcome(
                        step=step, target=target, status="fired", event_id=event
                    )
                )
        else:
            try:
                state = run_line(target.line.target.name, step)
            except (CoreError, CruxibleError) as exc:
                outcome = RunbookStepOutcome(
                    step=step,
                    target=target,
                    status="failed",
                    code=_error_code(exc),
                    message=str(exc),
                )
            else:
                if isinstance(state, LineStepNotRun):
                    outcome = RunbookStepOutcome(
                        step=step,
                        target=target,
                        status=state.status,
                        code=state.code,
                        message=state.message,
                    )
                else:
                    outcome = _line_outcome(step, target, state)
        outcomes.append(outcome)
        if outcome.status in _STOPS:
            stop, stopped_at = _STOPS[outcome.status], step
    return RunbookRunResult(
        run_id=run_id or new_id("RBR", length=32),
        runbook=accepted.runbook.identity.name,
        runbook_artifact_digest=accepted.artifact_digest,
        mode=mode,
        trigger=trigger,
        started_at=started,
        finished_at=clock(),
        status="completed" if stop is None else "stopped",
        stop_reason=stop,
        stopped_at_step=stopped_at,
        steps=tuple(outcomes),
        attention_steps=runbook_attention_steps(tuple(outcomes)),
    )


def record_runbook_run(instance: PlaybillInstance, result: RunbookRunResult) -> None:
    """Retain the run record beside the Runbook's others: one insert."""

    with RUNBOOK_STATE.open(instance) as connection:
        assert connection is not None
        connection.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
            (
                result.run_id,
                result.runbook,
                result.finished_at.isoformat(),
                json.dumps(result.model_dump(mode="json"), sort_keys=True),
            ),
        )


def latest_runbook_run(instance: PlaybillInstance, name: str) -> RunbookRunResult | None:
    """The Runbook's most recent retained run record: one index seek."""

    with RUNBOOK_STATE.open(instance, create=False) as connection:
        if connection is None:
            return None
        row = connection.execute(
            "SELECT payload FROM runs WHERE runbook=? ORDER BY finished_at DESC LIMIT 1", (name,)
        ).fetchone()
    return None if row is None else RunbookRunResult.model_validate(json.loads(row[0]))


class _DaemonClock:
    def __init__(self, clock: Any | None) -> None:
        self._clock = clock

    def __call__(self) -> datetime:
        if self._clock is not None:
            now: datetime = self._clock.now()
            return now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return datetime.now(UTC)


def service_run_runbook(
    instance: PlaybillInstance,
    *,
    name: str,
    actor_context: GovernedActorContext,
    caller_rung: int,
    provider_runtime_operator: Any | None = None,
    workspace_file_reader: Any | None = None,
    daemon_clock: Any | None = None,
    evaluation_instant_skew: timedelta | None = None,
) -> RunbookRunResult:
    """Run the Runbook now under the caller's authority (``runbook run``).

    Every Line step is the caller's manual Line run, checked against the
    caller's tier per step exactly as ``line run`` checks it; an action step
    needs the tier ``action run`` needs. Any refusal is that step's failure.
    """

    instance.require_writable()
    from cruxible_core.runtime.permissions import (
        RUNTIME_OPERATION_PERMISSIONS,
    )
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from cruxible_core.triggers.journal import fire_manual

    accepted = accepted_runbook(instance, name)
    clock = _DaemonClock(daemon_clock)
    action_rung = RUNTIME_OPERATION_PERMISSIONS["cruxible_action_run"].value - 1

    def run_line(line: str, _step: int) -> ProcedureRunStateV2:
        return service_run_playbill_line(
            instance,
            path_identity_digest=line,
            request=LineRunRequest(line=line),
            actor_context=actor_context,
            caller_rung=caller_rung,
            provider_runtime_operator=provider_runtime_operator,
            workspace_file_reader=workspace_file_reader,
            daemon_clock=daemon_clock,
            evaluation_instant_skew=evaluation_instant_skew,
        )

    def fire_action(action: str, _step: int) -> int:
        if caller_rung < action_rung:
            from cruxible_core.errors import PermissionDeniedError
            from cruxible_core.runtime.permissions import PermissionMode

            raise PermissionDeniedError(
                "cruxible_action_run",
                PermissionMode(max(caller_rung + 1, 1)).name.lower(),
                PermissionMode(action_rung + 1).name.lower(),
            )
        return fire_manual(
            instance, action=action, principal=actor_context.actor_id, now=clock()
        ).sequence

    result = execute_runbook(
        accepted, mode="manual", run_line=run_line, fire_action=fire_action, clock=clock
    )
    record_runbook_run(instance, result)
    return result


__all__ = [
    "RUNBOOK_STATE",
    "LineStepNotRun",
    "RunbookNotFoundError",
    "accepted_runbook",
    "execute_runbook",
    "latest_runbook_run",
    "record_runbook_run",
    "service_run_runbook",
]
