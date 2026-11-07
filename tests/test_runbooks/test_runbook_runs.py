"""Running a Runbook (D-R36): steps in order, stopping on a failure or on review.

A manual run runs each Line step as the caller's manual Line run and fires each
action step into the trigger journal. The run stops at the first step that
fails or whose writes go to a proposal; every later step reports ``not_run``
and never runs. A Trigger-fired run runs each Line under that Line's own
enablement, and a Line that is not enabled stops it.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.runbooks import RunbookRunResult
from cruxible_core.service import runbooks as runbook_service
from cruxible_core.service.procedures import procedure_runs
from tests.support.runbooks import (
    FAIL,
    PROPOSE,
    SETTLE,
    FakeManager,
    author_runbook,
    author_runbook_trigger,
    line_steps,
    run_runbook,
    runbook_world,
)


def _statuses(result: RunbookRunResult) -> list[str]:
    return [step.status for step in result.steps]


@pytest.fixture
def counted_line_runs(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Every Line the run actually started, by name."""

    calls: Counter[str] = Counter()
    real = procedure_runs.service_run_playbill_line

    def counting(instance: Any, **kwargs: Any) -> Any:
        calls[kwargs["path_identity_digest"]] += 1
        return real(instance, **kwargs)

    monkeypatch.setattr(procedure_runs, "service_run_playbill_line", counting)
    return calls


def test_a_completed_run_settles_and_fires_in_order(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    from cruxible_core.triggers.journal import trigger_events

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"line": SETTLE}, {"action": "evidence.sweep"}])
    base = instance.accepted_coordinate()
    result = run_runbook(instance, root, "test.rb")
    assert (result.status, result.stop_reason, result.mode) == ("completed", None, "manual")
    assert _statuses(result) == ["succeeded", "fired"]
    assert result.steps[0].run_id is not None and result.steps[0].run_status == "succeeded"
    # The settle step moved the head; the action landed in the journal, by its principal.
    assert instance.accepted_coordinate() != base
    (event,) = trigger_events(instance, action="evidence.sweep")
    assert event.sequence == result.steps[1].event_id
    assert counted_line_runs == {SETTLE: 1}
    assert runbook_service.latest_runbook_run(instance, "test.rb") == result


def test_a_stop_on_proposal_halts_every_later_step(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    from cruxible_core.triggers.journal import trigger_events

    instance, root = runbook_world(tmp_path)
    author_runbook(
        instance,
        "test.rb",
        [{"line": PROPOSE}, {"action": "evidence.sweep"}, {"line": SETTLE}],
    )
    base = instance.accepted_coordinate()
    result = run_runbook(instance, root, "test.rb")
    assert (result.status, result.stop_reason, result.stopped_at_step) == (
        "stopped",
        "step_proposed",
        1,
    )
    assert _statuses(result) == ["proposed", "not_run", "not_run"]
    (proposal,) = result.steps[0].proposal_ids
    assert proposal.startswith("sha256:")
    # Nothing after the review ran: no later Line run, no action fire, no settle.
    assert counted_line_runs == {PROPOSE: 1}
    assert trigger_events(instance, action="evidence.sweep") == ()
    assert instance.accepted_coordinate() == base
    # The open proposal is still current on the next run: still review, still a stop.
    again = run_runbook(instance, root, "test.rb", minute=1)
    assert again.stop_reason == "step_proposed"
    assert again.steps[0].proposal_ids == (proposal,)
    assert _statuses(again) == ["proposed", "not_run", "not_run"]


def test_a_failed_step_halts_the_run(tmp_path: Path, counted_line_runs: Counter[str]) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(FAIL, SETTLE))
    base = instance.accepted_coordinate()
    result = run_runbook(instance, root, "test.rb")
    assert (result.status, result.stop_reason, result.stopped_at_step) == (
        "stopped",
        "step_failed",
        1,
    )
    failed, skipped = result.steps
    assert (failed.status, failed.run_status) == ("failed", "node_refused")
    assert failed.code == "workspace_file_read_refused"
    assert skipped.status == "not_run" and skipped.run_id is None
    assert counted_line_runs == {FAIL: 1}
    assert instance.accepted_coordinate() == base


def test_a_refused_step_is_a_failed_step(tmp_path: Path, counted_line_runs: Counter[str]) -> None:
    """A caller below governed write cannot run a proposing Line: that step fails."""

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"line": PROPOSE}, {"action": "evidence.sweep"}])
    result = run_runbook(instance, root, "test.rb", caller_rung=0)
    assert result.stop_reason == "step_failed"
    assert result.steps[0].status == "failed" and result.steps[0].run_id is None
    assert result.steps[1].status == "not_run"


def test_an_action_step_needs_the_action_tier(tmp_path: Path) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"action": "evidence.sweep"}, {"line": SETTLE}])
    result = run_runbook(instance, root, "test.rb", caller_rung=0)
    assert result.stop_reason == "step_failed"
    assert _statuses(result) == ["failed", "not_run"]


def test_a_missing_runbook_refuses(tmp_path: Path) -> None:
    instance, root = runbook_world(tmp_path)
    with pytest.raises(runbook_service.RunbookNotFoundError):
        run_runbook(instance, root, "test.none")


# -- run overhead is constant per step ---------------------------------------------


class _Succeeded:
    """A Line run that succeeded and wrote nothing: the step's own cost is zero."""

    status = "succeeded"
    run_id = "RUN-fixture"
    terminal = None
    terminal_egress = ()


def _overhead(tmp_path: Path, steps: int, monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """What a run reads and writes itself, beyond its steps' own Line runs."""

    from cruxible_core.consumers.state import DisposableState

    tmp_path.mkdir(parents=True)
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"line": SETTLE}] * steps)
    counts: Counter[str] = Counter()
    real_open = DisposableState.open
    real_coordinate = type(instance).accepted_coordinate
    real_bind = type(instance).bind_accepted_projection

    def run_line(instance: Any, **kwargs: Any) -> Any:
        counts["line_runs"] += 1
        return _Succeeded()

    def opened(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts["state_opens"] += 1
        return real_open(self, *args, **kwargs)

    def coordinate(self: Any) -> Any:
        counts["head_reads"] += 1
        return real_coordinate(self)

    def bind(self: Any, *args: Any, **kwargs: Any) -> Any:
        counts["projection_binds"] += 1
        return real_bind(self, *args, **kwargs)

    monkeypatch.setattr(procedure_runs, "service_run_playbill_line", run_line)
    monkeypatch.setattr(DisposableState, "open", opened)
    monkeypatch.setattr(type(instance), "accepted_coordinate", coordinate)
    monkeypatch.setattr(type(instance), "bind_accepted_projection", bind)
    result = run_runbook(instance, root, "test.rb")
    monkeypatch.undo()
    assert result.status == "completed" and len(result.steps) == steps
    return counts


def test_run_overhead_is_constant_per_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    small = _overhead(tmp_path / "five", 5, monkeypatch)
    large = _overhead(tmp_path / "twenty", 20, monkeypatch)
    assert (small["line_runs"], large["line_runs"]) == (5, 20)
    # Everything the run does itself is per run, never per step.
    own = {key: value for key, value in small.items() if key != "line_runs"}
    assert own == {key: value for key, value in large.items() if key != "line_runs"}
    # Measured: two head reads (writability, the Runbook), one bind, one record.
    assert own == {"head_reads": 2, "projection_binds": 1, "state_opens": 1}


# -- a Runbook as a Trigger target -------------------------------------------------


def _fire(instance: Any, *, since: datetime, now: datetime) -> Any:
    from cruxible_core.triggers.journal import evaluate_triggers

    with instance.accepted_history_reader() as history:
        generation = history.sequence
    return evaluate_triggers(
        instance, now=now, listening_since=since, listening_generation=generation
    )


def _drain(instance: Any, root: Path, *, now: datetime) -> list[Any]:
    from cruxible_core.consumers.runbooks import RUNBOOKS

    manager = FakeManager(instance, root)
    work = list(RUNBOOKS.due(instance, now=now))
    for item in work:
        RUNBOOKS.run(manager, "fixture", item, now=now)
    return work


def test_a_trigger_fired_run_stops_on_a_line_that_is_not_enabled(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    from cruxible_core.consumers.runbooks import RUNBOOKS
    from cruxible_core.triggers.journal import internal_triggers

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"action": "evidence.sweep"}, {"line": SETTLE}])
    author_runbook_trigger(
        instance,
        "test.rb-tick",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 60},
    )
    (item,) = (t for t in internal_triggers(instance) if t.trigger == "Trigger:test.rb-tick")
    assert item.action == "runbook:test.rb"
    assert RUNBOOKS.active(instance)
    fires = _fire(instance, since=item.accepted_at, now=item.accepted_at + timedelta(seconds=61))
    assert [event.trigger for event in fires if event.action == "runbook:test.rb"] == [
        "Trigger:test.rb-tick"
    ]
    (work,) = _drain(instance, root, now=item.accepted_at + timedelta(seconds=61))
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None and (last.mode, last.trigger) == ("trigger", "Trigger:test.rb-tick")
    assert (last.stop_reason, last.stopped_at_step) == ("line_not_enabled", 2)
    assert _statuses(last) == ["fired", "not_enabled"]
    assert counted_line_runs == {}
    # The fire is covered once its run is recorded: nothing is due again.
    assert list(RUNBOOKS.due(instance, now=item.accepted_at + timedelta(seconds=62))) == []
    (health,) = RUNBOOKS.health(instance, now=item.accepted_at)
    assert health.consumer_id == "Runbook:test.rb"
    assert health.detail["last_stop_reason"] == "line_not_enabled"


def test_a_trigger_fired_run_runs_each_line_under_its_own_enablement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counted_line_runs: Counter[str]
) -> None:
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        parse_line_spec,
    )
    from cruxible_core.consumers import runbooks as consumer
    from cruxible_core.runtime import line_arms
    from tests.support.operation_world import actor

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE, PROPOSE))
    author_runbook_trigger(
        instance, "test.rb-on-accept", runbook="test.rb", schedule={"kind": "generation_accepted"}
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    digests = {
        name: line_spec_digest(
            parse_line_spec(tree[line_spec_path(name)], path=line_spec_path(name))
        ).tagged
        for name in (SETTLE, PROPOSE)
    }
    principals: list[str] = []

    def arm(instance: Any, line: str) -> dict[str, Any]:
        return {
            "armed_by": {"kind": "local_operator", "label": "operator"},
            "line_artifact_digest": digests[line],
        }

    def authority(instance: Any, principal: Any, *, now: datetime) -> Any:
        principals.append(principal.label)
        return actor(), 3

    monkeypatch.setattr(consumer, "_active_arm", arm)
    monkeypatch.setattr(line_arms, "arm_authority", authority)
    now = datetime.now(UTC)
    from cruxible_core.consumers.runbooks import run_fired_runbook

    result = run_fired_runbook(
        FakeManager(instance, root), "fixture", name="test.rb", trigger=None, now=now
    )
    assert result.mode == "trigger"
    assert _statuses(result) == ["succeeded", "proposed"]
    assert result.stop_reason == "step_proposed"
    assert counted_line_runs == {SETTLE: 1, PROPOSE: 1}
    assert principals == ["operator", "operator"]
