"""Restart recovery for enabled Lines: every uncovered range shows in next until evaluated.

A daemon restart rolls an enabled Line forward-only. `line_attention` names
each range its daemon never matched; an evaluation of that exact range finds
what the Line's Triggers made eligible there, and only then is the range
covered. Re-enabling does not hide a range nobody evaluated.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.line_dispatch import (
    LineDispatchRequest,
    LineEnablementPrincipal,
    LineEvaluateRequest,
)
from cruxible_client.contracts.temporal import parse_datetime
from cruxible_client.contracts.triggers import (
    CadenceSchedule,
    CaptureLandingSchedule,
    CronSchedule,
    GenerationAcceptedSchedule,
    TriggerSchedule,
)
from cruxible_core.runtime.line_arms import dispatch_armed_line
from cruxible_core.service.procedures.line_dispatch import (
    armed_work,
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
    _manager,
    _match,
    _status,
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


# --- timed Triggers across a restart (forward-only resume) -------------------


@dataclass(frozen=True)
class _Timer:
    """One timed Trigger's instants around a restart, all after its 15:00 acceptance."""

    schedule: TriggerSchedule
    first: datetime  # the tick the live arm ran before the downtime
    restart: datetime
    missed: tuple[datetime, ...]  # the instants the downtime skipped
    next: datetime  # the resumed arm's first tick
    then: datetime  # the tick after it


def _at(minutes: int, seconds: int = 0) -> datetime:
    return READ_TIME + timedelta(minutes=minutes, seconds=seconds)


TIMERS = (
    # A 150 s cadence sits on its own grid from its 15:00:00 acceptance:
    # 16:00:00, 16:02:30, 16:05:00, ... A restart at 16:08:40 is off that grid.
    _Timer(
        CadenceSchedule(interval_seconds=150),
        first=_at(2, 30),
        restart=_at(8, 40),
        missed=(_at(5), _at(7, 30)),
        next=_at(10),
        then=_at(12, 30),
    ),
    _Timer(
        CronSchedule(expression="*/3 * * * *"),
        first=_at(3),
        restart=_at(10, 20),
        missed=(_at(6), _at(9)),
        next=_at(12),
        then=_at(15),
    ),
)


def _queued(instance, *, disposition=None):  # type: ignore[no-untyped-def]
    """Every tick the dispatch store holds, as (eligible_at, matched by an arm)."""

    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    with LineDispatchStore(instance).locked() as conn:
        rows = conn.execute(
            "SELECT eligible_at,session_id FROM pending"
            + ("" if disposition is None else " WHERE disposition=?")
            + " ORDER BY eligible_at",
            () if disposition is None else (disposition,),
        ).fetchall()
    return [(parse_datetime(row[0]), row[1] is not None) for row in rows]


def _drain_armed(instance, at):  # type: ignore[no-untyped-def]
    for arm in armed_work(instance, now=at):
        dispatch_armed_line(_manager(instance), instance.descriptor.instance_id, arm, now=at)


def _restarted_timed_line(tmp_path, timer: _Timer, *, covered_until=None):  # type: ignore[no-untyped-def]
    """An enabled timed Line that ran one tick, then a downtime spanning two more.

    `covered_until` is where the live listener's last pass before the downtime
    reached (exclusive), so the restart gap starts exactly there.
    """

    instance, line, _procedure = line_world(tmp_path, timer.schedule)
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    _match(instance, start + timedelta(seconds=1))
    assert _queued(instance) == []  # nothing ticks at the enable instant
    ran = timer.first + timedelta(seconds=1)
    _match(instance, ran)
    _drain_armed(instance, ran)
    assert _queued(instance, disposition="admitted") == [(timer.first, True)]
    if covered_until is not None:
        _match(instance, covered_until - timedelta(microseconds=1))
    # Down from here until `restart`; the restarted daemon rolls the arm over.
    _match(instance, timer.restart, daemon_id="restarted")
    return instance, line


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_restarted_timed_line_ticks_next_on_its_schedule_never_at_the_restart(tmp_path, timer):
    instance, _line = _restarted_timed_line(tmp_path, timer)

    _match(instance, timer.restart + timedelta(seconds=1), daemon_id="restarted")
    # No tick fires at the restart instant: it is not one of the schedule's.
    assert _queued(instance) == [(timer.first, True)]

    _match(instance, timer.next + timedelta(seconds=1), daemon_id="restarted")
    _drain_armed(instance, timer.next + timedelta(seconds=1))
    assert _queued(instance) == [(timer.first, True), (timer.next, True)]
    assert _admissions(instance) == 2


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_evaluating_a_timed_lines_restart_gap_recovers_exactly_the_ticks_it_missed(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    _match(instance, resumed, daemon_id="restarted")
    (gap,), pending = line_attention(instance, now=resumed)
    assert pending == () and gap.since < timer.missed[0] and gap.until == timer.restart

    previewed = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=gap.since, until=gap.until, dry_run=True),
        actor=None,
        now=resumed,
    )
    assert previewed.status == "met"
    assert [item.eligible_at for item in previewed.occurrences] == list(timer.missed)
    assert all(item.dispatch_status is None for item in previewed.occurrences)
    assert line_attention(instance, now=resumed)[0] == (gap,)  # a dry run covers nothing

    evaluated = _evaluate(instance, line, gap, resumed)
    assert [(item.eligible_at, item.pending) for item in evaluated.occurrences] == [
        (at, True) for at in timer.missed
    ]
    gaps, (work,) = line_attention(instance, now=resumed)
    assert gaps == () and work.due == 2
    # Evaluating the range again finds nothing new and enqueues nothing twice.
    again = _evaluate(instance, line, gap, resumed)
    assert again.status == "not_met" and again.occurrences == ()

    drained = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=resumed + timedelta(seconds=1),
    )
    assert [item.status for item in drained.items] == ["admitted", "admitted"]
    assert _queued(instance, disposition="admitted") == [
        (timer.first, True),
        *((at, False) for at in timer.missed),
    ]
    assert line_attention(instance, now=resumed + timedelta(seconds=1)) == ((), ())

    # The resumed arm ticks on by itself at its schedule's next instant.
    ticked = timer.next + timedelta(seconds=1)
    _match(instance, ticked, daemon_id="restarted")
    _drain_armed(instance, ticked)
    assert _queued(instance, disposition="admitted")[-1] == (timer.next, True)
    assert _admissions(instance) == 4


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_timed_gap_is_covered_only_as_far_as_its_ticks_were_found(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)

    def evaluate(since, until, **page):  # type: ignore[no-untyped-def]
        return service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequest(since=since, until=until, **page),
            actor=_actor(instance),
            now=resumed,
        )

    # A range with no tick in it is covered by an empty evaluation.
    empty = evaluate(gap.since, timer.missed[0])
    assert empty.status == "not_met" and empty.occurrences == ()
    (rest,), _ = line_attention(instance, now=resumed)
    assert (rest.since, rest.until) == (timer.missed[0], gap.until)

    # A page that stops before the range's last tick covers nothing...
    first = evaluate(rest.since, rest.until, limit=1)
    assert first.status == "incomplete" and first.cursor is not None
    assert [item.eligible_at for item in first.occurrences] == [timer.missed[0]]
    assert line_attention(instance, now=resumed)[0] == (rest,)
    # ...until its cursor is followed to the end of the range.
    last = evaluate(rest.since, rest.until, limit=1, cursor=first.cursor)
    assert last.status == "met" and last.cursor is None
    assert [item.eligible_at for item in last.occurrences] == [timer.missed[1]]
    assert line_attention(instance, now=resumed)[0] == ()
    assert [at for at, _ in _queued(instance, disposition="pending")] == list(timer.missed)


@pytest.mark.parametrize(
    ("timer", "after_acceptance"),
    (
        (TIMERS[0], (_at(-57, -30), _at(-55))),  # 15:02:30, 15:05:00
        (TIMERS[1], (_at(-57), _at(-54))),  # 15:03, 15:06: 15:00 is the acceptance itself
    ),
    ids=("cadence", "cron"),
)
def test_a_timed_range_never_reaches_before_acceptance_or_lists_a_delivered_tick(
    tmp_path, timer, after_acceptance
):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    accepted = READ_TIME - timedelta(hours=1)  # line_world accepts its Trigger at 15:00

    def check(since, until):  # type: ignore[no-untyped-def]
        return service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequest(since=since, until=until, dry_run=True),
            actor=None,
            now=resumed,
        )

    # No Trigger fires retroactively: none of its instants is at or before acceptance.
    early = check(accepted - timedelta(minutes=5), after_acceptance[1] + timedelta(seconds=1))
    assert [item.eligible_at for item in early.occurrences] == list(after_acceptance)
    # From the enablement to the restart, which holds the tick the arm already
    # ran, only the missed ones show.
    spanning = check(READ_TIME + timedelta(seconds=10), timer.restart)
    assert [item.eligible_at for item in spanning.occurrences] == list(timer.missed)


# --- recovered ticks never move the live chain (review F-001) ----------------


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_recovered_ticks_dispatched_after_the_next_live_tick_never_skip_it(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)
    recovered = _evaluate(instance, line, gap, resumed)
    assert [item.eligible_at for item in recovered.occurrences] == list(timer.missed)

    # The listener runs on through the second before the next live tick, and
    # the recovered ticks are dispatched only once that tick is due.
    _match(instance, timer.next - timedelta(seconds=1), daemon_id="restarted")
    drained = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=timer.next + timedelta(seconds=1),
    )
    assert [item.status for item in drained.items] == ["admitted", "admitted"]

    # The chain reads each tick's scheduled instant, never when it ran: the
    # arm still matches the live tick it was due, then the one after it.
    for tick in (timer.next, timer.then):
        at = tick + timedelta(seconds=2)
        _match(instance, at, daemon_id="restarted")
        assert (tick, True) in _queued(instance, disposition="pending")
        _drain_armed(instance, at)
    assert _queued(instance, disposition="admitted") == [
        (timer.first, True),
        *((at, False) for at in timer.missed),
        (timer.next, True),
        (timer.then, True),
    ]
    assert line_attention(instance, now=timer.then + timedelta(seconds=2)) == ((), ())


# --- the listener never passes a tick it still owes (review F-003) ----------


def _recovered(instance, line, timer):  # type: ignore[no-untyped-def]
    """Evaluate and dispatch the restart gap, leaving nothing owed before the resume."""

    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)
    _evaluate(instance, line, gap, resumed)
    service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=resumed,
    )
    assert line_attention(instance, now=resumed) == ((), ())


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_later_tick_evaluated_first_never_stands_in_for_an_earlier_live_one(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    _recovered(instance, line, timer)
    # The listener runs through the second before its next tick, then stalls,
    # while the tick after that one is evaluated explicitly, ahead of it.
    _match(instance, timer.next - timedelta(seconds=1), daemon_id="restarted")
    at = timer.then + timedelta(seconds=1)
    later = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=timer.then, until=at),
        actor=_actor(instance),
        now=at,
    )
    assert [item.eligible_at for item in later.occurrences] == [timer.then]

    # The listener still matches the earlier tick it owes, and only that one.
    matched = at + timedelta(seconds=1)
    _match(instance, matched, daemon_id="restarted")
    assert _queued(instance, disposition="pending") == [
        (timer.next, True),
        (timer.then, False),
    ]
    _drain_armed(instance, matched)
    service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=matched,
    )
    # Each tick was delivered once, and nothing is owed through both instants.
    assert _queued(instance) == [
        (timer.first, True),
        *((tick, False) for tick in timer.missed),
        (timer.next, True),
        (timer.then, False),
    ]
    assert _queued(instance, disposition="admitted") == _queued(instance)
    assert line_attention(instance, now=matched) == ((), ())
    # Its matching reached past both: a restart now leaves a gap only after them.
    restarted = matched + timedelta(seconds=1)
    _match(instance, restarted, daemon_id="again")
    (gap,), _ = line_attention(instance, now=restarted)
    assert timer.then < gap.since and gap.until == restarted


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_stalled_listener_matches_every_tick_it_owes_in_order(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    _recovered(instance, line, timer)
    _match(instance, timer.next - timedelta(seconds=1), daemon_id="restarted")

    # Both ticks fell due while the listener stalled: it matches the first and
    # reaches only up to the second, which it matches once the first ran.
    at = timer.then + timedelta(seconds=1)
    _match(instance, at, daemon_id="restarted")
    assert _queued(instance, disposition="pending") == [(timer.next, True)]
    _drain_armed(instance, at)
    _match(instance, at + timedelta(seconds=1), daemon_id="restarted")
    assert _queued(instance, disposition="pending") == [(timer.then, True)]
    _drain_armed(instance, at + timedelta(seconds=1))
    assert _queued(instance, disposition="admitted")[-2:] == [
        (timer.next, True),
        (timer.then, True),
    ]
    assert line_attention(instance, now=at + timedelta(seconds=1)) == ((), ())


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_restart_while_the_listener_owes_ticks_leaves_them_in_the_gap(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    _recovered(instance, line, timer)
    _match(instance, timer.next - timedelta(seconds=1), daemon_id="restarted")
    at = timer.then + timedelta(seconds=1)
    _match(instance, at, daemon_id="restarted")  # matches `next`, still owes `then`
    assert _queued(instance, disposition="pending") == [(timer.next, True)]

    # Restarted before the arm ran its tick: that tick lapses, and the gap
    # starts where the listener's matching reached, at the tick it still owed.
    restarted = at + timedelta(seconds=30)
    _match(instance, restarted, daemon_id="again")
    (gap,), _ = line_attention(instance, now=restarted)
    assert (gap.since, gap.until) == (timer.then, restarted)
    assert _queued(instance, disposition="lapsed") == [(timer.next, True)]
    evaluated = _evaluate(instance, line, gap, restarted)
    assert [item.eligible_at for item in evaluated.occurrences] == [timer.then]
    assert line_attention(instance, now=restarted)[0] == ()


@dataclass(frozen=True)
class _HeldTimer:
    """A timed Line whose first tick, matched inside the idle checkpoint, stays pending."""

    schedule: TriggerSchedule
    start: datetime  # enabled
    held: datetime  # the first pass, under a minute in: matches `queued`
    queued: datetime
    checkpoint: datetime  # a later pass, past the idle interval, while `queued` waits
    restart: datetime
    unqueued: tuple[datetime, ...]  # every tick from `queued` to the restart nothing matched


HELD_TIMERS = (
    # A 20 s cadence from 15:00 ticks on :00, :20 and :40 of each minute.
    _HeldTimer(
        CadenceSchedule(interval_seconds=20),
        start=_at(0, 10),
        held=_at(0, 21),
        queued=_at(0, 20),
        checkpoint=_at(1, 15),
        restart=_at(1, 20),
        unqueued=(_at(0, 40), _at(1)),
    ),
    _HeldTimer(
        CronSchedule(expression="* * * * *"),
        start=_at(0, 59),
        held=_at(1, 1),
        queued=_at(1),
        checkpoint=_at(4),
        restart=_at(5, 1),
        unqueued=(_at(2), _at(3), _at(4), _at(5)),
    ),
)


def _held_through_a_checkpoint(tmp_path, timer: _HeldTimer):  # type: ignore[no-untyped-def]
    instance, line, _procedure = line_world(tmp_path, timer.schedule)
    _enable(instance, line, timer.start)
    _match(instance, timer.held)
    assert _queued(instance, disposition="pending") == [(timer.queued, True)]
    _match(instance, timer.checkpoint)  # its coverage advances; the tick still waits
    return instance, line


@pytest.mark.parametrize("timer", HELD_TIMERS, ids=("cadence", "cron"))
def test_a_first_tick_held_through_a_checkpoint_leaves_every_owed_tick_in_the_gap(tmp_path, timer):
    instance, line = _held_through_a_checkpoint(tmp_path, timer)
    _match(instance, timer.restart, daemon_id="restarted")

    # The gap starts where the Trigger's own matching reached, before every
    # tick it had not matched, however far the segment's coverage advanced.
    (gap,), _ = line_attention(instance, now=timer.restart)
    assert timer.queued < gap.since <= timer.unqueued[0] and gap.until == timer.restart
    evaluated = _evaluate(instance, line, gap, timer.restart)
    assert [item.eligible_at for item in evaluated.occurrences] == list(timer.unqueued)
    assert _queued(instance, disposition="lapsed") == [(timer.queued, True)]
    assert line_attention(instance, now=timer.restart)[0] == ()


@pytest.mark.parametrize("timer", HELD_TIMERS, ids=("cadence", "cron"))
def test_a_segment_with_no_recorded_reach_gaps_from_its_start(tmp_path, timer):
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    instance, line = _held_through_a_checkpoint(tmp_path, timer)
    # A segment that recorded no reach for its timed Trigger, as one written
    # before reaches were kept, is read from its start, as its matching is.
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        (payload,) = conn.execute("SELECT payload FROM sessions WHERE active=1").fetchone()
        session = json.loads(payload)
        session.pop("trigger_until")
        store.append(conn, "coverage", session, actor=_actor(instance), now=timer.checkpoint)
    _match(instance, timer.restart, daemon_id="restarted")

    (gap,), _ = line_attention(instance, now=timer.restart)
    assert (gap.since, gap.until) == (timer.start, timer.restart)
    evaluated = _evaluate(instance, line, gap, timer.restart)
    assert [item.eligible_at for item in evaluated.occurrences] == list(timer.unqueued)


# --- a timed page's cursor serves only its own mode (review F-002) -----------


def _paged(instance, line, gap, at, *, dry_run, cursor=None):  # type: ignore[no-untyped-def]
    """One one-tick page of a gap, previewed or enqueued."""

    return service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(
            since=gap.since, until=gap.until, limit=1, cursor=cursor, dry_run=dry_run
        ),
        actor=None if dry_run else _actor(instance),
        now=at,
    )


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_dry_run_cursor_never_finishes_an_evaluation_that_enqueues(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)

    previewed = _paged(instance, line, gap, resumed, dry_run=True)
    assert previewed.status == "incomplete"
    assert [item.eligible_at for item in previewed.occurrences] == [timer.missed[0]]
    with pytest.raises(ExecutionError, match="mode"):
        _paged(instance, line, gap, resumed, dry_run=False, cursor=previewed.cursor)
    # Nothing was enqueued and nothing covered: the first tick is still owed.
    assert _queued(instance, disposition="pending") == []
    assert line_attention(instance, now=resumed)[0] == (gap,)
    # The cursor still finishes the dry run that returned it.
    rest = _paged(instance, line, gap, resumed, dry_run=True, cursor=previewed.cursor)
    assert rest.status == "met"
    assert [item.eligible_at for item in rest.occurrences] == [timer.missed[1]]

    # An enqueueing page's cursor likewise never continues a dry run...
    first = _paged(instance, line, gap, resumed, dry_run=False)
    assert first.status == "incomplete"
    assert [item.eligible_at for item in first.occurrences] == [timer.missed[0]]
    with pytest.raises(ExecutionError, match="mode"):
        _paged(instance, line, gap, resumed, dry_run=True, cursor=first.cursor)
    # ...and finishes its own evaluation, which covers the gap only now.
    last = _paged(instance, line, gap, resumed, dry_run=False, cursor=first.cursor)
    assert last.status == "met" and last.cursor is None
    assert [item.eligible_at for item in last.occurrences] == [timer.missed[1]]
    assert [at for at, _ in _queued(instance, disposition="pending")] == list(timer.missed)
    assert line_attention(instance, now=resumed)[0] == ()


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_an_enqueueing_cursor_cannot_skip_a_tick_no_page_enqueued(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)
    previewed = _paged(instance, line, gap, resumed, dry_run=True)

    # Rewritten to claim the enqueueing mode, the dry run's cursor still skips
    # a tick no page enqueued, so it cannot finish the range.
    decoded = json.loads(base64.urlsafe_b64decode(previewed.cursor or ""))
    decoded["scope"] = ["enqueue" if part == "preview" else part for part in decoded["scope"]]
    forged = base64.urlsafe_b64encode(canonical_bytes(decoded)).decode()
    with pytest.raises(ExecutionError, match="no page enqueued"):
        _paged(instance, line, gap, resumed, dry_run=False, cursor=forged)
    assert _queued(instance, disposition="pending") == []
    assert line_attention(instance, now=resumed)[0] == (gap,)


# --- tick boundaries: a gap's edges, a range's edges, a successor ------------


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_tick_exactly_where_the_listener_stopped_is_the_gaps_and_recovered_once(tmp_path, timer):
    # The last pass before the downtime covered up to, not including, the
    # first missed tick, so the gap's inclusive start is exactly that tick.
    instance, line = _restarted_timed_line(tmp_path, timer, covered_until=timer.missed[0])
    resumed = timer.restart + timedelta(seconds=1)
    _match(instance, resumed, daemon_id="restarted")
    (gap,), _ = line_attention(instance, now=resumed)
    assert gap.since == timer.missed[0]
    assert _queued(instance) == [(timer.first, True)]  # no live pass reached it

    evaluated = _evaluate(instance, line, gap, resumed)
    assert [item.eligible_at for item in evaluated.occurrences] == list(timer.missed)
    assert line_attention(instance, now=resumed)[0] == ()
    assert [at for at, _ in _queued(instance)] == [timer.first, *timer.missed]


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_tick_exactly_at_the_restart_is_the_resumed_arms_not_the_gaps(tmp_path, timer):
    timer = replace(timer, restart=timer.missed[1], missed=timer.missed[:1])
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    _match(instance, resumed, daemon_id="restarted")
    assert (timer.restart, True) in _queued(instance, disposition="pending")
    (gap,), _ = line_attention(instance, now=resumed)
    assert gap.until == timer.restart  # the gap's exclusive end leaves it to the arm

    evaluated = _evaluate(instance, line, gap, resumed)
    assert [item.eligible_at for item in evaluated.occurrences] == list(timer.missed)
    _drain_armed(instance, resumed)
    service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=resumed,
    )
    assert _queued(instance, disposition="admitted") == [
        (timer.first, True),
        (timer.missed[0], False),
        (timer.restart, True),
    ]


@pytest.mark.parametrize("timer", TIMERS, ids=("cadence", "cron"))
def test_a_tick_on_since_is_in_the_range_and_one_on_until_is_not(tmp_path, timer):
    instance, line = _restarted_timed_line(tmp_path, timer)
    resumed = timer.restart + timedelta(seconds=1)
    (gap,), _ = line_attention(instance, now=resumed)

    def evaluate(since, until):  # type: ignore[no-untyped-def]
        return service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequest(since=since, until=until),
            actor=_actor(instance),
            now=resumed,
        )

    head = evaluate(timer.missed[0], timer.missed[1])
    assert [item.eligible_at for item in head.occurrences] == [timer.missed[0]]
    tail = evaluate(timer.missed[1], gap.until)
    assert [item.eligible_at for item in tail.occurrences] == [timer.missed[1]]
    assert [at for at, _ in _queued(instance, disposition="pending")] == list(timer.missed)
    # Only the part of the gap before the first tick is left, and it holds none.
    (rest,), _ = line_attention(instance, now=resumed)
    assert (rest.since, rest.until) == (gap.since, timer.missed[0])
    again = _evaluate(instance, line, rest, resumed)
    assert again.status == "not_met" and again.occurrences == ()
    assert line_attention(instance, now=resumed)[0] == ()


#: Per timer: a successor schedule accepted mid-downtime at 16:04:10, its
#: instants from there to the timer's restart (a cadence on its own grid from
#: that acceptance), and its next instant after the Line is enabled again.
SUCCESSORS = (
    (CadenceSchedule(interval_seconds=120), (_at(6, 10), _at(8, 10)), _at(10, 10)),
    (CronSchedule(expression="*/2 * * * *"), (_at(6), _at(8), _at(10)), _at(12)),
)


@pytest.mark.parametrize(
    ("timer", "successor"), tuple(zip(TIMERS, SUCCESSORS, strict=True)), ids=("cadence", "cron")
)
def test_a_successor_accepted_mid_gap_recovers_and_ticks_on_its_own_schedule(
    tmp_path, timer, successor
):
    from tests.support.lines import line_trigger, trigger_members
    from tests.support.lines import successor as successor_of
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_line_triggers import TRIGGER

    schedule, missed, next_tick = successor
    instance, line, _procedure, owner = line_world(tmp_path, timer.schedule, with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    _enable(instance, line, start)
    ran = timer.first + timedelta(seconds=1)
    _match(instance, ran)
    _drain_armed(instance, ran)
    # Down from here; the successor is accepted at 16:04:10, mid-downtime.
    predecessor = line_trigger(TRIGGER, line=line.identity.name, schedule=timer.schedule)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(trigger_members(successor_of(predecessor, schedule=schedule)))
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T16:04:10.000000Z", proposal_name="retime"
    )
    # The enablement is pinned to the predecessor, so the restarted daemon
    # stops it rather than adopting the successor; it is enabled again.
    _match(instance, timer.restart, daemon_id="restarted")
    assert _status(instance, line, timer.restart).stop_reason == "trigger_changed"
    resumed = timer.restart + timedelta(seconds=1)
    _enable(instance, line, resumed)

    # The downtime holds only the live successor's instants after its
    # acceptance: none of the predecessor's, and not the tick it already ran.
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=start, until=timer.restart),
        actor=_actor(instance),
        now=resumed,
    )
    assert [item.eligible_at for item in evaluated.occurrences] == list(missed)

    # Dispatched only once the successor's next tick is due, the recovered
    # ticks still leave that tick to the enabled Line.
    _match(instance, next_tick - timedelta(seconds=1))
    drained = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(),
        actor=_actor(instance),
        caller_rung=3,
        now=next_tick + timedelta(seconds=1),
    )
    assert [item.status for item in drained.items] == ["admitted"] * len(missed)
    at = next_tick + timedelta(seconds=2)
    _match(instance, at)
    _drain_armed(instance, at)
    assert _queued(instance, disposition="admitted") == [
        (timer.first, True),
        *((tick, False) for tick in missed),
        (next_tick, True),
    ]
    assert line_attention(instance, now=at) == ((), ())
