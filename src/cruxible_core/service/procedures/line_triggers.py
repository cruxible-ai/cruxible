"""One indexed evaluator for explicit checks, listening, and historical work."""

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
)
from cruxible_client.contracts.procedures.line_specs import (
    CadenceTriggerPolicyV1,
    CaptureLandingTriggerPolicyV2,
    ManualTriggerPolicyV1,
    WindowCloseTriggerPolicyV2,
    line_identity_digest,
)
from cruxible_client.contracts.procedures.windows import (
    CaptureEventWindowV1,
    LineTriggerBindingV1,
    TriggerEventReferenceV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.procedure_runs import (
    _accepted_line_by_reference,
    _journal,
    _line_admissions,
    _line_occurrence,
    _stream,
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
) -> LineTriggerCheckResultV1:
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root

    now = ensure_utc(now)
    until = min(request.until or now + timedelta(microseconds=1), now + timedelta(microseconds=1))
    coordinate = instance.accepted_coordinate()
    accepted = _accepted_line_by_reference(instance, coordinate=coordinate, reference=line)
    policy = accepted.line.trigger_policy
    identity = line_identity_digest(accepted.line.identity)
    context: dict[str, Any] = dict(
        line=accepted.line.identity.qualified,
        line_identity_digest=identity,
        line_artifact_digest=accepted.artifact_digest,
        occurrence_epoch=accepted.line.occurrence_epoch,
        coordinate=AcceptedCoordinate.from_internal(coordinate),
        checked_since=request.since,
        checked_until=until,
    )
    scope = [
        identity,
        accepted.line.occurrence_epoch,
        request.since.isoformat() if request.since else None,
        until.isoformat(),
        after,
        through,
    ]
    cursor = None
    if request.cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(request.cursor))
            if decoded["scope"] != scope:
                raise ValueError("cursor range or Line changed")
            cursor = decoded["position"]
            if len(cursor) != 3 or not isinstance(cursor[2], int):
                raise ValueError("invalid cursor position")
        except (ValueError, KeyError, TypeError) as exc:
            raise PlaybillExecutionError(
                "trigger cursor must retain its original Line and range"
            ) from exc
    occurrences = []
    next_cursor = None
    complete = True
    try:
        bindings: list[tuple[LineTriggerBindingV1 | None, datetime]] = []
        if isinstance(policy, (CaptureLandingTriggerPolicyV2, WindowCloseTriggerPolicyV2)):
            window = policy.window if isinstance(policy, WindowCloseTriggerPolicyV2) else None
            selector = (
                policy.event
                if isinstance(policy, CaptureLandingTriggerPolicyV2)
                else (window.event if isinstance(window, CaptureEventWindowV1) else None)
            )
            if selector is not None:
                delay = (
                    timedelta(seconds=window.duration_seconds)
                    if window is not None
                    else timedelta(0)
                )
                journal, _ = _journal(instance)
                records, next_position, complete = journal.index.captures(
                    _stream(instance),
                    bodies=instance.body_store(),
                    contract_digest=selector.capture_contract_digest,
                    since=request.since - delay if request.since else None,
                    until=until if include_future_windows else until - delay,
                    limit=request.limit,
                    cursor=cursor,
                    after=after,
                    through=through,
                )
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
                            (LineTriggerBindingV1(kind="capture_landing", event=event), event_time)
                        )
                    else:
                        bound = bind_window(instance, window, event, now=now)
                        bindings.append(
                            (
                                LineTriggerBindingV1(
                                    kind="window_close", event=event, window=bound
                                ),
                                bound.ends_at,
                            )
                        )
                if next_position:
                    next_cursor = base64.urlsafe_b64encode(
                        canonical_bytes({"scope": scope, "position": list(next_position)})
                    ).decode()
            else:
                assert window is not None
                bound = bind_window(instance, window, None, now=now)
                bindings.append(
                    (LineTriggerBindingV1(kind="window_close", window=bound), bound.ends_at)
                )
        elif isinstance(policy, CadenceTriggerPolicyV1):
            prior = _line_admissions(instance, accepted)
            _, due = _line_occurrence(
                accepted, evaluation_time=now, prior=prior, not_before=request.since
            )
            # An already-pending cadence tick keeps its original due instant;
            # checks must not invent a new occurrence on every call. An armed
            # segment (`pending_scope`) only ever resumes its own tick.
            if dispatch_root(instance).exists():
                with LineDispatchStore(instance).locked() as conn:
                    row = conn.execute(
                        "SELECT eligible_at FROM pending WHERE line_id=? AND epoch=? "
                        "AND disposition='pending'"
                        + (" AND session_id=?" if pending_scope is not None else "")
                        + " ORDER BY eligible_at,occurrence_id LIMIT 1",
                        (
                            identity,
                            accepted.line.occurrence_epoch,
                            *((pending_scope,) if pending_scope is not None else ()),
                        ),
                    ).fetchone()
                    if row is not None:
                        due = datetime.fromisoformat(row[0])
            bindings.append((None, due or now))
        elif isinstance(policy, ManualTriggerPolicyV1):
            return LineTriggerCheckResultV1(
                **context,
                status="not_met",
                detail="Manual Lines require an explicit run; no automatic trigger.",
            )
        else:
            return LineTriggerCheckResultV1(
                **context,
                status="incomplete",
                detail="Accept a Line with explicit event/window bindings.",
            )
        for binding, eligible in bindings:
            if (eligible >= until and not include_future_windows) or (
                request.since is not None and eligible < request.since
            ):
                continue
            occurrence, _ = _line_occurrence(
                accepted,
                evaluation_time=eligible,
                prior=(),
                binding=binding,
                exact_basis=eligible if binding is None else None,
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
            identity,
            accepted.line.occurrence_epoch,
            tuple(item.occurrence_id for item in occurrences),
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
