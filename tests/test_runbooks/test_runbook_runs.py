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
    # The settle step.s counts say what it achieved: written and settled, nothing refused.
    counts = result.steps[0].counts
    assert counts is not None and counts.complete and not counts.needs_attention
    assert counts.written >= 1 and counts.settled == counts.written and counts.proposed == 0
    assert (counts.refused, counts.inbox) == (0, 0)
    assert result.steps[1].counts is None and result.attention_steps == ()


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
    proposed = result.steps[0].counts
    assert proposed is not None and proposed.proposed == proposed.written >= 1
    assert proposed.settled == 0
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
    # Setup's own writes each fire a background workspace-advertisement
    # (``advertise_workspace``, fire-and-forget) that reads the accepted
    # coordinate on its own thread. Drain it before opening the counted
    # window: on a slower or busier host it can still be running once the
    # monkeypatches below are in place, and its reads would be counted as
    # this run's own overhead, making the measurement flaky rather than
    # reflecting what the run itself does.
    instance.settled_workspace_advertisement()
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


#: Enabled as the fixture's registered principal, rechecked before every admission.
OWNER_CLAIM = {"kind": "principal_claim", "label": "owner"}


def _enable(instance: Any, line: str, *, now: datetime) -> None:
    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
    from cruxible_core.service.procedures.line_dispatch import service_enable_line
    from tests.support.operation_world import actor

    service_enable_line(
        instance,
        line,
        principal=LineEnablementPrincipal.model_validate(OWNER_CLAIM),
        actor=actor(),
        now=now,
        daemon_id="runbook-tests",
    )


def _fired_world(tmp_path: Path, steps: list[dict[str, str]]) -> tuple[Any, Path, datetime]:
    """A Runbook with a cadence Trigger, its Lines enabled, and one fire in the journal."""

    instance, root = runbook_world(tmp_path)
    return _fire_runbook(instance, root, steps)


def _fire_runbook(
    instance: Any, root: Path, steps: list[dict[str, str]]
) -> tuple[Any, Path, datetime]:
    from cruxible_core.triggers.journal import internal_triggers

    author_runbook(instance, "test.rb", steps)
    author_runbook_trigger(
        instance,
        "test.rb-tick",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 60},
    )
    now = datetime.now(UTC)
    for step in steps:
        if "line" in step:
            _enable(instance, step["line"], now=now)
    (item,) = (t for t in internal_triggers(instance) if t.trigger == "Trigger:test.rb-tick")
    _fire(instance, since=item.accepted_at, now=item.accepted_at + timedelta(seconds=61))
    return instance, root, now


def _action_fires(instance: Any, action: str = "evidence.sweep") -> int:
    from cruxible_core.triggers.journal import trigger_events

    # Only the Runbook's own fires: a seeded Trigger fires the same action too.
    return sum(
        event.trigger == "Trigger:test.rb-tick" for event in trigger_events(instance, action=action)
    )


def test_a_trigger_fired_run_runs_each_line_under_its_own_enablement(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    instance, root, now = _fired_world(
        tmp_path, [{"line": SETTLE}, {"action": "evidence.sweep"}, {"line": PROPOSE}]
    )
    (work,) = _drain(instance, root, now=now)
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None and last.mode == "trigger"
    assert _statuses(last) == ["succeeded", "fired", "proposed"]
    assert counted_line_runs == {SETTLE: 1, PROPOSE: 1}
    assert _action_fires(instance) == 1


def test_a_replayed_fire_never_runs_a_step_again(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    """F1: progress lost after the run (crash before it was covered, or any rebuild of
    the disposable records) replays the same fire, which reads every step back."""

    from cruxible_core.consumers.runbooks import RUNBOOKS
    from cruxible_core.triggers.journal import retained_records

    instance, root, now = _fired_world(tmp_path, [{"line": SETTLE}, {"action": "evidence.sweep"}])
    _drain(instance, root, now=now)
    first = runbook_service.latest_runbook_run(instance, "test.rb")
    assert first is not None and _statuses(first) == ["succeeded", "fired"]
    head = instance.accepted_coordinate()
    # Lose everything rebuildable and un-finish the fire, as a crash before its
    # finish would leave it.
    runbook_service.RUNBOOK_STATE.reset(instance)
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        connection.execute("UPDATE runbook_fires SET covered=0")
    assert [work.key for work in RUNBOOKS.due(instance, now=now)] == ["runbook:test.rb"]
    _drain(instance, root, now=now + timedelta(seconds=5))
    replay = runbook_service.latest_runbook_run(instance, "test.rb")
    assert replay is not None and replay.run_id == first.run_id
    assert _statuses(replay) == ["succeeded", "fired"]
    assert replay.steps[0].run_id == first.steps[0].run_id
    assert replay.steps[1].event_id == first.steps[1].event_id
    # Nothing ran again: no second Line occurrence, no second fire, no new head.
    assert counted_line_runs == {SETTLE: 1}
    assert _action_fires(instance) == 1
    assert instance.accepted_coordinate() == head
    assert list(RUNBOOKS.due(instance, now=now)) == []


def test_a_crash_mid_run_resumes_the_same_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counted_line_runs: Counter[str]
) -> None:
    """F1: a run that dies after step 1 resumes the fire it claimed: step 1 is read back."""

    from cruxible_core.triggers import journal

    instance, root, now = _fired_world(tmp_path, [{"line": SETTLE}, {"action": "evidence.sweep"}])
    real = journal.fire_runbook_action

    def crash(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("the daemon died here")

    monkeypatch.setattr(journal, "fire_runbook_action", crash)
    with pytest.raises(RuntimeError):
        _drain(instance, root, now=now)
    monkeypatch.setattr(journal, "fire_runbook_action", real)
    # The fire stays claimed and unfinished, so it is the one resumed.
    (claimed,) = journal.pending_runbook_fires(instance)
    assert claimed.started > claimed.covered
    _drain(instance, root, now=now + timedelta(seconds=5))
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None and _statuses(last) == ["succeeded", "fired"]
    assert last.run_id == runbook_service.fired_run_id("test.rb", claimed.started)
    assert counted_line_runs == {SETTLE: 1}
    assert _action_fires(instance) == 1


def test_a_replay_runs_the_claimed_plan_not_a_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1a: a Runbook succeeded between a crash and its replay changes nothing the fire runs.

    The fire's exact ordered plan is retained with its claim; the replay runs that
    plan, reads step 1 back and fires step 2 once. The successor's reordering neither
    re-runs the completed action at its new position nor skips the other.
    """

    from cruxible_client.contracts.runbooks import parse_runbook, runbook_digest, runbook_path
    from cruxible_core.triggers import journal

    instance, root, now = _fired_world(
        tmp_path, [{"action": "evidence.sweep"}, {"action": "curation.detect"}]
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)

    original = runbook_digest(
        parse_runbook(tree[runbook_path("test.rb")], path=runbook_path("test.rb"))
    ).tagged
    real = journal.fire_runbook_action

    def crash_on_curation(instance: Any, **kwargs: Any) -> int:
        if kwargs["action"] == "curation.detect":
            raise RuntimeError("the daemon died here")
        return real(instance, **kwargs)

    monkeypatch.setattr(journal, "fire_runbook_action", crash_on_curation)
    with pytest.raises(RuntimeError):
        _drain(instance, root, now=now)
    monkeypatch.setattr(journal, "fire_runbook_action", real)
    assert (_action_fires(instance), _action_fires(instance, "curation.detect")) == (1, 0)
    # A successor reorders the steps before the replay.
    author_runbook(
        instance, "test.rb", [{"action": "curation.detect"}, {"action": "evidence.sweep"}]
    )
    _drain(instance, root, now=now + timedelta(seconds=5))
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None and last.runbook_artifact_digest == original
    assert [step.target.action for step in last.steps] == ["evidence.sweep", "curation.detect"]
    assert _statuses(last) == ["fired", "fired"]
    # Each action fired exactly once for this fire: no reuse, no repeat, no skip.
    assert (_action_fires(instance), _action_fires(instance, "curation.detect")) == (1, 1)
    assert list(journal.pending_runbook_fires(instance)) == []


def test_a_claimed_step_whose_target_is_gone_stops_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1a: a step of the claimed plan whose Line no longer exists stops, typed."""

    from cruxible_client.contracts.authoring.inputs import LineInput
    from cruxible_core.triggers import journal
    from tests.support.operation_world import author_line
    from tests.support.runbooks import PROCEDURE, author

    observe = "test.observe-line"
    instance, root = runbook_world(tmp_path)
    author_line(
        instance,
        name=observe,
        procedure=PROCEDURE,
        parameters={"path": "data/doc.json"},
        max_authority="observe",
        label=observe,
    )
    instance, root, now = _fire_runbook(
        instance, root, [{"line": observe}, {"action": "evidence.sweep"}]
    )

    def crash(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the daemon died here")

    monkeypatch.setattr(procedure_runs, "service_run_playbill_line", crash)
    with pytest.raises(RuntimeError):
        _drain(instance, root, now=now)
    monkeypatch.undo()
    author(
        instance,
        LineInput.model_validate(
            {
                "kind": "line",
                "name": observe,
                "procedure_name": PROCEDURE,
                "parameters": {"path": "data/doc.json"},
                "max_authority": "observe",
                "retire": True,
            }
        ),
        name="retire-observe",
    )
    _drain(instance, root, now=now + timedelta(seconds=5))
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None
    assert (last.stop_reason, last.stopped_at_step) == ("target_unavailable", 1)
    assert _statuses(last) == ["unavailable", "not_run"]
    assert last.steps[0].code == "target_unavailable"
    assert _action_fires(instance) == 0
    assert list(journal.pending_runbook_fires(instance)) == []


def test_a_replay_finds_a_completed_step_across_line_epochs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counted_line_runs: Counter[str]
) -> None:
    """F1c: a Line step completes, the daemon dies, the Line is succeeded through
    epochs 2 and 3; the replay finds the epoch-1 admission and never runs it again."""

    from cruxible_client.contracts.authoring.inputs import LineInput
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, parse_line_spec
    from cruxible_core.triggers import journal
    from cruxible_core.triggers.journal import runbook_step_record
    from tests.support.runbooks import author

    instance, root, now = _fired_world(tmp_path, [{"line": SETTLE}, {"action": "evidence.sweep"}])
    real = journal.fire_runbook_action

    def crash(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("the daemon died here")

    monkeypatch.setattr(journal, "fire_runbook_action", crash)
    with pytest.raises(RuntimeError):
        _drain(instance, root, now=now)
    monkeypatch.setattr(journal, "fire_runbook_action", real)
    assert counted_line_runs == {SETTLE: 1}
    recorded = runbook_step_record(instance, "runbook:test.rb", _fire_of(instance), 1)
    assert recorded is not None
    # Legal successors: binding the triggering Capture moves the epoch to 2, and
    # returning to manual runs moves it to 3.
    for epoch, trigger_input in ((2, "doc.capture"), (3, None)):
        author(
            instance,
            LineInput.model_validate(
                {
                    "kind": "line",
                    "name": SETTLE,
                    "procedure_name": "test.override",
                    "parameters": {"path": "data/doc.json"},
                    "max_authority": "settle",
                    "trigger_input": trigger_input,
                    "occurrence_epoch": epoch,
                }
            ),
            name=f"settle-epoch-{epoch}",
        )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(SETTLE)], path=line_spec_path(SETTLE))
    assert line.occurrence_epoch == 3
    _drain(instance, root, now=now + timedelta(seconds=5))
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None and _statuses(last) == ["succeeded", "fired"]
    # The step was read back from its epoch-1 admission, not run under epoch 3.
    assert counted_line_runs == {SETTLE: 1}
    assert runbook_step_record(instance, "runbook:test.rb", _fire_of(instance), 1) == recorded
    assert _action_fires(instance) == 1


def test_a_line_disabled_before_its_admission_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counted_line_runs: Counter[str]
) -> None:
    """F2: the step is admitted inside the arm boundary only while the arm stands."""

    from cruxible_core.runtime import line_arms
    from cruxible_core.service.procedures.line_dispatch import service_disable_line
    from tests.support.operation_world import actor

    instance, root, now = _fired_world(tmp_path, line_steps(SETTLE, PROPOSE))
    real = line_arms.arm_authority

    def disabled_meanwhile(instance: Any, principal: Any, *, now: datetime) -> Any:
        # The arm snapshot and its recheck both held; the Line is disabled
        # between them and the admission.
        resolved = real(instance, principal, now=now)
        service_disable_line(instance, SETTLE, actor=actor(), now=now)
        return resolved

    monkeypatch.setattr(line_arms, "arm_authority", disabled_meanwhile)
    head = instance.accepted_coordinate()
    _drain(instance, root, now=now)
    last = runbook_service.latest_runbook_run(instance, "test.rb")
    assert last is not None
    assert (last.stop_reason, last.stopped_at_step) == ("line_not_enabled", 1)
    assert last.steps[0].code == "enablement_ended"
    assert _statuses(last) == ["not_enabled", "not_run"]
    assert last.steps[0].run_id is None
    assert instance.accepted_coordinate() == head
    # Nothing was admitted for the step: a replay would find no run to read back.
    from cruxible_core.service.procedures import procedure_runs
    from cruxible_core.triggers.journal import runbook_step_record

    recorded = runbook_step_record(instance, "runbook:test.rb", _fire_of(instance), 1)
    assert recorded is not None and recorded[0] == f"Line:{SETTLE}"
    assert procedure_runs.admitted_runbook_step(instance, SETTLE, recorded[1]) is None


def _fire_of(instance: Any) -> int:
    from cruxible_core.triggers.journal import retained_records

    with retained_records(instance, write=False) as connection:
        assert connection is not None
        (started,) = connection.execute(
            "SELECT started FROM runbook_fires WHERE fire_key='runbook:test.rb'"
        ).fetchone()
    return int(started)


# -- an idle poll reads no fire history (F3) ------------------------------------------


def _due_vm_steps(instance: Any, monkeypatch: pytest.MonkeyPatch) -> int:
    """SQLite VM steps one idle ``due()`` poll runs on the trigger journal."""

    import sqlite3

    from cruxible_core.consumers.runbooks import RUNBOOKS
    from cruxible_core.triggers import journal

    steps = 0
    real_connect = sqlite3.connect

    def counting(*args: Any, **kwargs: Any) -> Any:
        connection = real_connect(*args, **kwargs)

        def step() -> int:
            nonlocal steps
            steps += 1
            return 0

        connection.set_progress_handler(step, 1)
        return connection

    monkeypatch.setattr(journal.sqlite3, "connect", counting)
    try:
        assert list(RUNBOOKS.due(instance, now=datetime.now(UTC))) == []
    finally:
        monkeypatch.setattr(journal.sqlite3, "connect", real_connect)
    return steps


def test_an_idle_poll_is_constant_in_retained_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.triggers.journal import (
        _Fire,
        _open,
        _record,
        finish_runbook_fire,
        pending_runbook_fires,
    )

    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"action": "evidence.sweep"}])
    author_runbook_trigger(
        instance,
        "test.rb-tick",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 60},
    )
    counts: dict[int, int] = {}
    fired = 0
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for history in (100, 1000):
        with _open(instance, create=True) as connection:
            assert connection is not None
            _record(
                connection,
                [
                    _Fire("runbook:test.rb", "Trigger:test.rb-tick", start, f"tick:{index}")
                    for index in range(fired, history)
                ],
                now=start,
            )
        fired = history
        (pending,) = pending_runbook_fires(instance)
        finish_runbook_fire(instance, "runbook:test.rb", pending.latest)
        _due_vm_steps(instance, monkeypatch)  # warm: the Trigger set is read once per head
        counts[history] = _due_vm_steps(instance, monkeypatch)
    assert counts[1000] == counts[100], counts


def test_a_completed_run_names_the_steps_whose_counts_need_attention() -> None:
    """A step that "succeeded" but refused every item or read an incomplete source is
    named in ``attention_steps``; the status stays ``completed``."""

    from types import SimpleNamespace

    from cruxible_client.contracts.claim_evidence import (
        ClaimsDiagnostic,
        CompletenessReason,
        RunCompleteness,
    )
    from cruxible_client.contracts.procedures.node_summary import (
        ClaimsRunSummary,
        ExtractRunSummary,
        run_counts,
    )
    from cruxible_client.contracts.runbooks import (
        AcceptedRunbook,
        Runbook,
        runbook_digest,
        runbook_path,
    )

    def claims(written: int, skipped: dict[str, int], delivery: str) -> ClaimsRunSummary:
        return ClaimsRunSummary.model_validate(
            {
                "written": written,
                "revised": 0,
                "inverse_written": 0,
                "subjects_created": 0,
                "re_observed": 0,
                "no_op": 0,
                "retired": 0,
                "surfaced": 0,
                "downgraded": 0,
                "skipped": skipped,
                "delivery": delivery,
            }
        )

    refused = run_counts(
        [claims(2, {"extract_refused": 9}, "settled")],
        findings=[
            ClaimsDiagnostic(kind="extract_refused", target="t", problem="value_invalid")
            for _ in range(9)
        ],
        inbox=9,
    )
    assert (refused.written, refused.settled, refused.refused, refused.inbox) == (2, 2, 9, 9)
    assert [(item.code, item.count) for item in refused.top_refusals] == [
        ("extract_refused.value_invalid", 9)
    ]
    assert refused.complete and refused.needs_attention
    incomplete = run_counts(
        [
            ExtractRunSummary(
                records=0, invalid_records=0, duplicates=0, invalid_fields=0, complete=False
            ),
            claims(0, {}, "empty"),
        ],
        completeness=RunCompleteness(
            reasons=(CompletenessReason(node="scores", kind="count_mismatch"),)
        ),
    )
    assert not incomplete.complete and incomplete.needs_attention
    assert incomplete.written == 0
    clean = run_counts([claims(3, {}, "settled")])
    assert not clean.needs_attention

    def state(counts: Any) -> Any:
        return SimpleNamespace(
            run_id="RUN-x", status="succeeded", terminal_egress=(), counts=counts, terminal=None
        )

    tallies = {"test.a": clean, "test.b": refused, "test.c": incomplete}
    runbook = Runbook.model_validate(
        {
            "identity": {"kind": "Runbook", "name": "test.rb"},
            "steps": [
                {"kind": "line", "line": {"target": {"kind": "Line", "name": n}}}
                for n in ("test.a", "test.b", "test.c")
            ],
        }
    )
    accepted = AcceptedRunbook(
        path=runbook_path("test.rb"),
        runbook=runbook,
        artifact_digest=runbook_digest(runbook).tagged,
    )
    result = runbook_service.execute_runbook(
        accepted,
        mode="manual",
        run_line=lambda line, _step: state(tallies[line]),
        fire_action=lambda action, _step: 1,
        clock=lambda: datetime(2026, 11, 9, tzinfo=UTC),
    )
    assert result.status == "completed"
    assert [step.status for step in result.steps] == ["succeeded"] * 3
    assert result.attention_steps == (2, 3)
    assert RunbookRunResult.model_validate(result.model_dump(mode="json")) == result

    # The CLI prints each step's counts and names the steps needing attention.
    import click
    from click.testing import CliRunner

    from cruxible_core.cli.commands import playbill as playbill_cli

    @click.command()
    def show() -> None:
        playbill_cli._echo_runbook_run(result)

    output = CliRunner().invoke(show).output
    assert "completed; needs attention: steps 2, 3" in output
    assert "! written 2 (settled 2, proposed 0); unchanged 0; refused 9 " in output
    assert "(extract_refused.value_invalid 9); inbox 9" in output
    assert "INCOMPLETE (scores count_mismatch)" in output
