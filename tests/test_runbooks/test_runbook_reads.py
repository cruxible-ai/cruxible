"""Runbook reads (D-R48): the ``get Runbook:<name>`` card and ``orient(section="runbooks")``.

The card names each step with its target and whether it can run now, the
Triggers aimed at the Runbook, and its recent runs (bounded, newest first, each
with every step's outcome) from the retained run records. orient counts the
live Runbooks and pages them by name. Every read is an index seek: the work of
one ``get`` is the same beside 10, 100 or 1000 unrelated Runbooks and runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
)
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.operational_reads import GetRunbookCard
from cruxible_client.contracts.runbooks import (
    Runbook,
    RunbookRunResult,
    RunbookStepOutcome,
    render_runbook,
    runbook_path,
)
from cruxible_client.contracts.triggers import ActionTarget, LineTarget
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.runbooks import record_runbook_run
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._identity_support import accept_tree
from tests.support.runbooks import (
    PROPOSE,
    SETTLE,
    author_runbook,
    author_runbook_trigger,
    run_runbook,
    runbook_world,
)

ACCESS = BodyAccessContext(principal_id="owner", can_read_body=True)
START = "test.start"


def _get(instance: Any, ref: str, **request: Any) -> Any:
    return service_playbill_get(instance, request=GetRequest(ref=ref, **request), access=ACCESS)


def _line(name: str) -> LineTarget:
    return LineTarget(line=ArtifactNameRef(target=ArtifactIdentity(kind="Line", name=name)))


def _start_world(tmp_path: Path) -> tuple[Any, Path]:
    instance, root = runbook_world(tmp_path)
    author_runbook(
        instance,
        START,
        [{"line": SETTLE}, {"action": "evidence.sweep"}, {"line": PROPOSE}],
        description="Seed the fixture sources in order.",
    )
    author_runbook_trigger(
        instance,
        "test.start-daily",
        runbook=START,
        schedule={"kind": "cron", "expression": "0 6 * * *"},
    )
    return instance, root


def test_the_card_shows_steps_triggers_and_recent_runs(tmp_path: Path) -> None:
    instance, root = _start_world(tmp_path)

    before = _get(instance, f"Runbook:{START}")
    card = before.card
    assert isinstance(card, GetRunbookCard)
    assert before.kind == "runbook"
    assert card.runbook == f"Runbook:{START}"
    assert card.description == "Seed the fixture sources in order."
    assert card.lifecycle == "live"
    assert [(step.step, step.target, step.availability) for step in card.steps] == [
        (1, f"Line:{SETTLE}", "available"),
        (2, "action:evidence.sweep", "available"),
        (3, f"Line:{PROPOSE}", "available"),
    ]
    assert [item.trigger for item in card.triggers] == ["Trigger:test.start-daily"]
    assert "0 6 * * *" in card.triggers[0].fires
    assert card.triggers_total == 1
    assert card.recent_runs == () and card.runs_more is False
    assert before.live is not None and set(before.live.fields) == {"recent_runs", "runs_more"}

    first = run_runbook(instance, root, START, minute=1)
    second = run_runbook(instance, root, START, minute=2)
    card = _get(instance, f"Runbook:{START}").card
    assert isinstance(card, GetRunbookCard)
    # Newest first, each with every step's outcome (the run stopped at review).
    assert [run.run_id for run in card.recent_runs] == [second.run_id, first.run_id]
    assert card.recent_runs[0] == second
    assert any(step.run_id for step in card.recent_runs[0].steps)
    assert any(SETTLE in ref for ref in card.next)


def test_a_bare_runbook_name_and_history_and_proof_resolve(tmp_path: Path) -> None:
    instance, _root = _start_world(tmp_path)
    assert _get(instance, START).kind == "runbook"
    history = _get(instance, f"Runbook:{START}", detail="history")
    assert history.history is not None and len(history.history.revisions) == 1
    proof = _get(instance, f"Runbook:{START}", detail="proof")
    assert proof.proof["path"] == runbook_path(START)
    from cruxible_core.service.read_refusals import ReadRefusalError

    with pytest.raises(ReadRefusalError) as missing:
        _get(instance, "Runbook:test.no-such")
    assert missing.value.error_code == "cruxible.get.ref_not_found"
    assert f"Runbook:{START}" in missing.value.candidates


def test_the_card_bounds_its_runs_and_says_more_are_retained(tmp_path: Path) -> None:
    instance, _root = _start_world(tmp_path)
    runs = _record_runs(instance, START, 8)
    card = _get(instance, f"Runbook:{START}").card
    assert isinstance(card, GetRunbookCard)
    assert [run.run_id for run in card.recent_runs] == [run.run_id for run in runs[::-1][:5]]
    assert card.runs_more is True


def test_each_step_says_whether_it_can_run_now(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from cruxible_client.contracts.procedures.line_specs import line_spec_path, parse_line_spec
    from cruxible_client.contracts.triggers import CaptureEventNameSelector
    from cruxible_core.service.discovery.runbook_reads import _step

    instance, _root = runbook_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(SETTLE)], path=line_spec_path(SETTLE))
    retired = line.model_copy(
        update={"lifecycle": ArtifactLifecycle(state="retired", predecessor_digest=None)}
    )
    evented = line.model_copy(
        update={
            "trigger_input": "doc",
            "trigger_event": CaptureEventNameSelector(
                capture_contract_identity=ArtifactIdentity(
                    kind="CaptureContract", name="test.orders-v1"
                )
            ),
        }
    )
    for source, availability in (
        (None, "line_missing"),
        (retired, "line_retired"),
        (evented, "line_takes_event"),
        (line, "available"),
    ):
        projection = SimpleNamespace(typed=SimpleNamespace(source=lambda _identity, s=source: s))
        assert _step(projection, 1, _line(SETTLE)).availability == availability
    projection = SimpleNamespace(typed=SimpleNamespace(source=lambda _identity: None))
    assert _step(projection, 2, ActionTarget(action="evidence.sweep")).availability == "available"


def test_orient_counts_and_pages_runbooks_by_name(tmp_path: Path) -> None:
    instance, root = _start_world(tmp_path)
    _unrelated_runbooks(instance, 3, 0)
    run_runbook(instance, root, START, minute=1)

    default = service_playbill_orient(instance, surface="cli")
    assert default.artifacts is not None and default.artifacts.runbooks == 4
    assert any("runbooks" in call for call in default.next)

    page = service_playbill_orient(instance, section="runbooks", limit=2, surface="cli")
    assert page.runbooks is not None
    assert [row.runbook for row in page.runbooks] == [
        "Runbook:test.other-0",
        "Runbook:test.other-1",
    ]
    assert page.truncated and page.next_cursor is not None
    rest = service_playbill_orient(
        instance, section="runbooks", limit=2, cursor=page.next_cursor, surface="cli"
    )
    assert rest.runbooks is not None
    assert [row.runbook for row in rest.runbooks] == ["Runbook:test.other-2", f"Runbook:{START}"]
    assert not rest.truncated
    start = rest.runbooks[1]
    assert (start.steps, start.triggers_total, start.last_run) == (3, 1, "stopped")
    assert start.last_run_at is not None
    assert rest.live is not None


# -- cost: one get is flat in unrelated Runbooks and runs ------------------------------


def _record_runs(instance: Any, name: str, count: int, *, offset: int = 0) -> list[Any]:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    runs = []
    for index in range(offset, offset + count):
        at = start + timedelta(seconds=index)
        run = RunbookRunResult(
            run_id=f"RBR-{name}-{index:06d}",
            runbook=name,
            runbook_artifact_digest="sha256:" + "0" * 64,
            mode="manual",
            started_at=at,
            finished_at=at,
            status="completed",
            steps=(
                RunbookStepOutcome(
                    step=1, target=ActionTarget(action="evidence.sweep"), status="fired", event_id=1
                ),
            ),
        )
        record_runbook_run(instance, run)
        runs.append(run)
    return runs


def _unrelated_runbooks(instance: Any, count: int, offset: int) -> None:
    accept_tree(
        instance,
        {
            runbook_path(f"test.other-{index}"): render_runbook(
                Runbook(
                    identity=ArtifactIdentity(kind="Runbook", name=f"test.other-{index}"),
                    steps=(ActionTarget(action="evidence.sweep"),),
                    lifecycle=ArtifactLifecycle(),
                )
            )
            for index in range(offset, offset + count)
        },
        name=f"other-runbooks-{offset}",
    )


@pytest.fixture
def traced(tmp_path: Path) -> Iterator[Path]:
    from tests.test_scale._counters import trace_sqlite

    with trace_sqlite(tmp_path):
        yield tmp_path


def _measured_get(instance: Any, root: Path) -> tuple[int, int]:
    from tests.test_scale._counters import counting

    _get(instance, f"Runbook:{START}")  # warm: the projection binding is cached per head
    with counting(root) as window:
        card = _get(instance, f"Runbook:{START}").card
    assert not window.unexplained, window.unexplained
    assert isinstance(card, GetRunbookCard) and len(card.recent_runs) == 5
    return window.counts["sql_statements"], window.counts["sql_vm_steps"]


def test_a_get_costs_the_same_beside_10_100_and_1000_unrelated_runbooks_and_runs(
    traced: Path,
) -> None:
    instance, _root = _start_world(traced)
    _record_runs(instance, START, 6)
    measured: dict[int, tuple[int, int]] = {}
    have = 0
    for size in (10, 100, 1000):
        _unrelated_runbooks(instance, size - have, have)
        for index in range(have, size):
            _record_runs(instance, f"test.other-{index}", 1)
        # The Runbook's own older runs grow too: the card still reads five.
        _record_runs(instance, START, size - have, offset=10_000 + have)
        have = size
        measured[size] = _measured_get(instance, traced)
    print("runbook get (statements, vm steps):", measured)
    statements = {size: value[0] for size, value in measured.items()}
    steps = {size: value[1] for size, value in measured.items()}
    assert statements[10] == statements[100] == statements[1000], measured
    # VM steps: B-tree seeks are one step at any size; the few-step wobble is the
    # pages a seek lands on, never a walk (growth would be hundreds per 900 rows).
    assert max(steps.values()) - min(steps.values()) <= 10, measured
