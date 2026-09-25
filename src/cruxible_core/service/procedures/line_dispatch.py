"""Line matching, arming and dispatch.

An armed Line's daemon matches new trigger evidence forward-only and admits the
occurrences it matched under the arming credential; nothing it did not observe
while armed is ever run implicitly. Explicit evaluation and dispatch are the
only way to act on anything else.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from cruxible_client.contracts.errors import PlaybillError, PlaybillExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipalV1,
    LineArmStopReasonV1,
    LineArmV1,
    LineDispatchItemV1,
    LineDispatchRequestV1,
    LineDispatchResultV1,
    LineEvaluateRequestV1,
    LineTriggerCheckRequestV1,
    LineTriggerCheckResultV1,
    LineTriggerOccurrenceV1,
)
from cruxible_client.contracts.procedures.line_specs import (
    CadenceTriggerPolicyV1,
    line_identity_digest,
)
from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusalV1,
    ProcedureNodeRefusalV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import served_repair_for_refusal
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.procedures.line_admission import (
    LINE_ARM_ADMISSION_GATE,
    line_admission_guard,
    line_arm_boundary,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.line_triggers import service_check_line_trigger
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequestV1,
    _accepted_line_by_reference,
    _journal,
    _line_admissions,
    _stream,
    service_run_playbill_line,
)

_ARM_FIELDS = (
    "arm_id",
    "line",
    "line_id",
    "line_artifact_digest",
    "occurrence_epoch",
    "armed_at",
    "armed_by",
)
_EPOCH_CHANGED = "The Line's trigger epoch changed; rearm to match the new epoch."
_LINE_CHANGED = "The Line changed; rearm to run its new version automatically."

# Idle polls need not retain a record per tick. A crash may leave at most this
# checkpoint interval uncovered; restart never advances beyond durable coverage.
_IDLE_COVERAGE_INTERVAL = timedelta(minutes=1)


def _positions(instance: PlaybillInstance) -> dict[str, Any]:
    journal, _ = _journal(instance)
    return journal.index.positions(_stream(instance))


def _enqueue(
    store: LineDispatchStore,
    conn: Any,
    result: LineTriggerCheckResultV1,
    actor: GovernedActorContext,
    now: datetime,
    *,
    session_id: str | None = None,
) -> LineTriggerCheckResultV1:
    occurrences = []
    for occurrence in result.occurrences:
        pending = False
        disposition = "admitted"
        if occurrence.admitted_run_id is None:
            exists = conn.execute(
                "SELECT disposition FROM pending WHERE line_id=? AND epoch=? AND occurrence_id=?",
                (result.line_identity_digest, result.occurrence_epoch, occurrence.occurrence_id),
            ).fetchone()
            if exists is None:
                store.append(
                    conn,
                    "pending",
                    {
                        "line": result.line,
                        "line_identity_digest": result.line_identity_digest,
                        "line_artifact_digest": result.line_artifact_digest,
                        "occurrence_epoch": result.occurrence_epoch,
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
    request: LineEvaluateRequestV1,
    *,
    actor: GovernedActorContext,
    now: datetime,
) -> LineTriggerCheckResultV1:
    instance.require_writable()
    result = service_check_line_trigger(instance, line, request, now=now)
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        return _enqueue(store, conn, result, actor, now)


class LineArmAuthorityLost(PlaybillExecutionError):
    """The arming credential, scope or permission no longer holds; the arm stops."""

    def __init__(self, reason: LineArmStopReasonV1, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class LineArmSegmentEnded(PlaybillExecutionError):
    """The arm segment was disarmed, rearmed or stopped after its work was scheduled."""


def require_active_segment(instance: PlaybillInstance, session_id: str) -> None:
    """Refuse an automatic admission for a segment that is no longer the active arm."""

    store = LineDispatchStore(instance)
    with store.locked() as conn:
        row = conn.execute(
            "SELECT active FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
    if row is None or not row[0]:
        raise LineArmSegmentEnded("the arm that matched this work is no longer active")


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
) -> dict[str, Any]:
    """End a segment and open its successor in one transition.

    Two transitions could be interrupted between them, leaving an arm with no
    active segment that still reports itself armed. One record either lands
    whole or not at all.
    """

    stopped = dict(
        current,
        stops_at=current["evaluated_until"],
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
    """Lapse a cadence Line's pending ticks as a new arm segment opens.

    A cadence tick is not an event but "the Line is due", and the evaluator
    re-offers the first undispatched one, so a tick left pending by a restart,
    a disarm or explicit evaluation would hold every later tick back. The new
    segment's own ticks supersede it: it closes as `lapsed`, retained and still
    runnable with an explicit retry, and is never run implicitly.
    """

    if not isinstance(accepted.line.trigger_policy, CadenceTriggerPolicyV1):
        return
    line_id = line_identity_digest(accepted.line.identity)
    for (occurrence_id,) in conn.execute(
        "SELECT occurrence_id FROM pending WHERE line_id=? AND epoch=? AND disposition='pending'",
        (line_id, accepted.line.occurrence_epoch),
    ).fetchall():
        store.append(
            conn,
            "closed",
            dict(
                line_id=line_id,
                epoch=accepted.line.occurrence_epoch,
                occurrence_id=occurrence_id,
                detail=(
                    "A cadence tick due before this arm lapsed; retry it explicitly to run it."
                ),
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
    reason: LineArmStopReasonV1 | None,
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


def _arm_view(store: LineDispatchStore, conn: Any, data: dict[str, Any]) -> LineArmV1:
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
    return store.arm_view(data, pending_automatic=automatic, pending_explicit=total - automatic)


def service_arm_line(
    instance: PlaybillInstance,
    line: str,
    *,
    principal: LineArmPrincipalV1,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
) -> LineArmV1:
    """Arm the current Line version forward-only under the caller's credential.

    Arming never catches up: matching starts at `now`, and any work already
    pending stays for explicit dispatch. Rearming an armed Line rebinds it to
    this caller and the current version, again from `now`.
    """

    instance.require_writable()
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line
    )
    identity = line_identity_digest(accepted.line.identity)
    store = LineDispatchStore(instance)
    with line_arm_boundary(instance.root, identity), store.locked() as conn:
        current = _active_session(conn, identity)
        if current is not None:
            _stop(
                store,
                conn,
                current,
                reason="disarmed",
                detail="Rearmed; the new arm matches forward from its own start.",
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
            armed_at=format_datetime(now),
            armed_by=principal.model_dump(mode="json"),
        )
        data = _open_segment(
            store, conn, arm, instance=instance, actor=actor, now=now, daemon_id=daemon_id
        )
        return _arm_view(store, conn, data)


def service_disarm_line(
    instance: PlaybillInstance,
    line: str,
    *,
    actor: GovernedActorContext,
    now: datetime,
) -> LineArmV1:
    """Stop admitting new work; a run already admitted is not cancelled."""

    instance.require_writable()
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line
    )
    identity = line_identity_digest(accepted.line.identity)
    store = LineDispatchStore(instance)
    with line_arm_boundary(instance.root, identity), store.locked() as conn:
        current = _active_session(conn, identity)
        if current is None:
            raise PlaybillExecutionError("this Line is not armed")
        data = _stop(
            store, conn, current, reason="disarmed", detail="Disarmed.", actor=actor, now=now
        )
        return _arm_view(store, conn, data)


def service_line_arm_status(instance: PlaybillInstance, line: str) -> LineArmV1:
    """The Line's current arm, or the last one and why it stopped."""

    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line
    )
    identity = line_identity_digest(accepted.line.identity)
    if not dispatch_root(instance).exists():
        raise PlaybillExecutionError("this Line has never been armed")
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        row = conn.execute(
            "SELECT payload FROM sessions WHERE line_id=? ORDER BY rowid DESC LIMIT 1",
            (identity,),
        ).fetchone()
        if row is None:
            raise PlaybillExecutionError("this Line has never been armed")
        return _arm_view(store, conn, json.loads(row[0]))


def service_stop_line_arm(
    instance: PlaybillInstance,
    session_id: str,
    *,
    reason: LineArmStopReasonV1,
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


def stalled_line_arms(
    instance: PlaybillInstance, *, now: datetime, stall_after: timedelta
) -> tuple[LineArmV1, ...]:
    """Arms whose automation quietly stopped doing its job.

    An arm that stopped for any reason but a deliberate disarm, or an armed
    Line whose own due work has waited longer than `stall_after`, is reported.
    A disarmed Line, or one never armed, is not.
    """

    if not dispatch_root(instance).exists():
        return ()
    store = LineDispatchStore(instance)
    stalled: list[LineArmV1] = []
    with store.locked() as conn:
        latest = conn.execute(
            "SELECT s.payload FROM sessions s WHERE s.rowid = "
            "(SELECT max(rowid) FROM sessions WHERE line_id=s.line_id)"
        ).fetchall()
        for (payload,) in latest:
            data = json.loads(payload)
            if data["stops_at"] is not None:
                if data.get("stop_reason") not in {None, "disarmed"}:
                    stalled.append(_arm_view(store, conn, data))
                continue
            oldest = conn.execute(
                "SELECT min(eligible_at) FROM pending WHERE session_id=? AND disposition='pending'",
                (data["session_id"],),
            ).fetchone()[0]
            due = None if oldest is None else parse_datetime(oldest)
            if due is not None and due <= now - stall_after:
                stalled.append(_arm_view(store, conn, data))
    return tuple(sorted(stalled, key=lambda item: item.line.encode("utf-8")))


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
            accepted = _accepted_line_by_reference(
                instance, coordinate=instance.accepted_coordinate(), reference=session["line"]
            )
        except (PlaybillError, OSError, ValueError) as exc:
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
        stop: tuple[LineArmStopReasonV1, str] | None = None
        if accepted.line.occurrence_epoch != session["occurrence_epoch"]:
            stop = ("epoch_changed", _EPOCH_CHANGED)
        elif accepted.artifact_digest != session["line_artifact_digest"]:
            # The arm is pinned to the version it was armed under: a changed
            # Line is never adopted implicitly, even within the same epoch.
            stop = ("line_changed", _LINE_CHANGED)
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
                )
            continue
        with line_arm_boundary(instance.root, session["line_id"]), store.locked() as conn:
            current = conn.execute(
                "SELECT active,payload FROM sessions WHERE session_id=?", (session["session_id"],)
            ).fetchone()
            if current is None or not current[0]:
                continue
            session = json.loads(current[1])
            if accepted.line.occurrence_epoch != session["occurrence_epoch"]:
                _stop(
                    store,
                    conn,
                    session,
                    reason="epoch_changed",
                    detail=_EPOCH_CHANGED,
                    actor=actor,
                    now=now,
                )
                continue
            evaluated_until = parse_datetime(session["evaluated_until"])
            assert evaluated_until is not None
            if now <= evaluated_until:
                continue
            scan = session.get("scan") or {
                "until": format_datetime(now + timedelta(microseconds=1)),
                "through": _positions(instance),
                "cursor": None,
            }
            is_cadence = isinstance(accepted.line.trigger_policy, CadenceTriggerPolicyV1)
            # One outstanding cadence tick per arm segment: work explicit
            # evaluation recorded, or an earlier segment left, never holds the
            # arm's own ticks back.
            if (
                is_cadence
                and conn.execute(
                    "SELECT 1 FROM pending WHERE session_id=? AND disposition='pending' LIMIT 1",
                    (session["session_id"],),
                ).fetchone()
            ):
                continue
            request = LineTriggerCheckRequestV1(
                since=parse_datetime(session["evaluated_until"])
                if is_cadence
                or accepted.line.trigger_policy.kind == "window_close"
                and getattr(accepted.line.trigger_policy, "window").kind == "fixed"
                else None,
                until=parse_datetime(scan["until"]),
                cursor=scan["cursor"],
                limit=256,
            )
            result = service_check_line_trigger(
                instance,
                session["line"],
                request,
                now=now,
                after=session["positions"],
                through=scan["through"],
                include_future_windows=request.since is None,
                pending_scope=session["session_id"],
            )
            if result.occurrence_epoch != session["occurrence_epoch"]:
                # Acceptance may advance while the evaluator opens its snapshot.
                # Arming the old epoch never arms the caller for a new one.
                _stop(
                    store,
                    conn,
                    session,
                    reason="epoch_changed",
                    detail=_EPOCH_CHANGED,
                    actor=actor,
                    now=now,
                )
                continue
            if result.line_artifact_digest != session["line_artifact_digest"]:
                # A same-epoch revision accepted while this check ran: what it
                # matched belongs to a version the arm is not bound to.
                _stop(
                    store,
                    conn,
                    session,
                    reason="line_changed",
                    detail=_LINE_CHANGED,
                    actor=actor,
                    now=now,
                )
                continue
            _enqueue(store, conn, result, actor, now, session_id=session["session_id"])
            if (
                result.status != "incomplete"
                and session.get("scan") is None
                and scan["through"] == session["positions"]
                and result.detail == session.get("detail")
                and now - evaluated_until < _IDLE_COVERAGE_INTERVAL
            ):
                # Pending transitions have already landed independently. Time-only
                # progress can wait; event progress and partial scans cannot.
                continue
            session["detail"] = result.detail
            if result.status != "incomplete":
                session.update(evaluated_until=scan["until"], positions=scan["through"], scan=None)
            else:
                scan["cursor"] = result.cursor
                session["scan"] = scan
            store.append(conn, "coverage", session, actor=actor, now=now)


def service_dispatch_line(
    instance: PlaybillInstance,
    line: str,
    request: LineDispatchRequestV1,
    *,
    actor: GovernedActorContext,
    now: datetime,
    caller_rung: int,
    provider_runtime_operator: Any = None,
    workspace_file_reader: Any = None,
    session_id: str | None = None,
    pinned_line_artifact_digest: str | None = None,
    before_admission: Callable[[], tuple[GovernedActorContext, int]] | None = None,
) -> LineDispatchResultV1:
    """Admit pending occurrences, explicitly or for one armed segment.

    `session_id` limits dispatch to the work that armed segment matched itself,
    under the Line version it is pinned to. `before_admission` runs immediately
    before each admission and returns the actor and caller rung that admission
    uses -- authority is re-resolved every time, never carried over -- or
    raises :class:`LineArmAuthorityLost`, leaving the occurrence pending. The
    admission record itself is ordered against a disarm.
    """

    instance.require_writable()
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line
    )
    identity, epoch = line_identity_digest(accepted.line.identity), accepted.line.occurrence_epoch
    if not dispatch_root(instance).exists():
        return LineDispatchResultV1()
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
        rows = conn.execute(
            sql + " ORDER BY eligible_at,occurrence_id LIMIT ?", (*args, request.limit)
        ).fetchall()
    results = []
    for row in rows:
        data = json.loads(row[0])
        epoch = data["occurrence_epoch"]
        occurrence = LineTriggerOccurrenceV1.model_validate(data["occurrence"])
        if occurrence.eligible_at > now:
            results.append(
                LineDispatchItemV1(
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
            refusal: ProcedureAdmissionRefusalV1 | ProcedureNodeRefusalV1 | None = None
            status: Literal["blocked", "rejected", "superseded"] = "blocked"
            prior = next(
                iter(_line_admissions(instance, accepted, occurrence_id=occurrence.occurrence_id)),
                None,
            )
            run_id, detail = (prior.run_id if prior else None), None
            if prior is None:
                current = _accepted_line_by_reference(
                    instance, coordinate=instance.accepted_coordinate(), reference=line
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
                if current.artifact_digest != data["line_artifact_digest"]:
                    refusal = ProcedureAdmissionRefusalV1(
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
                            request=LineRunRequestV1(
                                line=line,
                                occurrence_id=occurrence.occurrence_id,
                                trigger_event=binding.event if binding else None,
                            ),
                            actor_context=run_actor,
                            caller_rung=run_rung,
                            provider_runtime_operator=provider_runtime_operator,
                            workspace_file_reader=workspace_file_reader,
                            daemon_clock=SimpleNamespace(now=lambda: now),
                            occurrence_basis_time=occurrence.eligible_at,
                            expected_line_artifact_digest=data["line_artifact_digest"],
                            explicit_occurrence=session_id is None,
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
                                (ProcedureAdmissionRefusalV1, ProcedureNodeRefusalV1),
                            ):
                                refusal = result.terminal
                                detail = refusal.message
                                if refusal.code in {
                                    "trigger_capture_stale",
                                    "trigger_capture_over_budget",
                                    "trigger_capture_unavailable",
                                    "trigger_capture_forbidden",
                                    "trigger_capture_invalid",
                                }:
                                    status = "rejected"
                                elif refusal.code == "line_binding_superseded":
                                    status = "superseded"
                                elif (
                                    refusal.code == "occurrence_id_mismatch"
                                    and session_id is not None
                                ):
                                    # An intervening admission moved the cadence
                                    # chain past this tick. It stays retryable;
                                    # closing it lets the arm match the next one.
                                    status = "superseded"
                            else:
                                detail = "No durable admission was produced."
                    except LineArmSegmentEnded:
                        raise
                    except PlaybillExecutionError as exc:
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
                LineDispatchItemV1(
                    occurrence_id=occurrence.occurrence_id,
                    status="admitted" if run_id else status,
                    run_id=run_id,
                    detail=detail,
                    refusal=refusal,
                )
            )
    return LineDispatchResultV1(items=tuple(results))
