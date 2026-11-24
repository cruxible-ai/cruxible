"""Runbook Procedure fan-out (0.6.0): a ``{"procedure": P}`` step runs every live Line of P.

The step is valid with no Line yet; it expands at the accepted head to P's live
Lines in Line-name order, so a Line added later joins without editing the
Runbook. A run records one Line step per expanded Line (same step number,
naming the Procedure) with the ordinary stop rules, and a step with no Line is
``skipped`` and the run continues. Preview, the ``get`` card and
``resolve_runbook_lines`` expand it the same way, and a Procedure a live Runbook
fans out over cannot be retired.

The world (``tests/support/runbooks.py``): Procedure ``test.override`` with
Lines ``test.override-line`` (settles), ``test.propose-line`` (proposes) and
``test.fail-line`` (refuses); ``_settle_world`` holds only the first.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.authoring.inputs import ProcedureInput, RunbookInput
from cruxible_client.contracts.authoring.models import RunbookStepAuthoring
from cruxible_client.contracts.runbooks import (
    ProcedureTarget,
    Runbook,
    RunbookProcedureState,
    RunbookRunResult,
    evaluate_runbook_law,
    parse_runbook,
    runbook_path,
)
from cruxible_client.contracts.triggers import LineTarget
from cruxible_core.service import runbooks as runbook_service
from cruxible_core.service.procedures import procedure_runs
from tests.support.runbooks import (
    FAIL,
    PROCEDURE,
    PROPOSE,
    SETTLE,
    author,
    author_runbook,
    propose_line,
    readers,
    run_runbook,
    runbook_world,
)

EMPTY = "test.empty"
ZZ = "test.zz-propose"
AA = "test.aa-propose"


def _statuses(result: RunbookRunResult) -> list[str]:
    return [step.status for step in result.steps]


def _targets(result: Any) -> list[str]:
    from cruxible_client.contracts.text_display import runbook_target_text

    return [runbook_target_text(step.target) for step in result.steps]


@pytest.fixture
def counted_line_runs(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    calls: Counter[str] = Counter()
    real = procedure_runs.service_run_playbill_line

    def counting(instance: Any, **kwargs: Any) -> Any:
        calls[kwargs["path_identity_digest"]] += 1
        return real(instance, **kwargs)

    monkeypatch.setattr(procedure_runs, "service_run_playbill_line", counting)
    return calls


def _settle_world(tmp_path: Path) -> tuple[Any, Path]:
    from tests.test_procedures.test_line_run_parameter_override import _settle_world

    return _settle_world(tmp_path)


def _author_empty_procedure(instance: Any) -> None:
    """A second Procedure (the override graph under another name) that no Line runs."""

    from tests.test_procedures.test_line_run_parameter_override import override_spec

    spec = override_spec().model_copy(update={"procedure": EMPTY})
    author(instance, ProcedureInput(kind="procedure", spec=spec), name="empty-procedure")


def _accepted_runbook(instance: Any, name: str) -> Runbook:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return parse_runbook(tree[runbook_path(name)], path=runbook_path(name))


# --- Authoring, law and retirement ------------------------------------------


def test_the_procedure_shorthand_lowers_to_a_procedure_target(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"line": SETTLE}])
    runbook = _accepted_runbook(instance, "test.rb")
    first, second = runbook.steps
    assert first == ProcedureTarget(
        procedure=ArtifactNameRef(target=ArtifactIdentity(kind="Procedure", name=PROCEDURE))
    )
    assert isinstance(second, LineTarget)
    assert runbook.procedures == (ArtifactIdentity(kind="Procedure", name=PROCEDURE),)
    assert runbook.lines == (ArtifactIdentity(kind="Line", name=SETTLE),)


def test_a_step_names_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one of line, action or procedure"):
        RunbookStepAuthoring.model_validate({"line": SETTLE, "procedure": PROCEDURE})
    with pytest.raises(ValueError):
        RunbookStepAuthoring.model_validate({"procedure": "Not A Name"})
    with pytest.raises(ValueError, match="names a Procedure"):
        ProcedureTarget(
            procedure=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=SETTLE))
        )


def test_a_procedure_with_no_line_is_a_valid_step(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    _author_empty_procedure(instance)
    changed = author_runbook(instance, "test.rb", [{"procedure": EMPTY}])
    assert runbook_path("test.rb") in changed
    assert _accepted_runbook(instance, "test.rb").procedures == (
        ArtifactIdentity(kind="Procedure", name=EMPTY),
    )


def test_a_missing_procedure_refuses_at_authoring(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root = _settle_world(tmp_path)
    with pytest.raises(AuthoringLoweringError) as refused:
        author_runbook(instance, "test.rb", [{"procedure": "test.nope"}])
    assert refused.value.code == "cruxible.authoring.runbook_procedure_missing"


def test_the_law_refuses_a_procedure_that_is_not_live() -> None:
    from cruxible_client.contracts.artifacts import ArtifactLifecycle

    runbook = Runbook(
        identity=ArtifactIdentity(kind="Runbook", name="test.rb"),
        steps=(
            ProcedureTarget(
                procedure=ArtifactNameRef(target=ArtifactIdentity(kind="Procedure", name=EMPTY))
            ),
        ),
        lifecycle=ArtifactLifecycle(predecessor_digest=None, state="live"),
    )
    path = runbook_path("test.rb")
    for procedures in ({}, {f"Procedure:{EMPTY}": RunbookProcedureState(live=False)}):
        law = evaluate_runbook_law(
            runbook, path=path, predecessor=None, lines={}, procedures=procedures
        )
        assert law.verdict == "refused"
        assert law.diagnostics[0].code == "cruxible.runbook.procedure_unavailable"
    accepted = evaluate_runbook_law(
        runbook,
        path=path,
        predecessor=None,
        lines={},
        procedures={f"Procedure:{EMPTY}": RunbookProcedureState(live=True)},
    )
    assert accepted.verdict == "accepted"


def test_a_procedure_a_live_runbook_fans_out_over_cannot_be_retired(tmp_path: Path) -> None:
    from tests.test_procedures.test_line_run_parameter_override import override_spec

    instance, _root = _settle_world(tmp_path)
    _author_empty_procedure(instance)
    author_runbook(instance, "test.rb", [{"procedure": EMPTY}])
    spec = override_spec().model_copy(update={"procedure": EMPTY})
    retire = ProcedureInput(kind="procedure", spec=spec, retire=True)
    head = instance.accepted_coordinate()
    with pytest.raises(Exception, match="runbooks_not_settled|strand live Runbooks"):
        author(instance, retire, name="retire-empty")
    assert instance.accepted_coordinate() == head
    # Once the Runbook is retired, the Procedure retires.
    author(
        instance,
        RunbookInput.model_validate({"kind": "runbook", "name": "test.rb", "retire": True}),
        name="retire-rb",
    )
    author(instance, retire, name="retire-empty-after")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    retired = parse_procedure(tree[procedure_path(EMPTY)], path=procedure_path(EMPTY))
    assert retired.lifecycle.state == "retired"


# --- Running -----------------------------------------------------------------


def test_a_procedure_with_no_line_is_skipped_and_the_run_continues(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    instance, root = _settle_world(tmp_path)
    _author_empty_procedure(instance)
    author_runbook(instance, "test.rb", [{"procedure": EMPTY}, {"line": SETTLE}])
    result = run_runbook(instance, root, "test.rb")
    assert (result.status, result.stop_reason) == ("completed", None)
    assert _statuses(result) == ["skipped", "succeeded"]
    skipped = result.steps[0]
    assert isinstance(skipped.target, ProcedureTarget)
    assert skipped.code == runbook_service.NO_LINES_BOUND
    assert skipped.message is not None and f"Procedure:{EMPTY}" in skipped.message
    assert counted_line_runs == {SETTLE: 1}
    text = str(result)
    assert f"skipped     Procedure:{EMPTY}" in text and "no live Lines run" in text


def test_a_procedure_with_one_line_runs_it(tmp_path: Path, counted_line_runs: Counter[str]) -> None:
    instance, root = _settle_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}])
    result = run_runbook(instance, root, "test.rb")
    assert result.status == "completed"
    (step,) = result.steps
    assert (step.step, step.status, step.from_procedure) == (1, "succeeded", PROCEDURE)
    assert step.target == LineTarget(
        line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=SETTLE))
    )
    assert step.run_id is not None
    assert counted_line_runs == {SETTLE: 1}
    assert f"via Procedure:{PROCEDURE}" in str(result)


def test_a_line_added_later_joins_without_editing_the_runbook(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    instance, root = _settle_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"action": "evidence.sweep"}])
    before = _accepted_runbook(instance, "test.rb")
    first = run_runbook(instance, root, "test.rb")
    assert _targets(first) == [f"Line:{SETTLE}", "action:evidence.sweep"]
    propose_line(instance, ZZ)
    assert _accepted_runbook(instance, "test.rb") == before
    second = run_runbook(instance, root, "test.rb", minute=1)
    # Name order: the settle Line first, then the new proposing Line, which stops the run.
    assert _targets(second) == [f"Line:{SETTLE}", f"Line:{ZZ}", "action:evidence.sweep"]
    assert _statuses(second) == ["succeeded", "proposed", "not_run"]
    assert [step.step for step in second.steps] == [1, 1, 2]
    assert [step.from_procedure for step in second.steps] == [PROCEDURE, PROCEDURE, None]
    assert (second.stop_reason, second.stopped_at_step) == ("step_proposed", 1)


def test_a_proposal_mid_fan_out_stops_the_siblings_after_it(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    instance, root = _settle_world(tmp_path)
    propose_line(instance, AA)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"line": SETTLE}])
    result = run_runbook(instance, root, "test.rb")
    assert _targets(result) == [f"Line:{AA}", f"Line:{SETTLE}", f"Line:{SETTLE}"]
    assert _statuses(result) == ["proposed", "not_run", "not_run"]
    assert result.steps[1].from_procedure == PROCEDURE and result.steps[2].from_procedure is None
    assert (result.stop_reason, result.stopped_at_step) == ("step_proposed", 1)
    assert counted_line_runs == {AA: 1}


def test_a_failure_mid_fan_out_stops_the_run(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"procedure": PROCEDURE}])
    result = run_runbook(instance, root, "test.rb")
    # Sorted: test.fail-line < test.override-line < test.propose-line.
    assert _targets(result) == [
        f"Line:{FAIL}",
        f"Line:{SETTLE}",
        f"Line:{PROPOSE}",
        f"Procedure:{PROCEDURE}",
    ]
    assert _statuses(result) == ["failed", "not_run", "not_run", "not_run"]
    assert (result.stop_reason, result.stopped_at_step) == ("step_failed", 1)
    assert counted_line_runs == {FAIL: 1}


def test_attention_steps_name_a_fanned_out_step_once() -> None:
    from datetime import UTC, datetime

    from cruxible_client.contracts.procedures.node_summary import RunCounts
    from cruxible_client.contracts.runbooks import RunbookStepOutcome

    counts = RunCounts(written=1, refused=1, complete=True, needs_attention=True)
    assert counts.needs_attention
    line = LineTarget(line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=SETTLE)))
    steps = tuple(
        RunbookStepOutcome(
            step=1, target=line, status="succeeded", counts=counts, from_procedure=PROCEDURE
        )
        for _ in range(2)
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    result = RunbookRunResult(
        run_id="RBR-x",
        runbook="test.rb",
        runbook_artifact_digest="sha256:" + "0" * 64,
        mode="manual",
        started_at=now,
        finished_at=now,
        status="completed",
        steps=steps,
        attention_steps=(1,),
    )
    assert result.attention_steps == (1,)


# --- Trigger-fired -------------------------------------------------------------


def test_a_trigger_fired_fan_out_runs_each_line_under_its_enablement_and_replays_once(
    tmp_path: Path, counted_line_runs: Counter[str]
) -> None:
    from datetime import UTC, datetime

    from cruxible_core.consumers.runbooks import RUNBOOKS
    from cruxible_core.triggers.journal import internal_triggers, retained_records
    from tests.support.runbooks import author_runbook_trigger
    from tests.test_runbooks.test_runbook_runs import _drain, _enable, _fire

    instance, root = _settle_world(tmp_path)
    propose_line(instance, ZZ)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}])
    author_runbook_trigger(
        instance,
        "test.rb-tick",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 60},
    )
    now = datetime.now(UTC)
    _enable(instance, SETTLE, now=now)
    (item,) = (t for t in internal_triggers(instance) if t.trigger == "Trigger:test.rb-tick")
    _fire(instance, since=item.accepted_at, now=item.accepted_at + timedelta(seconds=61))
    _drain(instance, root, now=now)
    first = runbook_service.latest_runbook_run(instance, "test.rb")
    assert first is not None and first.mode == "trigger"
    # The settle Line is enabled and runs; the new Line is not, and stops the run.
    assert _targets(first) == [f"Line:{SETTLE}", f"Line:{ZZ}"]
    assert _statuses(first) == ["succeeded", "not_enabled"]
    assert (first.stop_reason, first.stopped_at_step) == ("line_not_enabled", 1)
    assert counted_line_runs == {SETTLE: 1}
    # Lose the run record and un-finish the fire: the replay reads the settle
    # Line's step back (its own record key) and never runs it again.
    _enable(instance, ZZ, now=now)
    runbook_service.RUNBOOK_STATE.reset(instance)
    with retained_records(instance, write=True) as connection:
        assert connection is not None
        connection.execute("UPDATE runbook_fires SET covered=0")
    _drain(instance, root, now=now + timedelta(seconds=5))
    replay = runbook_service.latest_runbook_run(instance, "test.rb")
    assert replay is not None and replay.run_id == first.run_id
    assert _statuses(replay) == ["succeeded", "proposed"]
    assert replay.steps[0].run_id == first.steps[0].run_id
    assert counted_line_runs == {SETTLE: 1, ZZ: 1}
    assert list(RUNBOOKS.due(instance, now=now)) == []


# --- Preview -------------------------------------------------------------------


def _preview(instance: Any, root: Path, name: str = "test.rb") -> Any:
    from cruxible_core.service.runbook_preview import service_preview_runbook

    return service_preview_runbook(
        instance, name=name, workspace_file_reader=readers(instance, root)["workspace_file_reader"]
    )


def test_preview_lists_a_procedure_with_no_line_and_flags_reads_of_its_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts.runbooks import RunbookPreviewRead
    from cruxible_core.service import runbook_preview

    instance, root = _settle_world(tmp_path)
    _author_empty_procedure(instance)
    author_runbook(instance, "test.rb", [{"procedure": EMPTY}, {"line": SETTLE}])
    preview = _preview(instance, root)
    first, second = preview.steps
    assert isinstance(first.target, ProcedureTarget) and not first.unavailable
    assert first.procedure == f"Procedure:{EMPTY}" and first.writes
    assert first.reads == () and first.inputs == ()
    assert isinstance(second.target, LineTarget) and second.from_procedure is None
    text = str(preview)
    assert f"Procedure:{EMPTY}  (0 lines configured)" in text
    assert "would write" in text
    assert preview.order_problems == ()
    # A later Line step reading what only the unbound step would write: an
    # order problem naming that step. The fixture's two Procedures share one
    # graph, so the Line step stands in for a reader: it reads the ClaimType
    # and writes nothing (only the first ``_writes``, step 1's, is real).
    written = first.writes[0].claim_types[0]
    real_writes = runbook_preview._writes
    calls: list[int] = []

    def first_writes_only(definition: Any) -> Any:
        calls.append(1)
        return real_writes(definition) if len(calls) == 1 else ()

    monkeypatch.setattr(runbook_preview, "_writes", first_writes_only)
    monkeypatch.setattr(
        runbook_preview,
        "_reads",
        lambda reader, definition: (RunbookPreviewRead(node_id="read", claim_types=(written,)),),
    )
    flagged = _preview(instance, root)
    (problem,) = (item for item in flagged.order_problems if item.claim_type == written)
    assert (problem.kind, problem.step, problem.written_by_steps) == (
        "reads_unbound_write",
        2,
        (1,),
    )
    assert f"order: step 2 reads {written}: written only by step(s) 1, which run no Line" in (
        str(flagged)
    )


def test_preview_expands_a_procedure_to_every_live_line_in_name_order(tmp_path: Path) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"action": "evidence.sweep"}])
    preview = _preview(instance, root)
    assert _targets(preview) == [
        f"Line:{FAIL}",
        f"Line:{SETTLE}",
        f"Line:{PROPOSE}",
        "action:evidence.sweep",
    ]
    assert [step.step for step in preview.steps] == [1, 1, 1, 2]
    assert [step.from_procedure for step in preview.steps[:3]] == [PROCEDURE] * 3
    for step in preview.steps[:3]:
        assert step.procedure == f"Procedure:{PROCEDURE}" and step.writes
    # Each expanded Line's workspace input, resolved from its own parameters.
    paths = [(item.location, item.state) for step in preview.steps[:3] for item in step.inputs]
    assert [location for location, _state in paths] == [
        "data/missing.json",
        "data/doc.json",
        "data/other.json",
    ]
    assert [state for _location, state in paths] == ["absent", "present", "present"]
    assert f"(from Procedure:{PROCEDURE})" in str(preview)


def test_preview_with_one_line(tmp_path: Path) -> None:
    instance, root = _settle_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}])
    (step,) = _preview(instance, root).steps
    assert step.target == LineTarget(
        line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=SETTLE))
    )
    assert step.from_procedure == PROCEDURE and step.authority is not None


# --- Reads: resolve_runbook_lines, get card, orient ------------------------------


def test_resolve_runbook_lines_expands_procedure_steps_in_order(tmp_path: Path) -> None:
    instance, _root = runbook_world(tmp_path)
    author_runbook(
        instance,
        "test.rb",
        [{"line": SETTLE}, {"action": "evidence.sweep"}, {"procedure": PROCEDURE}],
    )
    assert runbook_service.resolve_runbook_lines(instance, "test.rb") == (SETTLE, FAIL, PROPOSE)
    with pytest.raises(runbook_service.RunbookNotFoundError):
        runbook_service.resolve_runbook_lines(instance, "test.nope")


def test_resolve_runbook_lines_with_no_line_bound(tmp_path: Path) -> None:
    instance, _root = _settle_world(tmp_path)
    _author_empty_procedure(instance)
    author_runbook(instance, "test.rb", [{"procedure": EMPTY}])
    assert runbook_service.resolve_runbook_lines(instance, "test.rb") == ()
    propose_line(instance, ZZ)
    assert runbook_service.resolve_runbook_lines(instance, "test.rb") == ()


def test_the_get_card_and_orient_render_procedure_steps(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.runbook_reads import runbook_card, runbook_rows

    instance, _root = runbook_world(tmp_path)
    _author_empty_procedure(instance)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}, {"procedure": EMPTY}])
    coordinate = instance.accepted_coordinate()
    card = runbook_card(
        instance,
        coordinate,
        "Runbook:test.rb",
        render=lambda ref, detail: ref if detail is None else f"{ref} {detail}",
    )
    fan, empty = card.steps
    assert (fan.target, fan.availability) == (f"Procedure:{PROCEDURE}", "available")
    assert fan.lines == (f"Line:{FAIL}", f"Line:{SETTLE}", f"Line:{PROPOSE}")
    assert (empty.target, empty.availability, empty.lines) == (
        f"Procedure:{EMPTY}",
        "no_lines_bound",
        (),
    )
    assert f"Line:{SETTLE}" in card.next
    rows, _after = runbook_rows(instance, coordinate, limit=10, after=None)
    (row,) = (item for item in rows if item.runbook == "Runbook:test.rb")
    assert row.steps == 2


def test_a_line_card_names_a_triggered_fan_out_runbook(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.operational import runbook_trigger_names
    from tests.support.runbooks import author_runbook_trigger

    instance, _root = _settle_world(tmp_path)
    author_runbook(instance, "test.rb", [{"procedure": PROCEDURE}])
    author_runbook_trigger(
        instance,
        "test.rb-tick",
        runbook="test.rb",
        schedule={"kind": "cadence", "interval_seconds": 60},
    )
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        names = runbook_trigger_names(projection, f"Line:{SETTLE}", limit=2)
    assert names == ("Trigger:test.rb-tick",)
