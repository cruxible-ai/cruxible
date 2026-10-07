"""``line run`` is one manual occurrence: it never selects, consumes or waits on a Trigger.

A Line whose Procedure takes an event input gets it as ``event``; the event's
record digest names the manual occurrence, and an event the Line's Trigger
already admitted runs again only when ``repeat`` says so.
"""

from datetime import timedelta

import pytest

from cruxible_client.contracts.errors import ExecutionError
from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
from cruxible_client.contracts.triggers import CadenceSchedule
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_evaluate_line,
)
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    service_run_playbill_line,
)
from tests.support.lines import line_trigger, trigger_members
from tests.test_procedures.test_procedure_source_runs import (
    NOW,
    RELATIVE_PATH,
    _accept_more,
    _actor,
    _line_mandate,
    _line_world,
    _Operator,
    _reader,
    _run,
    _served_line,
    _TestClock,
    _WorkspaceInvoker,
    _world,
)
from tests.test_procedures.test_trigger_capture_inputs import admission, event_from, world

TICK = "advisory-tick"


def manual_run(instance, line, *, at, event=None, repeat=False, root=None):  # type: ignore[no-untyped-def]
    """A manual ``line run``: no Trigger named, only the Line's own inputs."""

    return service_run_playbill_line(
        instance,
        path_identity_digest=line.identity.name,
        request=LineRunRequest(line=line.identity.name, event=event, repeat=repeat),
        actor_context=_actor(instance),
        caller_rung=2,
        daemon_clock=_TestClock(at),
        provider_runtime_operator=None if root is None else _Operator(_WorkspaceInvoker()),
        workspace_file_reader=None if root is None else _reader(instance, root),
    )


def trigger_binding(instance, run_id):  # type: ignore[no-untyped-def]
    """The Trigger binding a run was admitted under; a manual run's admission carries none."""

    return getattr(admission(instance, run_id).admission, "trigger_binding", None)


def ticking_world(tmp_path):  # type: ignore[no-untyped-def]
    """The Source Line, with a live cadence Trigger aimed at it (never enabled)."""

    instance, owner, procedure, root, policy = _world(tmp_path)
    line = _served_line(procedure, policy)
    mandate = _line_mandate(procedure)
    tick = line_trigger(
        TICK, line=line.identity.name, schedule=CadenceSchedule(interval_seconds=60)
    )
    _accept_more(
        instance,
        owner,
        {
            line_spec_path(line.identity.name): render_line_spec(line),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
            **trigger_members(tick),
        },
        name="advisory-ticking-line",
    )
    return instance, root, line


def _eligible(instance, line, *, now):  # type: ignore[no-untyped-def]
    return service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=NOW - timedelta(hours=1), until=now, dry_run=True),
        actor=None,
        now=now,
    )


def test_a_manual_run_on_a_triggered_line_names_no_trigger_and_leaves_its_ticks(tmp_path):
    instance, root, line = ticking_world(tmp_path)
    later = NOW + timedelta(minutes=30)
    before = _eligible(instance, line, now=later)
    assert before.occurrences, before

    # A live Trigger aims at this Line, and the run still names none.
    manual = manual_run(instance, line, at=NOW, root=root)
    assert manual.status == "succeeded", manual.model_dump(mode="json")
    assert trigger_binding(instance, manual.run_id) is None

    # The Trigger's tick accounting never saw the manual run.
    after = _eligible(instance, line, now=later)
    assert [o.occurrence_id for o in after.occurrences] == [
        o.occurrence_id for o in before.occurrences
    ]
    assert all(o.admitted_run_id is None for o in after.occurrences)
    actor = _actor(instance)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=NOW - timedelta(hours=1), until=later),
        actor=actor,
        now=later,
    )
    (dispatched,) = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(limit=1),
        actor=actor,
        now=later,
        caller_rung=2,
        provider_runtime_operator=_Operator(_WorkspaceInvoker()),
        workspace_file_reader=_reader(instance, root),
    ).items
    assert dispatched.status == "admitted", dispatched
    assert dispatched.occurrence_id == before.occurrences[0].occurrence_id
    assert dispatched.run_id != manual.run_id
    bound = trigger_binding(instance, dispatched.run_id)
    assert bound is not None and bound.trigger.name == TICK


def test_a_manual_run_passes_its_event_as_the_lines_source_input(tmp_path):
    instance, root, line = world(tmp_path)
    produced, _ = _run(instance, root)
    event = event_from(produced)
    # The workspace now reads differently: only the retained Capture yields "high".
    (root / RELATIVE_PATH).write_text('{"severity":"low"}')

    consumed = manual_run(instance, line, at=NOW + timedelta(seconds=2), event=event)

    assert consumed.status == "succeeded", consumed.model_dump(mode="json")
    assert consumed.result == {"severity": "high"}
    bound = admission(instance, consumed.run_id)
    assert trigger_binding(instance, consumed.run_id) is None
    assert (
        bound.admission.landed_capture_inputs[0].capture_digest
        == produced.source_observations[0].capture_digest
    )
    assert bound.acquisition_plan.external_occurrences == ()


def test_the_manual_occurrence_id_names_its_event(tmp_path):
    instance, root, line = world(tmp_path)
    first = event_from(_run(instance, root)[0])
    (root / RELATIVE_PATH).write_text('{"severity":"low"}')
    second = event_from(_run(instance, root, evaluation_time=NOW + timedelta(seconds=1))[0])
    assert first != second
    at = NOW + timedelta(seconds=2)

    runs = [manual_run(instance, line, at=at, event=event) for event in (first, second)]

    assert [run.status for run in runs] == ["succeeded", "succeeded"], runs
    ids = [admission(instance, run.run_id).acquisition_plan.occurrence_id for run in runs]
    assert ids[0] != ids[1]
    assert runs[0].run_id != runs[1].run_id
    assert runs[0].result == {"severity": "high"} and runs[1].result == {"severity": "low"}


def test_a_manual_run_on_an_admitted_event_refuses_unless_repeated(tmp_path):
    instance, root, line = world(tmp_path)
    actor = _actor(instance)
    event = event_from(_run(instance, root)[0])
    now = NOW + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=NOW, until=now),
        actor=actor,
        now=now,
    )
    (dispatched,) = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequest(), actor=actor, now=now, caller_rung=2
    ).items
    assert dispatched.status == "admitted", dispatched

    refused = manual_run(instance, line, at=now + timedelta(seconds=1), event=event)

    assert refused.status == "admission_refused" and refused.run_id is None, refused
    assert refused.terminal.code == "occurrence_already_admitted"
    assert refused.terminal.details["run_id"] == dispatched.run_id
    assert dispatched.run_id in refused.terminal.message

    repeated = manual_run(instance, line, at=now + timedelta(seconds=1), event=event, repeat=True)
    assert repeated.status == "succeeded", repeated.model_dump(mode="json")
    assert repeated.run_id != dispatched.run_id
    assert trigger_binding(instance, repeated.run_id) is None


def test_an_event_on_a_line_without_an_event_input_refuses(tmp_path):
    instance, root, line = _line_world(tmp_path)
    assert line.trigger_input is None
    event = event_from(_run(instance, root)[0])

    with pytest.raises(ExecutionError, match="takes no event input"):
        manual_run(instance, line, at=NOW + timedelta(seconds=2), event=event, root=root)
