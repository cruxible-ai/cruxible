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
from cruxible_client.contracts.triggers import GenerationAcceptedSchedule
from cruxible_core.service.procedures.line_dispatch import (
    line_attention,
    service_dispatch_line,
    service_enable_line,
    service_evaluate_line,
)
from tests.test_procedures.test_line_arming import (
    LOCAL,
    _accept_generation,
    _admissions,
    _match,
)
from tests.test_procedures.test_line_triggers import line_world
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
