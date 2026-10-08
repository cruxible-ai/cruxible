"""Line matching, enablement and dispatch.

An enabled Line's daemon matches new evidence for the Triggers aimed at the
Line forward-only and admits the occurrences it matched under the enabling
credential; nothing it did not observe while enabled is ever run implicitly.
A Trigger aimed at a Line that is not enabled does nothing. The enablement is
pinned to the Line version and the exact Trigger versions it was enabled
under: a change to either stops it, never adopted implicitly, and retiring the
Line stops it too. Explicit evaluation and dispatch are the only way to act on
anything else (``line_attention`` names what they owe after a restart).

The dispatch store's records keep their internal ``arm`` names.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import CruxibleError, ExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineDispatchItem,
    LineDispatchRequest,
    LineDispatchResult,
    LineEnablement,
    LineEnablementOutcome,
    LineEnablementPrincipal,
    LineEnablementStopReason,
    LineEvaluateRequest,
    LineEvaluateResult,
    LineTriggerOccurrence,
)
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusal,
    ProcedureNodeRefusal,
)
from cruxible_client.contracts.procedures.windows import TIMED_BINDING_KINDS, FixedWindow
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import served_repair_for_refusal
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_client.contracts.triggers import (
    AcceptedTrigger,
    GenerationAcceptedSchedule,
    WindowCloseSchedule,
    schedule_is_timed,
)
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.procedures.line_admission import (
    LINE_ARM_ADMISSION_GATE,
    line_admission_guard,
    line_arm_boundary,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import change_scope
from cruxible_core.service.procedures.line_triggers import (
    TriggerRange,
    service_check_line_trigger,
)
from cruxible_core.service.procedures.procedure_runs import (
    LineNeverEnabled,
    LineRunRequest,
    LineTriggersChanged,
    LineVersionChanged,
    TriggerFire,
    _accepted_line_by_reference,
    _journal,
    _line_admissions,
    _stream,
    line_run_target_rung,
    line_trigger_pins,
    line_triggers,
    require_line_mandate,
    require_run_permission,
    service_run_playbill_line,
)
from cruxible_core.storage.preview_fence import is_previewing

_ARM_FIELDS = (
    "arm_id",
    "line",
    "line_id",
    "line_artifact_digest",
    "occurrence_epoch",
    "trigger_pins",
    "armed_at",
    "armed_by",
)
_EPOCH_CHANGED = "The Line's trigger epoch changed; enable it again to match the new epoch."
_LINE_CHANGED = "The Line changed; enable it again to run its new version automatically."
_TRIGGER_CHANGED = (
    "The Triggers aimed at this Line changed; enable it again to run under the current ones."
)

# Idle polls need not retain a record per tick. A crash may leave at most this
# checkpoint interval uncovered; restart never advances beyond durable coverage.
_IDLE_COVERAGE_INTERVAL = timedelta(minutes=1)


def _positions(instance: PlaybillInstance) -> dict[str, Any]:
    root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    if is_previewing() and not root.is_dir():
        # No run was ever journaled, so there is no position to start from, and
        # a preview must not create the journal to read that it is empty.
        return {}
    journal, _ = _journal(instance)
    return journal.index.positions(_stream(instance))


def _enqueue(
    store: LineDispatchStore,
    conn: Any,
    result: LineEvaluateResult,
    actor: GovernedActorContext,
    now: datetime,
    *,
    session_id: str | None = None,
) -> LineEvaluateResult:
    occurrences = []
    trigger_digests = {item.trigger: item.artifact_digest for item in result.triggers}
    for occurrence in result.occurrences:
        pending = False
        disposition = "admitted"
        if occurrence.admitted_run_id is None:
            exists = conn.execute(
                "SELECT disposition,payload FROM pending "
                "WHERE line_id=? AND epoch=? AND occurrence_id=?",
                (result.line_identity_digest, result.occurrence_epoch, occurrence.occurrence_id),
            ).fetchone()
            trigger = None if occurrence.binding is None else occurrence.binding.trigger.qualified
            trigger_digest = trigger_digests.get(trigger or "")
            if exists is not None and exists[0] == "superseded" and trigger is not None:
                stored = json.loads(exists[1])
                if stored.get("trigger_artifact_digest") != trigger_digest:
                    # The occurrence was closed, never admitted, because its Trigger
                    # changed. A current Trigger that derives it again rebinds it;
                    # only an admission is final.
                    store.append(
                        conn,
                        "reconciled",
                        dict(
                            stored,
                            trigger=trigger,
                            trigger_artifact_digest=trigger_digest,
                            occurrence=occurrence.model_dump(mode="json"),
                            session_id=session_id,
                        ),
                        actor=actor,
                        now=now,
                    )
                    exists = ("pending", exists[1])
            if exists is None:
                store.append(
                    conn,
                    "pending",
                    {
                        "line": result.line,
                        "line_identity_digest": result.line_identity_digest,
                        "line_artifact_digest": result.line_artifact_digest,
                        "occurrence_epoch": result.occurrence_epoch,
                        "trigger": trigger,
                        "trigger_artifact_digest": trigger_digest,
                        "coordinate": result.coordinate.model_dump(mode="json"),
                        "occurrence": occurrence.model_dump(mode="json"),
                        "session_id": session_id,
                    },
                    actor=actor,
                    now=now,
                )
            pending = exists is None or exists[0] == "pending"
            disposition = exists[0] if exists else "pending"
        occurrences.append(
            occurrence.model_copy(update={"pending": pending, "dispatch_status": disposition})
        )
    return result.model_copy(update={"occurrences": tuple(occurrences)})


def service_evaluate_line(
    instance: PlaybillInstance,
    line: str,
    request: LineEvaluateRequest,
    *,
    actor: GovernedActorContext | None,
    now: datetime,
) -> LineEvaluateResult:
    """Evaluate a Line's live Triggers over one range; never runs anything.

    A dry run only reads what the range makes eligible. Otherwise every
    occurrence found is enqueued for explicit dispatch, and a range evaluated
    to its end is recorded, so a restart gap it covers leaves ``next``.
    """

    window = TriggerRange(
        since=request.since, until=request.until, cursor=request.cursor, limit=request.limit
    )
    if request.dry_run:
        return service_check_line_trigger(instance, line, window, now=now, enqueue=False)
    if actor is None:
        raise ExecutionError("evaluation that enqueues requires an actor")
    return service_enqueue_line_range(instance, line, window, actor=actor, now=now)


def service_enqueue_line_range(
    instance: PlaybillInstance,
    line: str,
    window: TriggerRange,
    *,
    actor: GovernedActorContext,
    now: datetime,
) -> LineEvaluateResult:
    """Enqueue what one range of a Line's Triggers makes eligible, and record the range."""

    instance.require_writable()
    result = service_check_line_trigger(instance, line, window, now=now, enqueue=True)
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        enqueued = _enqueue(store, conn, result, actor, now)
        if result.status != "incomplete" and result.checked_since is not None:
            store.append(
                conn,
                "evaluated",
                dict(
                    line_id=result.line_identity_digest,
                    epoch=result.occurrence_epoch,
                    since=format_datetime(result.checked_since),
                    until=format_datetime(result.checked_until),
                ),
                actor=actor,
                now=now,
            )
        return enqueued


class LineArmAuthorityLost(ExecutionError):
    """The arming credential, scope or permission no longer holds; the arm stops."""

    def __init__(self, reason: LineEnablementStopReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class LineArmSegmentEnded(ExecutionError):
    """The arm segment was disarmed, rearmed or stopped after its work was scheduled."""


def require_active_segment(instance: PlaybillInstance, session_id: str) -> None:
    """Refuse an automatic admission for a segment that is no longer the active arm."""

    store = LineDispatchStore(instance)
    with store.locked() as conn:
        row = conn.execute(
            "SELECT active FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if row is None or not row[0]:
        raise LineArmSegmentEnded("the enablement that matched this work is no longer active")


@contextmanager
def _segment_gate(instance: PlaybillInstance, line_id: str, session_id: str) -> Iterator[None]:
    """Hold the arm boundary while the admission is recorded, if the arm still stands."""

    with line_arm_boundary(instance.root, line_id):
        require_active_segment(instance, session_id)
        yield


def _open_segment(
    store: LineDispatchStore,
    conn: Any,
    arm: dict[str, Any],
    *,
    instance: PlaybillInstance,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
) -> dict[str, Any]:
    """Start one forward-only matching segment of an arm at `now`."""

    data = _segment(arm, instance=instance, now=now, daemon_id=daemon_id)
    store.append(conn, "session", data, actor=actor, now=now)
    return data


def _accepted_sequence(instance: PlaybillInstance) -> int:
    with instance.accepted_history_reader() as history:
        return history.sequence


def _segment(
    arm: dict[str, Any], *, instance: PlaybillInstance, now: datetime, daemon_id: str
) -> dict[str, Any]:
    return dict(
        arm,
        session_id=uuid4().hex,
        starts_at=format_datetime(now),
        stops_at=None,
        stop_reason=None,
        evaluated_until=format_datetime(now),
        positions=_positions(instance),
        generation_start=_accepted_sequence(instance),
        daemon_id=daemon_id,
        detail=None,
        scan=None,
    )


def _roll_over(
    store: LineDispatchStore,
    conn: Any,
    current: dict[str, Any],
    *,
    instance: PlaybillInstance,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
    timed: Iterable[str],
) -> dict[str, Any]:
    """End a segment and open its successor in one transition.

    Two transitions could be interrupted between them, leaving an arm with no
    active segment that still reports itself armed. One record either lands
    whole or not at all.

    The segment stops where all its matching had reached: a timed Trigger
    (`timed`) whose own matching lags the segment's coverage (its tick held
    pending, or ticks it still owed) is behind it, so the restart's gap names
    those ticks too. One with no recorded reach is read from the segment's
    start, as its matching is (`_segment_request`).
    """

    reached = current.get("trigger_until", {})
    stops_at = min(
        (
            current["evaluated_until"],
            *(reached.get(name, current["starts_at"]) for name in timed),
        ),
        key=_instant,
    )
    stopped = dict(
        current,
        stops_at=stops_at,
        stop_reason=None,
        detail="Daemon restarted; uncovered ranges require explicit evaluation.",
    )
    opened = _segment(
        {key: current[key] for key in _ARM_FIELDS}, instance=instance, now=now, daemon_id=daemon_id
    )
    store.append(conn, "rollover", {"stopped": stopped, "opened": opened}, actor=actor, now=now)
    return opened


def _lapse_cadence_backlog(
    store: LineDispatchStore,
    conn: Any,
    accepted: Any,
    *,
    actor: GovernedActorContext,
    now: datetime,
) -> None:
    """Lapse a Line's pending cadence and cron ticks as a new arm segment opens.

    A tick is not an event but "the Trigger is due", and the evaluator
    re-offers the first undispatched one, so a tick left pending by a restart,
    a disarm or explicit evaluation would hold every later tick of its Trigger
    back. The new segment's own ticks supersede it: it closes as `lapsed`,
    retained and still runnable with an explicit retry, and is never run
    implicitly.
    """

    line_id = line_identity_digest(accepted.line.identity)
    for occurrence_id, payload in conn.execute(
        "SELECT occurrence_id,payload FROM pending WHERE line_id=? AND epoch=? "
        "AND disposition='pending'",
        (line_id, accepted.line.occurrence_epoch),
    ).fetchall():
        binding = json.loads(payload)["occurrence"].get("binding")
        if binding is None or binding.get("kind") not in TIMED_BINDING_KINDS:
            continue
        store.append(
            conn,
            "closed",
            dict(
                line_id=line_id,
                epoch=accepted.line.occurrence_epoch,
                occurrence_id=occurrence_id,
                detail=("A tick due before this enablement lapsed; retry it explicitly to run it."),
                status="lapsed",
                refusal=None,
            ),
            actor=actor,
            now=now,
        )


def _stop(
    store: LineDispatchStore,
    conn: Any,
    session: dict[str, Any],
    *,
    reason: LineEnablementStopReason | None,
    detail: str,
    actor: GovernedActorContext,
    now: datetime,
) -> dict[str, Any]:
    """End a segment. With a reason the arm stops; without one a new segment follows."""

    session.update(stops_at=session["evaluated_until"], stop_reason=reason, detail=detail)
    store.append(conn, "stop", session, actor=actor, now=now)
    return session


def _active_session(conn: Any, line_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT payload FROM sessions WHERE line_id=? AND active=1", (line_id,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def _arm_view(
    store: LineDispatchStore,
    conn: Any,
    data: dict[str, Any],
    *,
    outcome: LineEnablementOutcome | None = None,
    coordinate: AcceptedCoordinate | None = None,
) -> LineEnablement:
    active = data["stops_at"] is None
    automatic = (
        conn.execute(
            "SELECT count(*) FROM pending WHERE session_id=? AND disposition='pending'",
            (data["session_id"],),
        ).fetchone()[0]
        if active
        else 0
    )
    total = conn.execute(
        "SELECT count(*) FROM pending WHERE line_id=? AND disposition='pending'",
        (data["line_id"],),
    ).fetchone()[0]
    view = store.arm_view(data, pending_automatic=automatic, pending_explicit=total - automatic)
    if outcome is None:
        return view
    return view.model_copy(update={"outcome": outcome, "coordinate": coordinate})


def service_enable_line(
    instance: PlaybillInstance,
    line: str,
    *,
    principal: LineEnablementPrincipal,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> LineEnablement:
    """Arm the current Line version forward-only under the caller's credential.

    ``dry_run`` previews the arm on this same path and records nothing; its
    outcome reads ``would_enable`` or ``would_reenable`` (R12).

    The arm matches the live Triggers aimed at the Line now, pinned to their
    exact versions. Arming never catches up: matching starts at `now`, and any
    work already pending stays for explicit dispatch. A Line that can propose
    or settle refuses to arm (`cruxible.line.mandate_required`) while no
    current mandate covers its Procedure. Arming a Line already armed by this
    caller, at the current version, epoch and Triggers, on this daemon changes
    nothing and reports `already_enabled`. Rearming with any of those different
    rebinds it to this caller and the current versions, again from `now`.
    """

    instance.require_writable()
    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.line.enable",
        describe=f"enabling Line {line}",
    ) as mode:
        return _previewed(
            mode.previewing,
            _arm_line(
                instance,
                line,
                principal=principal,
                actor=actor,
                now=now,
                daemon_id=daemon_id,
                committing=mode.committing,
            ),
        )


def _previewed(previewing: bool, view: LineEnablement) -> LineEnablement:
    """A preview's arm view: the state the commit would leave, outcome ``would_*``."""

    if not previewing or view.outcome not in _WOULD_OUTCOMES:
        return view
    return view.model_copy(update={"outcome": _WOULD_OUTCOMES[view.outcome]})


_WOULD_OUTCOMES: dict[str | None, LineEnablementOutcome] = {
    "enabled": "would_enable",
    "reenabled": "would_reenable",
    "disabled": "would_disable",
}


def _arm_line(
    instance: PlaybillInstance,
    line: str,
    *,
    principal: LineEnablementPrincipal,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
    committing: Callable[[], AbstractContextManager[None]],
) -> LineEnablement:
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_line_by_reference(instance, coordinate=coordinate, reference=line)
    evaluated = AcceptedCoordinate.from_internal(coordinate)
    # An arm admits on its own; one whose every admission would refuse for want
    # of a mandate is a silent stall, so it refuses here instead.
    require_line_mandate(instance, accepted, coordinate=coordinate, now=now)
    trigger_pins = line_trigger_pins(line_triggers(instance, accepted, coordinate=coordinate))
    identity = line_identity_digest(accepted.line.identity)
    store = LineDispatchStore(instance)
    # The Line was read at `coordinate`. A commit pinned to a preview's
    # coordinate confirms the LIVE accepted head under the activation lock and
    # arms while holding it, so no Line revision can be accepted in between.
    # Lock order: the Line's arm boundary, then the activation lock, then the
    # dispatch store -- the order an automatic admission nests them in.
    with line_arm_boundary(instance.root, identity), committing(), store.locked() as conn:
        current = _active_session(conn, identity)
        if current is not None and (
            current["armed_by"] == principal.model_dump(mode="json")
            and current["line_artifact_digest"] == accepted.artifact_digest
            and current["occurrence_epoch"] == accepted.line.occurrence_epoch
            and current.get("trigger_pins") == trigger_pins
            and current["daemon_id"] == daemon_id
        ):
            return _arm_view(store, conn, current, outcome="already_enabled", coordinate=evaluated)
        if current is not None:
            _stop(
                store,
                conn,
                current,
                reason="disabled",
                detail="Enabled again; the new enablement matches forward from its own start.",
                actor=actor,
                now=now,
            )
        _lapse_cadence_backlog(store, conn, accepted, actor=actor, now=now)
        arm = dict(
            arm_id=uuid4().hex,
            line=accepted.line.identity.qualified,
            line_id=identity,
            line_artifact_digest=accepted.artifact_digest,
            occurrence_epoch=accepted.line.occurrence_epoch,
            trigger_pins=trigger_pins,
            armed_at=format_datetime(now),
            armed_by=principal.model_dump(mode="json"),
        )
        data = _open_segment(
            store, conn, arm, instance=instance, actor=actor, now=now, daemon_id=daemon_id
        )
        return _arm_view(
            store,
            conn,
            data,
            outcome="enabled" if current is None else "reenabled",
            coordinate=evaluated,
        )


def service_disable_line(
    instance: PlaybillInstance,
    line: str,
    *,
    actor: GovernedActorContext,
    now: datetime,
    dry_run: bool | None = None,
    at: str | None = None,
) -> LineEnablement:
    """Stop admitting new work; a run already admitted is not cancelled.

    Disarming a Line whose arm already stopped changes nothing and returns that
    arm with `already_disabled`. A Line never armed has no arm to return.
    ``dry_run`` previews it and records nothing (outcome ``would_disable``).
    """

    instance.require_writable()
    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.line.disable",
        describe=f"disabling Line {line}",
    ) as mode:
        return _previewed(
            mode.previewing,
            _disarm_line(instance, line, actor=actor, now=now, committing=mode.committing),
        )


def _disarm_line(
    instance: PlaybillInstance,
    line: str,
    *,
    actor: GovernedActorContext,
    now: datetime,
    committing: Callable[[], AbstractContextManager[None]],
) -> LineEnablement:
    coordinate = instance.accepted_coordinate()
    # A retired Line can still be disabled: its enablement may not have been
    # stopped by the listener yet.
    accepted = _accepted_line_by_reference(
        instance, coordinate=coordinate, reference=line, allow_retired=True
    )
    evaluated = AcceptedCoordinate.from_internal(coordinate)
    identity = line_identity_digest(accepted.line.identity)
    store = LineDispatchStore(instance)
    with line_arm_boundary(instance.root, identity), committing(), store.locked() as conn:
        current = _active_session(conn, identity)
        if current is None:
            last = conn.execute(
                "SELECT payload FROM sessions WHERE line_id=? ORDER BY rowid DESC LIMIT 1",
                (identity,),
            ).fetchone()
            if last is None:
                raise LineNeverEnabled(accepted.line.identity.name)
            return _arm_view(
                store,
                conn,
                json.loads(last[0]),
                outcome="already_disabled",
                coordinate=evaluated,
            )
        data = _stop(
            store, conn, current, reason="disabled", detail="Disabled.", actor=actor, now=now
        )
        return _arm_view(store, conn, data, outcome="disabled", coordinate=evaluated)


def service_stop_line_arm(
    instance: PlaybillInstance,
    session_id: str,
    *,
    reason: LineEnablementStopReason,
    detail: str,
    actor: GovernedActorContext,
    now: datetime,
) -> None:
    """Stop one arm segment the daemon found no longer authorized."""

    instance.require_writable()
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        found = conn.execute(
            "SELECT line_id FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if found is None:
        return
    with line_arm_boundary(instance.root, found[0]), store.locked() as conn:
        row = conn.execute(
            "SELECT active,payload FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is not None and row[0]:
            _stop(
                store, conn, json.loads(row[1]), reason=reason, detail=detail, actor=actor, now=now
            )


def line_arm_health(
    instance: PlaybillInstance, *, now: datetime, stall_after: timedelta
) -> tuple[tuple[Literal["running", "stalled", "stopped"], LineEnablement], ...]:
    """Every Line's latest arm segment, and whether its automation is doing its job.

    An arm that stopped for any reason but a deliberate disarm is `stopped`; an
    armed Line whose own due work has waited longer than `stall_after` is
    `stalled`; any other armed Line is `running`. A disarmed Line, or one never
    armed, is not reported.
    """

    if not dispatch_root(instance).exists():
        return ()
    store = LineDispatchStore(instance)
    arms: list[tuple[Literal["running", "stalled", "stopped"], LineEnablement]] = []
    with store.locked() as conn:
        latest = conn.execute(
            "SELECT s.payload FROM sessions s WHERE s.rowid = "
            "(SELECT max(rowid) FROM sessions WHERE line_id=s.line_id)"
        ).fetchall()
        for (payload,) in latest:
            data = json.loads(payload)
            if data["stops_at"] is not None:
                if data.get("stop_reason") not in {None, "disabled"}:
                    arms.append(("stopped", _arm_view(store, conn, data)))
                continue
            oldest = conn.execute(
                "SELECT min(eligible_at) FROM pending WHERE session_id=? AND disposition='pending'",
                (data["session_id"],),
            ).fetchone()[0]
            due = None if oldest is None else parse_datetime(oldest)
            stalled = due is not None and due <= now - stall_after
            arms.append(("stalled" if stalled else "running", _arm_view(store, conn, data)))
    return tuple(sorted(arms, key=lambda item: item[1].line.encode("utf-8")))


def armed_work(instance: PlaybillInstance, *, now: datetime) -> tuple[dict[str, Any], ...]:
    """Active arm segments with work they matched themselves that is now due."""

    if not dispatch_root(instance).exists():
        return ()
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        return tuple(
            json.loads(row[0])
            for row in conn.execute(
                "SELECT s.payload FROM sessions s WHERE s.active=1 AND EXISTS ("
                "SELECT 1 FROM pending p WHERE p.session_id=s.session_id "
                "AND p.disposition='pending' AND p.eligible_at<=?)",
                (format_datetime(now),),
            ).fetchall()
        )


def _arm_stop(
    session: dict[str, Any],
    *,
    occurrence_epoch: int,
    line_artifact_digest: str,
    trigger_pins: dict[str, str],
) -> tuple[LineEnablementStopReason, str] | None:
    """Why an arm no longer matches what is accepted, or None while it still does."""

    if occurrence_epoch != session["occurrence_epoch"]:
        return ("epoch_changed", _EPOCH_CHANGED)
    if line_artifact_digest != session["line_artifact_digest"]:
        # The arm is pinned to the version it was armed under: a changed
        # Line is never adopted implicitly, even within the same epoch.
        return ("line_changed", _LINE_CHANGED)
    if trigger_pins != session.get("trigger_pins"):
        # A Trigger added, changed or retired is held to the same rule as the
        # Line: what the arm runs on is never adopted implicitly.
        return ("trigger_changed", _TRIGGER_CHANGED)
    return None


def _timed(trigger: AcceptedTrigger) -> bool:
    """Whether a Trigger fires by time (ticks, fixed windows) rather than on events."""

    schedule = trigger.trigger.schedule
    return (
        schedule_is_timed(schedule)
        or isinstance(schedule, GenerationAcceptedSchedule)
        or (isinstance(schedule, WindowCloseSchedule) and isinstance(schedule.window, FixedWindow))
    )


def _segment_request(
    trigger: AcceptedTrigger, session: dict[str, Any], scan: dict[str, Any]
) -> TriggerRange:
    """One Trigger's forward-only range: ticks and fixed windows by time, events by position.

    A timed Trigger resumes from the instant its own matching last covered, so a
    cadence tick held pending keeps its chain where it was while event Triggers
    on the same Line move on.
    """

    name = trigger.trigger.identity.qualified
    return TriggerRange(
        since=(
            parse_datetime(session["starts_at"])
            if isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule)
            else (
                parse_datetime(session.get("trigger_until", {}).get(name, session["starts_at"]))
                if _timed(trigger)
                and not isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule)
                else None
            )
        ),
        until=parse_datetime(scan["until"]),
        cursor=None
        if isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule)
        else scan["cursors"].get(name),
        limit=256,
    )


def service_match_listening_lines(
    instance: PlaybillInstance, *, actor: GovernedActorContext, now: datetime, daemon_id: str
) -> None:
    instance.require_writable()
    if not dispatch_root(instance).exists():
        return
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        sessions = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM sessions WHERE active=1").fetchall()
        ]
    for session in sessions:
        try:
            coordinate = instance.accepted_coordinate()
            accepted = _accepted_line_by_reference(
                instance, coordinate=coordinate, reference=session["line"], allow_retired=True
            )
            if accepted.line.lifecycle.state == "retired":
                _stop_retired(instance, store, session, actor=actor, now=now)
                continue
            triggers = line_triggers(instance, accepted, coordinate=coordinate)
        except (CruxibleError, OSError, ValueError) as exc:
            # One unavailable Line cannot starve the other subscriptions. No
            # progress is claimed; the retained status explains the uncovered range.
            with store.locked() as conn:
                current = conn.execute(
                    "SELECT active,payload FROM sessions WHERE session_id=?",
                    (session["session_id"],),
                ).fetchone()
                if current is not None and current[0]:
                    session = json.loads(current[1])
                    if session.get("detail") != str(exc):
                        session["detail"] = str(exc)
                        store.append(conn, "coverage", session, actor=actor, now=now)
            continue
        pins = line_trigger_pins(triggers)
        stop: tuple[LineEnablementStopReason, str] | None
        stop = _arm_stop(
            session,
            occurrence_epoch=accepted.line.occurrence_epoch,
            line_artifact_digest=accepted.artifact_digest,
            trigger_pins=pins,
        )
        if stop is not None:
            with line_arm_boundary(instance.root, session["line_id"]), store.locked() as conn:
                current = _active_session(conn, session["line_id"])
                if current is not None and current["session_id"] == session["session_id"]:
                    _stop(
                        store, conn, current, reason=stop[0], detail=stop[1], actor=actor, now=now
                    )
            continue
        if (
            session["daemon_id"] != daemon_id
            or session["positions"]["generation"] != _positions(instance)["generation"]
        ):
            # The arm survives a restart forward-only: a new segment starts now,
            # and what this segment matched stays pending for explicit dispatch
            # (a cadence tick lapses instead; see `_lapse_cadence_backlog`).
            with line_arm_boundary(instance.root, session["line_id"]), store.locked() as conn:
                current = _active_session(conn, session["line_id"])
                if current is None or current["session_id"] != session["session_id"]:
                    continue
                # Lapsing first leaves nothing to recover: interrupted here, the
                # old segment still stands and the next pass rolls it over.
                _lapse_cadence_backlog(store, conn, accepted, actor=actor, now=now)
                _roll_over(
                    store,
                    conn,
                    current,
                    instance=instance,
                    actor=actor,
                    now=now,
                    daemon_id=daemon_id,
                    timed=(
                        trigger.trigger.identity.qualified
                        for trigger in triggers
                        if _timed(trigger)
                    ),
                )
            continue
        with line_arm_boundary(instance.root, session["line_id"]), store.locked() as conn:
            current = conn.execute(
                "SELECT active,payload FROM sessions WHERE session_id=?", (session["session_id"],)
            ).fetchone()
            if current is None or not current[0]:
                continue
            session = json.loads(current[1])
            stop = _arm_stop(
                session,
                occurrence_epoch=accepted.line.occurrence_epoch,
                line_artifact_digest=accepted.artifact_digest,
                trigger_pins=pins,
            )
            if stop is not None:
                _stop(store, conn, session, reason=stop[0], detail=stop[1], actor=actor, now=now)
                continue
            evaluated_until = parse_datetime(session["evaluated_until"])
            assert evaluated_until is not None
            if now <= evaluated_until:
                continue
            previous_scan = session.get("scan")
            previous_cursors = {} if previous_scan is None else previous_scan["cursors"]
            continuing_scan = previous_scan is not None and "until" in previous_scan
            scan = (
                previous_scan
                if continuing_scan
                else {
                    "until": format_datetime(now + timedelta(microseconds=1)),
                    "through": _positions(instance),
                    "cursors": dict(previous_cursors),
                    "done": [],
                    "timed": {},
                }
            )
            details: list[str] = []
            stopped = False
            for trigger in triggers:
                name = trigger.trigger.identity.qualified
                if name in scan["done"]:
                    continue
                # One outstanding tick per timed Trigger per arm segment: work
                # explicit evaluation recorded, or an earlier segment left, never
                # holds the arm's own ticks back.
                if (
                    schedule_is_timed(trigger.trigger.schedule)
                    or isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule)
                ) and conn.execute(
                    "SELECT 1 FROM pending WHERE session_id=? AND trigger_id=? "
                    "AND disposition='pending' LIMIT 1",
                    (session["session_id"], name),
                ).fetchone():
                    scan["done"].append(name)
                    continue
                request = _segment_request(trigger, session, scan)
                result = service_check_line_trigger(
                    instance,
                    session["line"],
                    request,
                    now=now,
                    after=session["positions"],
                    through=scan["through"],
                    include_future_windows=request.since is None,
                    pending_scope=session["session_id"],
                    only_trigger=name,
                    generation_after=session.get("generation_start"),
                    generation_cursors=scan["cursors"],
                    enqueue=True,
                )
                stop = _arm_stop(
                    session,
                    occurrence_epoch=result.occurrence_epoch,
                    line_artifact_digest=result.line_artifact_digest,
                    # A check reads only this Trigger; the rest of the pinned set
                    # stands unless the check saw this one change or go.
                    trigger_pins={
                        **{key: value for key, value in pins.items() if key != name},
                        **{item.trigger: item.artifact_digest for item in result.triggers},
                    },
                )
                if stop is not None:
                    # Acceptance may advance while the evaluator opens its
                    # snapshot: what it matched belongs to versions the arm is
                    # not bound to.
                    _stop(
                        store, conn, session, reason=stop[0], detail=stop[1], actor=actor, now=now
                    )
                    stopped = True
                    break
                _enqueue(store, conn, result, actor, now, session_id=session["session_id"])
                if result.detail is not None:
                    details.append(result.detail)
                if result.status == "incomplete":
                    if not isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule):
                        scan["cursors"][name] = result.cursor
                else:
                    if not isinstance(trigger.trigger.schedule, GenerationAcceptedSchedule):
                        scan["cursors"].pop(name, None)
                    scan["done"].append(name)
                    if _timed(trigger):
                        # How far this Trigger's own matching reached: short of
                        # the scan for a cadence or cron Trigger that still owes
                        # a tick there, which it never passes undelivered.
                        scan["timed"][name] = format_datetime(result.checked_until)
            if stopped:
                continue
            complete = all(
                trigger.trigger.identity.qualified in scan["done"] for trigger in triggers
            )
            detail = details[0] if details else None
            if (
                complete
                and not continuing_scan
                and scan["cursors"] == previous_cursors
                and scan["through"] == session["positions"]
                and detail == session.get("detail")
                and scan["timed"].keys() <= session.get("trigger_until", {}).keys()
                and now - evaluated_until < _IDLE_COVERAGE_INTERVAL
            ):
                # Pending transitions have already landed independently. Time-only
                # progress can wait (a Trigger's recorded reach only lags, which
                # is conservative); event progress, partial scans and a timed
                # Trigger's first reach cannot.
                continue
            session["detail"] = detail
            if complete:
                session.update(
                    evaluated_until=scan["until"],
                    positions=scan["through"],
                    trigger_until={
                        **session.get("trigger_until", {}),
                        **scan["timed"],
                    },
                    # Completed scans keep generation coverage across ticks;
                    # pagination cursors have already been cleared above.
                    scan={"cursors": scan["cursors"]} if scan["cursors"] else None,
                )
            else:
                session["scan"] = scan
            store.append(conn, "coverage", session, actor=actor, now=now)


_LINE_RETIRED = "The Line was retired; its enablement stopped and its pending work closed."


def _stop_retired(
    instance: PlaybillInstance,
    store: LineDispatchStore,
    session: dict[str, Any],
    *,
    actor: GovernedActorContext,
    now: datetime,
) -> None:
    """Stop a retired Line's enablement and close every occurrence still pending for it."""

    with line_arm_boundary(instance.root, session["line_id"]), store.locked() as conn:
        current = _active_session(conn, session["line_id"])
        if current is None or current["session_id"] != session["session_id"]:
            return
        for epoch, occurrence_id in conn.execute(
            "SELECT epoch,occurrence_id FROM pending WHERE line_id=? AND disposition='pending'",
            (session["line_id"],),
        ).fetchall():
            store.append(
                conn,
                "closed",
                dict(
                    line_id=session["line_id"],
                    epoch=epoch,
                    occurrence_id=occurrence_id,
                    detail=_LINE_RETIRED,
                    status="superseded",
                    refusal=None,
                ),
                actor=actor,
                now=now,
            )
        _stop(
            store, conn, current, reason="line_retired", detail=_LINE_RETIRED, actor=actor, now=now
        )


@dataclass(frozen=True)
class LineCoverageGap:
    """A range an enabled Line's daemon never matched: it was down, and nothing evaluated it."""

    line: str
    line_id: str
    since: datetime
    until: datetime


@dataclass(frozen=True)
class LinePendingWork:
    """Occurrences an enabled Line matched that wait for explicit dispatch, and are due."""

    line: str
    line_id: str
    due: int
    oldest_eligible_at: datetime


def _instant(text: str) -> datetime:
    value = parse_datetime(text)
    assert value is not None
    return value


def _uncovered(
    since: datetime, until: datetime, covered: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """The parts of [since, until) no evaluated range covers."""

    remaining = [(since, until)]
    for start, end in covered:
        remaining = [
            part
            for low, high in remaining
            for part in ((low, min(high, start)), (max(low, end), high))
            if part[0] < part[1]
        ]
    return remaining


def line_attention(
    instance: PlaybillInstance, *, now: datetime
) -> tuple[tuple[LineCoverageGap, ...], tuple[LinePendingWork, ...]]:
    """What enabled Lines owe explicit work after a restart, for ``next``.

    A restart rolls an enabled Line forward-only: the range between the last
    instant its daemon matched and the restart is a coverage gap until an
    explicit ``line evaluate`` covers it, and work it matched before the
    restart waits for ``line dispatch``. Only Lines enabled now are read (a
    disabled or stopped Line owes nothing automatic), but a range any earlier
    enablement of the Line in this epoch left uncovered stays listed until an
    evaluation covers it.
    """

    if not dispatch_root(instance).exists():
        return (), ()
    gaps: list[LineCoverageGap] = []
    pending: list[LinePendingWork] = []
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        active = [
            json.loads(row[0])
            for row in conn.execute("SELECT payload FROM sessions WHERE active=1").fetchall()
        ]
        for session in sorted(active, key=lambda item: item["line"].encode("utf-8")):
            # Every enablement of this Line in this epoch, not only the current
            # one: re-enabling (another credential, or after a disable) does not
            # evaluate a range an earlier enablement's restart left uncovered.
            # Gaps lie between consecutive segments of one enablement, so each
            # keeps its own boundaries; a deliberate stop is never a gap.
            by_enablement: dict[str, list[dict[str, Any]]] = {}
            for (payload,) in conn.execute(
                "SELECT payload FROM sessions WHERE line_id=? AND epoch=?",
                (session["line_id"], session["occurrence_epoch"]),
            ).fetchall():
                data = json.loads(payload)
                by_enablement.setdefault(data["arm_id"], []).append(data)
            covered = sorted(
                (_instant(since), _instant(until))
                for since, until in conn.execute(
                    "SELECT since,until FROM evaluated WHERE line_id=? AND epoch=?",
                    (session["line_id"], session["occurrence_epoch"]),
                ).fetchall()
            )
            line_gaps: list[LineCoverageGap] = []
            for segments in by_enablement.values():
                segments.sort(key=lambda data: data["starts_at"])
                for before, after in zip(segments, segments[1:], strict=False):
                    if before["stops_at"] is None or before.get("stop_reason") is not None:
                        continue
                    line_gaps.extend(
                        LineCoverageGap(session["line"], session["line_id"], since, until)
                        for since, until in _uncovered(
                            _instant(before["stops_at"]), _instant(after["starts_at"]), covered
                        )
                    )
            gaps.extend(sorted(line_gaps, key=lambda gap: (gap.since, gap.until)))
            due, oldest = conn.execute(
                "SELECT count(*),min(eligible_at) FROM pending WHERE line_id=? "
                "AND disposition='pending' AND eligible_at<=? "
                "AND (session_id IS NULL OR session_id!=?)",
                (session["line_id"], format_datetime(now), session["session_id"]),
            ).fetchone()
            if due:
                pending.append(
                    LinePendingWork(session["line"], session["line_id"], due, _instant(oldest))
                )
    return tuple(gaps), tuple(pending)


def service_dispatch_line(
    instance: PlaybillInstance,
    line: str,
    request: LineDispatchRequest,
    *,
    actor: GovernedActorContext,
    now: datetime,
    caller_rung: int,
    provider_runtime_operator: Any = None,
    workspace_file_reader: Any = None,
    session_id: str | None = None,
    pinned_line_artifact_digest: str | None = None,
    pinned_trigger_pins: dict[str, str] | None = None,
    before_admission: Callable[[], tuple[GovernedActorContext, int]] | None = None,
) -> LineDispatchResult:
    """Admit pending occurrences, explicitly or for one armed segment.

    `session_id` limits dispatch to the work that armed segment matched itself,
    under the Line and Trigger versions it is pinned to. An occurrence whose
    Trigger changed or no longer aims at the Line is superseded: it was matched
    under a schedule that no longer holds. `before_admission` runs immediately
    before each admission and returns the actor and caller rung that admission
    uses -- authority is re-resolved every time, never carried over -- or
    raises :class:`LineArmAuthorityLost`, leaving the occurrence pending. The
    admission record itself is ordered against a disarm.
    """

    instance.require_writable()
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_line_by_reference(instance, coordinate=coordinate, reference=line)
    # Dispatch runs what the Line can do, so it needs the tier those runs need.
    require_run_permission(
        "cruxible_line_dispatch",
        target_rung=line_run_target_rung(instance, line),
        caller_rung=caller_rung,
    )
    identity, epoch = line_identity_digest(accepted.line.identity), accepted.line.occurrence_epoch
    if not dispatch_root(instance).exists():
        return LineDispatchResult()
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        sql = "SELECT payload FROM pending WHERE line_id=?"
        args: list[Any] = [identity]
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        if not request.retry:
            sql += " AND disposition='pending'"
        if request.occurrence_id:
            sql += " AND occurrence_id=?"
            args.append(request.occurrence_id)
        if request.cursor is not None:
            after = _dispatch_cursor_key(request.cursor)
            sql += " AND (eligible_at>? OR (eligible_at=? AND occurrence_id>?))"
            args.extend((after[0], after[0], after[1]))
        rows = conn.execute(
            sql + " ORDER BY eligible_at,occurrence_id LIMIT ?", (*args, request.limit)
        ).fetchall()
    # Keyset over (eligible_at, occurrence_id): a page continues past every row
    # it attempted, so a row that stays blocked is reported once per drain.
    page_cursor = None
    if len(rows) == request.limit and rows:
        last = json.loads(rows[-1][0])
        page_cursor = _dispatch_cursor(
            str(format_datetime(_instant(last["occurrence"]["eligible_at"]))),
            last["occurrence"]["occurrence_id"],
        )
    results = []
    for row in rows:
        data = json.loads(row[0])
        epoch = data["occurrence_epoch"]
        occurrence = LineTriggerOccurrence.model_validate(data["occurrence"])
        if occurrence.eligible_at > now:
            results.append(
                LineDispatchItem(
                    occurrence_id=occurrence.occurrence_id,
                    status="pending",
                    detail="The bound window has not closed.",
                )
            )
            continue
        with line_admission_guard(instance.root, identity):
            # Another dispatcher may have closed this row after our initial selection.
            with store.locked() as conn:
                latest = conn.execute(
                    "SELECT disposition,payload FROM pending WHERE line_id=? AND epoch=? "
                    "AND occurrence_id=?",
                    (identity, epoch, occurrence.occurrence_id),
                ).fetchone()
                disposition, data = latest[0], json.loads(latest[1])
            if disposition != "pending" and not request.retry:
                continue
            refusal: ProcedureAdmissionRefusal | ProcedureNodeRefusal | None = None
            status: Literal["blocked", "rejected", "superseded"] = "blocked"
            prior = next(
                iter(_line_admissions(instance, accepted, occurrence_id=occurrence.occurrence_id)),
                None,
            )
            run_id, detail = (prior.run_id if prior else None), None
            if prior is None:
                current_coordinate = instance.accepted_coordinate()
                current = _accepted_line_by_reference(
                    instance, coordinate=current_coordinate, reference=line
                )
                current_triggers = line_triggers(instance, current, coordinate=current_coordinate)
                trigger = next(
                    (
                        item
                        for item in current_triggers
                        if item.trigger.identity.qualified == data.get("trigger")
                    ),
                    None,
                )
                if request.retry and current.line.occurrence_epoch == epoch:
                    # The exact event/window is unchanged. Only an explicit retry
                    # may bind a successor Line; retain that decision before admission.
                    data.update(
                        line_artifact_digest=current.artifact_digest,
                        coordinate=AcceptedCoordinate.from_internal(
                            instance.accepted_coordinate()
                        ).model_dump(mode="json"),
                    )
                    with store.locked() as conn:
                        store.append(conn, "reconciled", data, actor=actor, now=now)
                if pinned_line_artifact_digest is not None and (
                    current.artifact_digest != pinned_line_artifact_digest
                    or data["line_artifact_digest"] != pinned_line_artifact_digest
                ):
                    # Automatic dispatch runs only the version it was armed under;
                    # the occurrence stays pending for an explicit decision.
                    raise LineArmAuthorityLost("line_changed", _LINE_CHANGED)
                if (
                    pinned_trigger_pins is not None
                    and line_trigger_pins(current_triggers) != pinned_trigger_pins
                ):
                    raise LineArmAuthorityLost("trigger_changed", _TRIGGER_CHANGED)
                if data.get("trigger") is not None and (
                    trigger is None or trigger.artifact_digest != data["trigger_artifact_digest"]
                ):
                    refusal = ProcedureAdmissionRefusal(
                        code="line_binding_superseded",
                        repair=served_repair_for_refusal("line_binding_superseded"),
                        message=(
                            "The pending occurrence was matched by a Trigger that has since "
                            "changed or no longer aims at this Line; evaluate the Line's "
                            "current Triggers."
                        ),
                    )
                    status = "superseded"
                    detail = refusal.message
                elif current.artifact_digest != data["line_artifact_digest"]:
                    refusal = ProcedureAdmissionRefusal(
                        code="line_binding_superseded",
                        repair=served_repair_for_refusal("line_binding_superseded"),
                        message=(
                            "The pending occurrence binds a superseded Line. Explicit "
                            "retry is required within the same epoch; evaluate a changed "
                            "epoch separately."
                        ),
                    )
                    status = "superseded"
                    detail = refusal.message
                else:
                    binding = occurrence.binding
                    run_actor, run_rung = (
                        (actor, caller_rung) if before_admission is None else before_admission()
                    )
                    gate = (
                        None
                        if session_id is None
                        else LINE_ARM_ADMISSION_GATE.set(
                            partial(_segment_gate, instance, identity, session_id)
                        )
                    )
                    try:
                        result = service_run_playbill_line(
                            instance,
                            path_identity_digest=line,
                            request=LineRunRequest(
                                line=line, occurrence_id=occurrence.occurrence_id
                            ),
                            trigger_fire=None
                            if binding is None
                            else TriggerFire(
                                trigger=binding.trigger.name,
                                event=binding.event,
                                generation=binding.generation,
                            ),
                            actor_context=run_actor,
                            caller_rung=run_rung,
                            provider_runtime_operator=provider_runtime_operator,
                            workspace_file_reader=workspace_file_reader,
                            daemon_clock=SimpleNamespace(now=lambda: now),
                            occurrence_basis_time=occurrence.eligible_at,
                            expected_line_artifact_digest=data["line_artifact_digest"],
                            expected_trigger_artifact_digest=data.get("trigger_artifact_digest"),
                            expected_trigger_pins=pinned_trigger_pins,
                        )
                        # The journal, not the execution response, establishes admission.
                        admitted = next(
                            iter(
                                _line_admissions(
                                    instance, current, occurrence_id=occurrence.occurrence_id
                                )
                            ),
                            None,
                        )
                        run_id = admitted.run_id if admitted else None
                        if run_id is None:
                            if isinstance(
                                result.terminal,
                                (ProcedureAdmissionRefusal, ProcedureNodeRefusal),
                            ):
                                refusal = result.terminal
                                detail = refusal.message
                                if refusal.code in {
                                    "trigger_event_precedes_acceptance",
                                    "trigger_capture_stale",
                                    "trigger_capture_over_budget",
                                    "trigger_capture_unavailable",
                                    "trigger_capture_forbidden",
                                    "trigger_capture_invalid",
                                }:
                                    status = "rejected"
                                elif refusal.code == "line_binding_superseded":
                                    status = "superseded"
                            else:
                                detail = "No durable admission was produced."
                    except LineArmSegmentEnded:
                        raise
                    except LineTriggersChanged as exc:
                        raise LineArmAuthorityLost("trigger_changed", _TRIGGER_CHANGED) from exc
                    except LineVersionChanged as exc:
                        if pinned_line_artifact_digest is None:
                            # An explicit dispatch keeps the occurrence pending; the
                            # next pass reconciles it against the current Line.
                            detail = str(exc)
                        else:
                            raise LineArmAuthorityLost("line_changed", _LINE_CHANGED) from exc
                    except ExecutionError as exc:
                        detail = str(exc)
                    finally:
                        if gate is not None:
                            LINE_ARM_ADMISSION_GATE.reset(gate)
            with store.locked() as conn:
                if run_id is not None:
                    store.append(
                        conn,
                        "admitted",
                        dict(
                            line_id=identity,
                            epoch=epoch,
                            occurrence_id=occurrence.occurrence_id,
                            run_id=run_id,
                        ),
                        actor=actor,
                        now=now,
                    )
                else:
                    store.append(
                        conn,
                        "closed" if status in {"rejected", "superseded"} else "dispatch_refused",
                        dict(
                            line_id=identity,
                            epoch=epoch,
                            occurrence_id=occurrence.occurrence_id,
                            detail=detail,
                            status=status,
                            refusal=refusal.model_dump(mode="json") if refusal else None,
                        ),
                        actor=actor,
                        now=now,
                    )
            results.append(
                LineDispatchItem(
                    occurrence_id=occurrence.occurrence_id,
                    status="admitted" if run_id else status,
                    run_id=run_id,
                    detail=detail,
                    refusal=refusal,
                )
            )
    return LineDispatchResult(items=tuple(results), cursor=page_cursor)


def _dispatch_cursor(eligible_at: str, occurrence_id: str) -> str:
    return base64.urlsafe_b64encode(
        canonical_bytes({"eligible_at": eligible_at, "occurrence_id": occurrence_id})
    ).decode("ascii")


def _dispatch_cursor_key(cursor: str) -> tuple[str, str]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        key = (decoded["eligible_at"], decoded["occurrence_id"])
        if not all(isinstance(part, str) for part in key) or set(decoded) != {
            "eligible_at",
            "occurrence_id",
        }:
            raise ValueError("cursor fields")
    except (ValueError, KeyError, TypeError) as exc:
        raise ExecutionError("dispatch cursor must be one a previous dispatch returned") from exc
    return key
