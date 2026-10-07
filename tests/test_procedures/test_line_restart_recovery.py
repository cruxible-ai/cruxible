"""Restart recovery for enabled Lines: every uncovered range shows in next until evaluated.

A daemon restart rolls an enabled Line forward-only. `line_attention` names
each range its daemon never matched; an evaluation of that exact range finds
what the Line's Triggers made eligible there, and only then is the range
covered. Re-enabling does not hide a range nobody evaluated.
"""

from __future__ import annotations

from datetime import timedelta

from cruxible_client.contracts.line_dispatch import (
    LineDispatchRequest,
    LineEnablementPrincipal,
    LineEvaluateRequest,
)
from cruxible_client.contracts.triggers import CaptureLandingSchedule, GenerationAcceptedSchedule
from cruxible_core.service.procedures.line_dispatch import (
    line_attention,
    service_disable_line,
    service_dispatch_line,
    service_enable_line,
    service_evaluate_line,
)
from tests.test_procedures.test_line_arming import (
    CREDENTIAL,
    LOCAL,
    _accept_generation,
    _admissions,
    _match,
)
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor


def _enable(instance, line, at, principal: LineEnablementPrincipal = LOCAL):  # type: ignore[no-untyped-def]
    return service_enable_line(
        instance,
        line.identity.name,
        principal=principal,
        actor=_actor(instance),
        now=at,
        daemon_id="daemon",
    )


def _evaluate(instance, line, gap, at):  # type: ignore[no-untyped-def]
    return service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=gap.since, until=gap.until),
        actor=_actor(instance),
        now=at,
    )


def test_a_generation_accepted_during_downtime_is_found_by_evaluating_the_gap(tmp_path):
    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedSchedule(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    _match(instance, start + timedelta(seconds=1))
    # Downtime: a generation is accepted while no daemon listens.
    _accept_generation(instance, owner, "offline", start + timedelta(seconds=3))
    restarted = start + timedelta(seconds=6)
    _match(instance, restarted, daemon_id="restarted")

    (gap,), pending = line_attention(instance, now=restarted)
    assert pending == ()
    assert gap.since < start + timedelta(seconds=3) < gap.until == restarted

    evaluated = _evaluate(instance, line, gap, restarted + timedelta(seconds=1))
    assert evaluated.status == "met"
    (occurrence,) = evaluated.occurrences
    assert occurrence.pending and occurrence.binding is not None
    assert occurrence.eligible_at == start + timedelta(seconds=3)
    gaps, (work,) = line_attention(instance, now=restarted + timedelta(seconds=1))
    assert gaps == () and work.due == 1

    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=restarted + timedelta(seconds=2),
    )
    assert [item.status for item in result.items] == ["admitted"]
    assert _admissions(instance) == 1
    assert line_attention(instance, now=restarted + timedelta(seconds=2)) == ((), ())


def test_a_gap_with_no_accepted_generation_is_covered_by_an_empty_evaluation(tmp_path):
    instance, line, _, _owner = line_world(tmp_path, GenerationAcceptedSchedule(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    _match(instance, start + timedelta(seconds=1))
    restarted = start + timedelta(seconds=6)
    _match(instance, restarted, daemon_id="restarted")
    (gap,), _ = line_attention(instance, now=restarted)

    evaluated = _evaluate(instance, line, gap, restarted + timedelta(seconds=1))

    # Nothing was accepted in the range, so nothing is invented at `now`.
    assert evaluated.status == "not_met" and evaluated.occurrences == ()
    assert line_attention(instance, now=restarted + timedelta(seconds=1)) == ((), ())


def _restarted_capture_line(tmp_path):  # type: ignore[no-untyped-def]
    instance, line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    _match(instance, start + timedelta(seconds=1))
    capture(instance, procedure, at=start + timedelta(seconds=2))  # daemon down
    restarted = start + timedelta(seconds=10)
    _match(instance, restarted, daemon_id="restarted")
    (gap,), _ = line_attention(instance, now=restarted)
    return instance, line, gap, restarted


def test_reenabling_under_another_credential_keeps_the_unevaluated_gap(tmp_path):
    instance, line, gap, restarted = _restarted_capture_line(tmp_path)

    reenabled = _enable(instance, line, restarted + timedelta(seconds=1), principal=CREDENTIAL)

    assert reenabled.outcome == "reenabled"
    (kept,), _ = line_attention(instance, now=restarted + timedelta(seconds=2))
    assert (kept.since, kept.until) == (gap.since, gap.until)
    # Only an evaluation that covers it retires the row.
    _evaluate(instance, line, gap, restarted + timedelta(seconds=2))
    assert line_attention(instance, now=restarted + timedelta(seconds=2))[0] == ()


def test_disable_then_reenable_keeps_the_gap_and_adds_no_deliberate_one(tmp_path):
    instance, line, gap, restarted = _restarted_capture_line(tmp_path)
    service_disable_line(
        instance, line.identity.name, actor=_actor(instance), now=restarted + timedelta(seconds=1)
    )
    # While disabled the Line owes nothing automatic.
    assert line_attention(instance, now=restarted + timedelta(seconds=2)) == ((), ())

    _enable(instance, line, restarted + timedelta(seconds=5))

    # The restart gap is outstanding again; the disabled interval is not a gap.
    (kept,), _ = line_attention(instance, now=restarted + timedelta(seconds=6))
    assert (kept.since, kept.until) == (gap.since, gap.until)


def test_a_partial_evaluation_leaves_only_the_uncovered_rest(tmp_path):
    instance, line, gap, restarted = _restarted_capture_line(tmp_path)
    middle = gap.since + (gap.until - gap.since) / 2

    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=gap.since, until=middle),
        actor=_actor(instance),
        now=restarted + timedelta(seconds=1),
    )

    (rest,), _ = line_attention(instance, now=restarted + timedelta(seconds=1))
    assert (rest.since, rest.until) == (middle, gap.until)


# --- range boundaries for generation Triggers (S3 delta review F-001) --------


def _pending_rows(instance) -> int:  # type: ignore[no-untyped-def]
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    with LineDispatchStore(instance).locked() as conn:
        return int(conn.execute("SELECT count(*) FROM pending").fetchone()[0])


def test_an_accept_exactly_at_the_gaps_inclusive_start_is_found(tmp_path):
    """The listener covered [enable, 16:00:11.000001); an accept at that instant is the gap's."""

    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedSchedule(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)  # 16:00:10
    _enable(instance, line, start)
    _match(instance, start + timedelta(seconds=1))  # durable checked-until 16:00:11.000001
    boundary = start + timedelta(seconds=1, microseconds=1)
    _accept_generation(instance, owner, "at-the-boundary", boundary)  # daemon down
    restarted = start + timedelta(seconds=6)  # 16:00:16
    _match(instance, restarted, daemon_id="restarted")
    (gap,), _ = line_attention(instance, now=restarted)
    assert gap.since == boundary

    evaluated = _evaluate(instance, line, gap, restarted + timedelta(seconds=1))

    assert evaluated.status == "met"
    (occurrence,) = evaluated.occurrences
    assert occurrence.pending and occurrence.eligible_at == boundary
    gaps, (work,) = line_attention(instance, now=restarted + timedelta(seconds=1))
    assert gaps == () and work.due == 1
    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=restarted + timedelta(seconds=2),
    )
    assert [item.status for item in result.items] == ["admitted"]


def test_an_accept_exactly_at_the_exclusive_end_is_outside_the_range(tmp_path):
    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedSchedule(), with_owner=True)
    accepted_at = READ_TIME + timedelta(seconds=20)
    _accept_generation(instance, owner, "at-the-end", accepted_at)
    since = accepted_at - timedelta(seconds=5)

    def evaluate(until):  # type: ignore[no-untyped-def]
        return service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequest(since=since, until=until, dry_run=True),
            actor=None,
            now=accepted_at + timedelta(seconds=5),
        )

    excluded = evaluate(accepted_at)
    assert excluded.status == "not_met" and excluded.occurrences == ()
    included = evaluate(accepted_at + timedelta(microseconds=1))
    assert included.status == "met" and len(included.occurrences) == 1
    assert included.occurrences[0].eligible_at == accepted_at


def test_an_accept_at_since_the_listener_already_delivered_is_not_delivered_again(tmp_path):
    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedSchedule(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    accepted_at = start + timedelta(seconds=1)
    _accept_generation(instance, owner, "delivered-live", accepted_at)
    _match(instance, start + timedelta(seconds=2))  # the live segment delivers it
    assert _pending_rows(instance) == 1

    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=accepted_at, until=start + timedelta(seconds=3)),
        actor=_actor(instance),
        now=start + timedelta(seconds=4),
    )

    assert evaluated.status == "not_met" and evaluated.occurrences == ()
    assert _pending_rows(instance) == 1
