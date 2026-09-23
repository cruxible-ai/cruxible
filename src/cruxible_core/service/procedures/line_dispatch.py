"""Explicit matching and dispatch; the background listener never executes code."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from cruxible_client.contracts.errors import PlaybillError, PlaybillExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineDispatchItemV1,
    LineDispatchRequestV1,
    LineDispatchResultV1,
    LineEvaluateRequestV1,
    LineListeningSessionV1,
    LineListenRequestV1,
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
from cruxible_core.procedures.line_admission import line_admission_guard
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


def service_listen_line(
    instance: PlaybillInstance,
    line: str,
    request: LineListenRequestV1,
    *,
    actor: GovernedActorContext,
    now: datetime,
    daemon_id: str,
) -> LineListeningSessionV1:
    instance.require_writable()
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line
    )
    identity, epoch = line_identity_digest(accepted.line.identity), accepted.line.occurrence_epoch
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        row = conn.execute(
            "SELECT payload FROM sessions WHERE line_id=? AND epoch=? AND active=1",
            (identity, epoch),
        ).fetchone()
        data = json.loads(row[0]) if row else None
        position = _positions(instance)
        same_reader = (
            data is not None
            and data["daemon_id"] == daemon_id
            and data["positions"]["generation"] == position["generation"]
        )
        if data is not None and request.action == "start" and same_reader:
            return store.session_view(data)
        if data is not None:
            # A restart never claims the unobserved suffix of the old session.
            data["stops_at"] = data["evaluated_until"]
            data["detail"] = "Listener stopped; uncovered ranges require explicit evaluation."
            store.append(conn, "stop", data, actor=actor, now=now)
        if request.action == "stop":
            if data is None:
                raise PlaybillExecutionError("this Line has no active listening session")
            return store.session_view(data)
        data = dict(
            session_id=uuid4().hex,
            line=accepted.line.identity.qualified,
            line_id=identity,
            occurrence_epoch=epoch,
            starts_at=format_datetime(now),
            stops_at=None,
            evaluated_until=format_datetime(now),
            positions=position,
            daemon_id=daemon_id,
            detail=None,
            scan=None,
        )
        store.append(conn, "session", data, actor=actor, now=now)
        return store.session_view(data)


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
        if accepted.line.occurrence_epoch != session["occurrence_epoch"]:
            with store.locked() as conn:
                session.update(
                    stops_at=session["evaluated_until"],
                    detail="Trigger epoch changed; start a new subscription explicitly.",
                )
                store.append(conn, "stop", session, actor=actor, now=now)
            continue
        if (
            session["daemon_id"] != daemon_id
            or session["positions"]["generation"] != _positions(instance)["generation"]
        ):
            # Resume forward with a new range; only already-pending work survives.
            service_listen_line(
                instance,
                session["line"],
                LineListenRequestV1(action="start"),
                actor=actor,
                now=now,
                daemon_id=daemon_id,
            )
            continue
        with store.locked() as conn:
            current = conn.execute(
                "SELECT active,payload FROM sessions WHERE session_id=?", (session["session_id"],)
            ).fetchone()
            if current is None or not current[0]:
                continue
            session = json.loads(current[1])
            if accepted.line.occurrence_epoch != session["occurrence_epoch"]:
                session.update(
                    stops_at=session["evaluated_until"],
                    detail="Trigger epoch changed; start a new subscription explicitly.",
                )
                store.append(conn, "stop", session, actor=actor, now=now)
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
            if (
                is_cadence
                and conn.execute(
                    "SELECT 1 FROM pending WHERE line_id=? AND epoch=? AND "
                    "disposition='pending' LIMIT 1",
                    (session["line_id"], session["occurrence_epoch"]),
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
            )
            if result.occurrence_epoch != session["occurrence_epoch"]:
                # Acceptance may advance while the evaluator opens its snapshot.
                # Enabling the old epoch never subscribes the caller to a new one.
                session.update(
                    stops_at=session["evaluated_until"],
                    detail="Trigger epoch changed; start a new subscription explicitly.",
                )
                store.append(conn, "stop", session, actor=actor, now=now)
                continue
            _enqueue(store, conn, result, actor, now)
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
) -> LineDispatchResultV1:
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
                    try:
                        result = service_run_playbill_line(
                            instance,
                            path_identity_digest=line,
                            request=LineRunRequestV1(
                                line=line,
                                occurrence_id=occurrence.occurrence_id,
                                trigger_event=binding.event if binding else None,
                            ),
                            actor_context=actor,
                            caller_rung=caller_rung,
                            provider_runtime_operator=provider_runtime_operator,
                            workspace_file_reader=workspace_file_reader,
                            daemon_clock=SimpleNamespace(now=lambda: now),
                            occurrence_basis_time=occurrence.eligible_at,
                            expected_line_artifact_digest=data["line_artifact_digest"],
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
                                    "trigger_capture_unavailable",
                                    "trigger_capture_forbidden",
                                    "trigger_capture_invalid",
                                }:
                                    status = "rejected"
                                elif refusal.code == "line_binding_superseded":
                                    status = "superseded"
                            else:
                                detail = "No durable admission was produced."
                    except PlaybillExecutionError as exc:
                        detail = str(exc)
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
