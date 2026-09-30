"""One indexed evaluator for explicit checks, listening, and historical work.

A Line's occurrences come from the live Trigger artifacts aimed at it at the
coordinate checked, each evaluated on its own schedule; a Line no Trigger aims
at has no automatic occurrences and runs only when run explicitly.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from typing import Any

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillError, PlaybillExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineTriggerCheckRequestV1,
    LineTriggerCheckResultV1,
    LineTriggerOccurrenceV1,
    LineTriggerVersionV1,
)
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_client.contracts.procedures.windows import (
    CaptureEventWindowV1,
    LineTriggerBindingV1,
    TriggerEventReferenceV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_client.contracts.triggers import (
    AcceptedTriggerV1,
    CadenceScheduleV1,
    CaptureLandingScheduleV1,
    WindowCloseScheduleV1,
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
    trigger_binding_for,
)
from cruxible_core.service.procedures.resolution_contracts import bind_window, capture_event_time


def service_check_line_trigger(
    instance: PlaybillInstance,
    line: str,
    request: LineTriggerCheckRequestV1,
    *,
    now: datetime,
    after: dict[str, Any] | None = None,
    through: dict[str, Any] | None = None,
    include_future_windows: bool = False,
    pending_scope: str | None = None,
    only_trigger: str | None = None,
) -> LineTriggerCheckResultV1:
    """Every occurrence the Line's live Triggers make eligible in one range.

    Triggers are evaluated in identity order. A page that stops inside one
    Trigger's Capture range resumes there, with the cursor naming that Trigger;
    `only_trigger` limits the check to one Trigger, as an armed segment does.
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
            LineTriggerVersionV1(
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
            raise PlaybillExecutionError(
                "trigger cursor must retain its original Line, Triggers and range"
            ) from exc
    if not triggers:
        return LineTriggerCheckResultV1(
            **context,
            status="not_met",
            detail=(
                "No live Trigger aims at this Line; it runs only when run explicitly. "
                "Accept a Trigger aimed at it to run it automatically."
            ),
        )

    def cursor_at(trigger: AcceptedTriggerV1, position: list[Any] | None) -> str:
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
    try:
        bindings: list[tuple[AcceptedTriggerV1, LineTriggerBindingV1, datetime]] = []
        for trigger in triggers:
            name = trigger.trigger.identity.qualified
            if resume is not None and name.encode("utf-8") < resume[0].encode("utf-8"):
                continue
            position = resume[1] if resume is not None and name == resume[0] else None
            cursor = None if position is None else (position[0], position[1], position[2])
            schedule = trigger.trigger.schedule
            window = schedule.window if isinstance(schedule, WindowCloseScheduleV1) else None
            selector = (
                schedule.event
                if isinstance(schedule, CaptureLandingScheduleV1)
                else (window.event if isinstance(window, CaptureEventWindowV1) else None)
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
                    event = TriggerEventReferenceV1(
                        run_id=record.run_id or "",
                        partition_id=record.partition_id,
                        sequence=record.sequence,
                        record_digest=stored.record_digest,
                    )
                    event_time = capture_event_time(instance, selector, event, now=now)
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
                bindings.append(
                    (trigger, trigger_binding_for(trigger, window=bound), bound.ends_at)
                )
            else:
                assert isinstance(schedule, CadenceScheduleV1)
                binding = trigger_binding_for(trigger)
                _, due = _line_occurrence(
                    accepted,
                    evaluation_time=now,
                    prior=_trigger_admissions(instance, accepted, trigger.trigger.identity),
                    trigger=trigger,
                    binding=binding,
                    not_before=request.since,
                )
                # An already-pending cadence tick keeps its original due instant;
                # checks must not invent a new occurrence on every call. An armed
                # segment (`pending_scope`) only ever resumes its own tick.
                if dispatch_root(instance).exists():
                    with LineDispatchStore(instance).locked() as conn:
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
                        if row is not None:
                            due = datetime.fromisoformat(row[0])
                bindings.append((trigger, binding, due or now))
        for trigger, binding, eligible in bindings:
            if (eligible >= until and not include_future_windows) or (
                request.since is not None and eligible < request.since
            ):
                continue
            occurrence, _ = _line_occurrence(
                accepted,
                evaluation_time=eligible,
                prior=(),
                trigger=trigger,
                binding=binding,
                exact_basis=eligible if binding.kind == "cadence" else None,
            )
            admission = next(
                iter(_line_admissions(instance, accepted, occurrence_id=occurrence)), None
            )
            occurrences.append(
                LineTriggerOccurrenceV1(
                    occurrence_id=occurrence,
                    binding=binding,
                    eligible_at=eligible,
                    admitted_run_id=admission.run_id if admission else None,
                )
            )
    except (PlaybillError, OSError, ValueError) as exc:
        return LineTriggerCheckResultV1(**context, status="incomplete", detail=str(exc))
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
    return LineTriggerCheckResultV1(
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
