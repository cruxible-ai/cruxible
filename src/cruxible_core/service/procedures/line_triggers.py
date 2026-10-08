"""One indexed evaluator for explicit checks, listening, and historical work.

A Line's occurrences come from the live Trigger artifacts aimed at it at the
coordinate checked, each evaluated on its own schedule; a Line no Trigger aims
at has no automatic occurrences and runs only when run explicitly.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import CruxibleError, ExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineEvaluateResult,
    LineTriggerOccurrence,
    LineTriggerVersion,
)
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.procedures.windows import (
    TIMED_BINDING_KINDS,
    CaptureEventWindow,
    LineTriggerBinding,
    TriggerEventReference,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc, format_datetime, parse_datetime
from cruxible_client.contracts.triggers import (
    AcceptedTrigger,
    CaptureLandingSchedule,
    GenerationAcceptedSchedule,
    WindowCloseSchedule,
    schedule_is_timed,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.procedure_runs import (
    _accepted_line_by_reference,
    _journal,
    _line_admissions,
    _line_occurrence,
    _stream,
    _trigger_admissions,
    line_trigger_pins,
    line_triggers,
    trigger_accepted_at,
    trigger_binding_for,
)
from cruxible_core.service.procedures.resolution_contracts import bind_window, capture_event_time
from cruxible_core.triggers.cadence import timer_instants

#: The cursor position of a timed Trigger's page: the last tick it listed.
_TICK_POSITION = "tick"


@dataclass(frozen=True)
class TriggerRange:
    """One range to evaluate a Line's Triggers over, and the page of it to read."""

    since: datetime | None
    until: datetime | None
    cursor: str | None = None
    limit: int = 256


def _delivered_generations(
    instance: PlaybillInstance,
    accepted: Any,
    trigger: AcceptedTrigger,
    *,
    identity: str,
) -> frozenset[int]:
    """Every generation an occurrence of this Trigger was already delivered on.

    The dispatch store keeps every occurrence the Line's listener matched or an
    evaluation enqueued, whatever became of it; the journal keeps admissions.
    """

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root

    delivered: set[int] = set()
    for admission in _trigger_admissions(instance, accepted, trigger.trigger.identity):
        cause = getattr(admission, "trigger_binding", None)
        if cause is not None and cause.generation is not None:
            delivered.add(int(cause.generation))
    if dispatch_root(instance).exists():
        with LineDispatchStore(instance).locked() as conn:
            for (payload,) in conn.execute(
                "SELECT payload FROM pending WHERE line_id=? AND trigger_id=?",
                (identity, trigger.trigger.identity.qualified),
            ).fetchall():
                binding = json.loads(payload)["occurrence"].get("binding") or {}
                if isinstance(binding.get("generation"), int):
                    delivered.add(binding["generation"])
    return frozenset(delivered)


def _delivered_ticks(
    instance: PlaybillInstance, *, identity: str, epoch: int, trigger: str
) -> frozenset[str]:
    """Every occurrence of one timed Trigger already delivered to this Line.

    The dispatch store keeps every tick a listener matched or an evaluation
    enqueued, whatever became of it (pending, admitted, lapsed or closed).
    """

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root

    if not dispatch_root(instance).exists():
        return frozenset()
    with LineDispatchStore(instance).locked() as conn:
        return frozenset(
            row[0]
            for row in conn.execute(
                "SELECT occurrence_id FROM pending WHERE line_id=? AND epoch=? AND trigger_id=?",
                (identity, epoch, trigger),
            ).fetchall()
        )


def _tick_after(position: list[Any]) -> datetime:
    """The last tick a timed Trigger's earlier page listed: the next page starts after it."""

    tick = position[1] if position[0] == _TICK_POSITION else None
    instant = parse_datetime(tick) if isinstance(tick, str) else None
    if instant is None:
        raise ExecutionError("trigger cursor must retain its original Line, Triggers and range")
    return instant


def service_check_line_trigger(
    instance: PlaybillInstance,
    line: str,
    request: TriggerRange,
    *,
    now: datetime,
    after: dict[str, Any] | None = None,
    through: dict[str, Any] | None = None,
    include_future_windows: bool = False,
    pending_scope: str | None = None,
    only_trigger: str | None = None,
    generation_after: int | None = None,
    generation_cursors: dict[str, Any] | None = None,
) -> LineEvaluateResult:
    """Every occurrence the Line's live Triggers make eligible in one range.

    Triggers are evaluated in identity order. A page that stops inside one
    Trigger's Capture range or ticks resumes there, with the cursor naming that
    Trigger; `only_trigger` limits the check to one Trigger, as an armed segment
    does. A live listening pass (`pending_scope`) or a check with no `since`
    reads a cadence or cron Trigger's next tick in its chain; an explicit range
    reads every tick in it, which is how time a restart skipped is recovered.
    """

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root

    now = ensure_utc(now)
    until = min(request.until or now + timedelta(microseconds=1), now + timedelta(microseconds=1))
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_line_by_reference(instance, coordinate=coordinate, reference=line)
    triggers = tuple(
        item
        for item in line_triggers(instance, accepted, coordinate=coordinate)
        if only_trigger is None or item.trigger.identity.qualified == only_trigger
    )
    identity = line_identity_digest(accepted.line.identity)
    context: dict[str, Any] = dict(
        line=accepted.line.identity.qualified,
        line_identity_digest=identity,
        line_artifact_digest=accepted.artifact_digest,
        occurrence_epoch=accepted.line.occurrence_epoch,
        triggers=tuple(
            LineTriggerVersion(
                trigger=item.trigger.identity.qualified, artifact_digest=item.artifact_digest
            )
            for item in triggers
        ),
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        checked_since=request.since,
        checked_until=until,
    )
    scope = [
        identity,
        accepted.line.occurrence_epoch,
        line_trigger_pins(triggers),
        request.since.isoformat() if request.since else None,
        until.isoformat(),
        after,
        through,
    ]
    resume: tuple[str, list[Any] | None] | None = None
    if request.cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(request.cursor))
            if decoded["scope"] != scope:
                raise ValueError("cursor range, Line or Triggers changed")
            position = decoded["position"]
            if position is not None and (len(position) != 3 or not isinstance(position[2], int)):
                raise ValueError("invalid cursor position")
            resume = (decoded["trigger"], position)
        except (ValueError, KeyError, TypeError) as exc:
            raise ExecutionError(
                "trigger cursor must retain its original Line, Triggers and range"
            ) from exc
    if not triggers:
        return LineEvaluateResult(
            **context,
            status="not_met",
            detail=(
                "No live Trigger aims at this Line; it runs only when run explicitly. "
                "Accept a Trigger aimed at it and enable the Line to run it automatically."
            ),
        )

    def cursor_at(trigger: AcceptedTrigger, position: list[Any] | None) -> str:
        return base64.urlsafe_b64encode(
            canonical_bytes(
                {
                    "scope": scope,
                    "trigger": trigger.trigger.identity.qualified,
                    "position": position,
                }
            )
        ).decode()

    occurrences = []
    next_cursor = None
    complete = True
    generation_updates: dict[str, int] = {}
    try:
        bindings: list[tuple[AcceptedTrigger, LineTriggerBinding, datetime]] = []
        for trigger in triggers:
            name = trigger.trigger.identity.qualified
            if resume is not None and name.encode("utf-8") < resume[0].encode("utf-8"):
                continue
            position = resume[1] if resume is not None and name == resume[0] else None
            cursor = None if position is None else (position[0], position[1], position[2])
            schedule = trigger.trigger.schedule
            window = schedule.window if isinstance(schedule, WindowCloseSchedule) else None
            selector = (
                schedule.event
                if isinstance(schedule, CaptureLandingSchedule)
                else (window.event if isinstance(window, CaptureEventWindow) else None)
            )
            if selector is not None:
                remaining = request.limit - len(bindings)
                if remaining <= 0:
                    next_cursor = cursor_at(trigger, None)
                    break
                delay = (
                    timedelta(seconds=window.duration_seconds)
                    if window is not None
                    else timedelta(0)
                )
                journal, _ = _journal(instance)
                records, next_position, trigger_complete = journal.index.captures(
                    _stream(instance),
                    bodies=instance.body_store(),
                    contract_digest=selector.capture_contract_digest,
                    since=request.since - delay if request.since else None,
                    until=until if include_future_windows else until - delay,
                    limit=remaining,
                    cursor=cursor,
                    after=after,
                    through=through,
                )
                complete = complete and trigger_complete
                for stored in records:
                    record = stored.record
                    event = TriggerEventReference(
                        run_id=record.run_id or "",
                        partition_id=record.partition_id,
                        sequence=record.sequence,
                        record_digest=stored.record_digest,
                    )
                    event_time = capture_event_time(instance, selector, event, now=now)
                    if event_time <= trigger_accepted_at(instance, trigger):
                        # No Trigger fires retroactively: an event at or before its
                        # version's acceptance is not one it fires on, however it
                        # reached this range (a position scan ignores time bounds).
                        continue
                    if window is None:
                        bindings.append(
                            (trigger, trigger_binding_for(trigger, event=event), event_time)
                        )
                    else:
                        bound = bind_window(instance, window, event, now=now)
                        bindings.append(
                            (trigger, trigger_binding_for(trigger, window=bound), bound.ends_at)
                        )
                if next_position:
                    next_cursor = cursor_at(trigger, list(next_position))
                    break
            elif window is not None:
                bound = bind_window(instance, window, None, now=now)
                if bound.ends_at > trigger_accepted_at(instance, trigger):
                    bindings.append(
                        (trigger, trigger_binding_for(trigger, window=bound), bound.ends_at)
                    )
            elif isinstance(schedule, GenerationAcceptedSchedule):
                from cruxible_core.triggers.journal import generation_at, trigger_generation

                with instance.accepted_history_reader() as history:
                    head = history.sequence
                if generation_after is None and generation_cursors is None:
                    # An explicit or historical evaluation reads [since, until),
                    # `since` inclusive: accepts strictly before `since` are
                    # outside it, an accept exactly at `since` is inside. The
                    # latest generation accepted before `until` stands for every
                    # accept in the range (coalesced) and is eligible at its own
                    # acceptance instant. It is new unless an occurrence on that
                    # exact generation was already delivered (matched or
                    # evaluated before); anything delivered later than the range
                    # is another occurrence and never stands in for this one.
                    floor = trigger_generation(instance, trigger.trigger)
                    if request.since is not None:
                        floor = max(
                            floor,
                            generation_at(instance, request.since - timedelta(microseconds=1)),
                        )
                    head = min(head, generation_at(instance, until - timedelta(microseconds=1)))
                    generation_updates[name] = head
                    if head > floor and head not in _delivered_generations(
                        instance, accepted, trigger, identity=identity
                    ):
                        with instance.accepted_history_reader() as history:
                            accepted_at = instance.accepted_evaluation_time(
                                history.generation(head).git_oid
                            )
                        bindings.append(
                            (trigger, trigger_binding_for(trigger, generation=head), accepted_at)
                        )
                    continue
                covered = trigger_generation(instance, trigger.trigger)
                recorded = None if generation_cursors is None else generation_cursors.get(name)
                if generation_after is not None:
                    covered = max(covered, generation_after)
                elif request.since is not None and recorded is None:
                    # A listening segment starts afresh; accepts made while it
                    # was stopped are never replayed.
                    covered = max(covered, generation_at(instance, request.since))
                if recorded is not None:
                    covered = max(covered, recorded)
                else:
                    # A listening segment recorded before generation cursors
                    # recovers coverage from admissions once, then keeps it in
                    # its scan cursors.
                    for prior_admission in _trigger_admissions(
                        instance, accepted, trigger.trigger.identity
                    ):
                        cause = getattr(prior_admission, "trigger_binding", None)
                        if cause is not None and cause.generation is not None:
                            covered = max(covered, cause.generation)
                # A listening segment matches what was accepted since its last
                # pass, as of now.
                generation_updates[name] = head
                if head > covered:
                    bindings.append((trigger, trigger_binding_for(trigger, generation=head), now))
            elif (
                schedule_is_timed(schedule) and request.since is not None and pending_scope is None
            ):
                # An explicit or historical range reads every instant of the
                # schedule in [since, until) on this Trigger version's own
                # timeline (a cadence's grid from its acceptance, a cron's
                # calendar), none at or before that acceptance, that no earlier
                # match or evaluation delivered. A page ends only after a tick
                # it could not list, so a range is never covered past a tick
                # not found.
                remaining = request.limit - len(bindings)
                if remaining <= 0:
                    next_cursor = cursor_at(trigger, None)
                    break
                binding = trigger_binding_for(trigger)
                delivered = _delivered_ticks(
                    instance, identity=identity, epoch=accepted.line.occurrence_epoch, trigger=name
                )
                ticks_after = request.since - timedelta(microseconds=1)
                if position is not None:
                    ticks_after = max(ticks_after, _tick_after(position))
                listed: datetime | None = None
                for instant in timer_instants(
                    schedule,
                    accepted_at=trigger_accepted_at(instance, trigger),
                    after=ticks_after,
                    through=until - timedelta(microseconds=1),
                ):
                    occurrence, _ = _line_occurrence(
                        accepted,
                        evaluation_time=instant,
                        trigger=trigger,
                        binding=binding,
                        exact_basis=instant,
                    )
                    if occurrence in delivered or _line_admissions(
                        instance, accepted, occurrence_id=occurrence
                    ):
                        continue
                    if remaining == 0:
                        assert listed is not None
                        next_cursor = cursor_at(
                            trigger, [_TICK_POSITION, format_datetime(listed), 0]
                        )
                        break
                    bindings.append((trigger, binding, instant))
                    remaining -= 1
                    listed = instant
                if next_cursor:
                    break
            elif schedule_is_timed(schedule):
                # A listening pass, or a check with no range, reads the tick due
                # next in this Trigger's chain: the timer's first instant after
                # the latest tick delivered to the Line, by that tick's
                # scheduled instant (when any tick was dispatched, a recovered
                # one included, never moves the chain), and at or after where
                # the pass's own matching reached (`since`). A tick already
                # pending keeps its original due instant; checks must not invent
                # a new occurrence on every call. An armed segment
                # (`pending_scope`) only ever resumes its own tick.
                binding = trigger_binding_for(trigger)
                last_tick: datetime | None = None
                held: datetime | None = None
                if dispatch_root(instance).exists():
                    with LineDispatchStore(instance).locked() as conn:
                        # One +00:00 ISO form throughout, so text order is time order.
                        (latest,) = conn.execute(
                            "SELECT max(eligible_at) FROM pending "
                            "WHERE line_id=? AND epoch=? AND trigger_id=?",
                            (identity, accepted.line.occurrence_epoch, name),
                        ).fetchone()
                        row = conn.execute(
                            "SELECT eligible_at FROM pending WHERE line_id=? AND trigger_id=? "
                            "AND disposition='pending'"
                            + (" AND session_id=?" if pending_scope is not None else "")
                            + " ORDER BY eligible_at,occurrence_id LIMIT 1",
                            (
                                identity,
                                name,
                                *((pending_scope,) if pending_scope is not None else ()),
                            ),
                        ).fetchone()
                    last_tick = None if latest is None else parse_datetime(latest)
                    held = None if row is None else parse_datetime(row[0])
                _, due = _line_occurrence(
                    accepted,
                    evaluation_time=now,
                    last_tick=last_tick,
                    trigger=trigger,
                    binding=binding,
                    not_before=request.since,
                    accepted_at=trigger_accepted_at(instance, trigger),
                )
                if held is not None:
                    due = held
                bindings.append((trigger, binding, due or now))
            else:
                raise ExecutionError(f"unsupported Trigger schedule kind {schedule.kind!r}")
        for trigger, binding, eligible in bindings:
            if (eligible >= until and not include_future_windows) or (
                request.since is not None and eligible < request.since
            ):
                continue
            occurrence, _ = _line_occurrence(
                accepted,
                evaluation_time=eligible,
                trigger=trigger,
                binding=binding,
                exact_basis=eligible if binding.kind in TIMED_BINDING_KINDS else None,
            )
            admission = next(
                iter(_line_admissions(instance, accepted, occurrence_id=occurrence)), None
            )
            occurrences.append(
                LineTriggerOccurrence(
                    occurrence_id=occurrence,
                    binding=binding,
                    eligible_at=eligible,
                    admitted_run_id=admission.run_id if admission else None,
                )
            )
    except (CruxibleError, OSError, ValueError) as exc:
        return LineEvaluateResult(**context, status="incomplete", detail=str(exc))
    if dispatch_root(instance).exists() and occurrences:
        states = LineDispatchStore(instance).occurrence_states(
            identity, tuple(item.occurrence_id for item in occurrences)
        )
        occurrences = [
            item.model_copy(
                update={
                    "pending": states.get(item.occurrence_id) == "pending",
                    "dispatch_status": states.get(item.occurrence_id),
                }
            )
            for item in occurrences
        ]
    if generation_cursors is not None:
        generation_cursors.update(generation_updates)
    return LineEvaluateResult(
        **context,
        status="incomplete"
        if not complete or next_cursor
        else ("met" if occurrences else "not_met"),
        occurrences=tuple(occurrences),
        cursor=next_cursor,
        detail=None
        if complete
        else "Capture selector index is catching up; repeat this range before concluding absence.",
    )
