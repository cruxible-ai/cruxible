"""Procedure runs as reads: a card with live progress, and a bounded running-first listing.

``get("ProcedureRun:<run_id>")`` (or a ``RUN-`` prefix) answers what a run is
doing without replaying it while it runs: finished nodes over the Procedure
graph's nodes, the node it is on, elapsed time against the read's evaluation
time, and the Line, occurrence and arm that admitted it. ``orient(section=
"runs")`` lists runs newest first, paged by an immutable key, and
``section="running"`` keeps only runs still running.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
from cruxible_client.contracts.operational_reads import GetProcedureRunCard
from cruxible_core.procedures import execution
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_evaluate_line,
)
from cruxible_core.service.procedures.procedure_runs import (
    ProcedureRunRequest,
    service_run_playbill_procedure,
)
from cruxible_core.service.read_refusals import ReadRefusalError
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_arming import _armed_world
from tests.test_procedures.test_line_triggers import capture
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor, _world

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _get(instance, ref: str, **fields):  # type: ignore[no-untyped-def]
    return service_playbill_get(instance, request=GetRequest(ref=ref, **fields), access=_ACCESS)


class _Crash(BaseException):
    """A daemon crash between a run's last node and its finalization."""


@pytest.fixture(scope="module")
def run_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One finished direct run and one run left running by a crash before finalizing."""

    instance, _owner, procedure = _world(tmp_path_factory.mktemp("runs"))
    finished = service_run_playbill_procedure(
        instance,
        name=procedure.identity.name,
        request=ProcedureRunRequest(evaluation_time=READ_TIME, input={}),
        actor_context=_actor(instance),
    )
    real = execution.ProcedureExecutor._append_event

    def crash_before_final(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "attempt_finalized":
            raise _Crash()
        return real(self, admission, records, event_kind, payload)

    patch = pytest.MonkeyPatch()
    patch.setattr(execution.ProcedureExecutor, "_append_event", crash_before_final)
    try:
        with pytest.raises(_Crash):
            service_run_playbill_procedure(
                instance,
                name=procedure.identity.name,
                request=ProcedureRunRequest(
                    evaluation_time=READ_TIME + timedelta(minutes=5), input={}
                ),
                actor_context=_actor(instance),
            )
    finally:
        patch.undo()
    return instance, procedure, finished


def _running_id(instance) -> str:  # type: ignore[no-untyped-def]
    answer = service_playbill_orient(instance, section="runs", evaluation_time=READ_TIME)
    assert answer.runs is not None
    running = [row.run for row in answer.runs if row.status == "running"]
    assert len(running) == 1
    return running[0]


def test_a_finished_run_card_carries_its_receipt_and_measured_elapsed(run_world) -> None:  # type: ignore[no-untyped-def]
    instance, procedure, finished = run_world

    result = _get(instance, f"ProcedureRun:{finished.run_id}")

    card = result.card
    assert result.kind == "procedure_run" and result.ref == f"ProcedureRun:{finished.run_id}"
    assert isinstance(card, GetProcedureRunCard)
    assert card.status == "succeeded" and card.procedure == procedure.identity.qualified
    assert card.nodes_total == len(procedure.definition.nodes)
    assert card.nodes_done == card.nodes_total == len(card.nodes)
    assert [node.node for node in card.nodes] == [
        node.node_id for node in procedure.definition.nodes
    ]
    assert all(node.verdict == "succeeded" for node in card.nodes)
    assert all(node.duration_us is None for node in card.nodes)
    assert card.receipt_digest == finished.receipt_digest
    assert card.elapsed_basis == "measured_wall_clock" and card.elapsed_us is not None
    assert card.current_node is None and card.triggered_by is None
    assert card.actor == "owner" and card.started_at == READ_TIME


def test_a_running_run_names_its_current_node_and_elapsed_against_the_read(run_world) -> None:  # type: ignore[no-untyped-def]
    instance, procedure, _finished = run_world
    run_id = _running_id(instance)
    later = READ_TIME + timedelta(minutes=7)

    card = _get(instance, run_id[:16], evaluation_time=later).card

    assert isinstance(card, GetProcedureRunCard)
    assert card.status == "running" and card.receipt_digest is None
    assert card.nodes_done == len(procedure.definition.nodes)
    # Every node fired; only finalization is outstanding, so no node is current.
    assert card.current_node is None
    assert card.elapsed_basis == "read_time"
    assert card.elapsed_us == int(timedelta(minutes=2) / timedelta(microseconds=1))


def test_run_references_refuse_with_a_repair_and_read_live(run_world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, finished = run_world

    with pytest.raises(ReadRefusalError) as missing:
        _get(instance, "RUN-" + "0" * 12)
    assert missing.value.error_code == "cruxible.get.ref_not_found"
    assert missing.value.repair is not None
    assert missing.value.repair.arguments == {"section": "runs"}

    with pytest.raises(ReadRefusalError) as malformed:
        _get(instance, "ProcedureRun:RUN-12")
    assert malformed.value.error_code == "cruxible.get.ref_malformed"

    # A run has no history: read beside an older at, it is live and says so.
    first = instance.accepted_history()[0].oid
    historical = _get(instance, finished.run_id, at=first)
    assert historical.live is not None and historical.live.fields == ("card",)
    assert historical.coordinate.git_oid == first[:12]


def test_orient_lists_runs_newest_first_filters_running_and_pages_by_key(run_world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, finished = run_world

    whole = service_playbill_orient(instance, section="runs", evaluation_time=READ_TIME)
    assert whole.runs is not None
    # Newest admission first: the crashed run was admitted after the finished one.
    assert [row.status for row in whole.runs] == ["running", "succeeded"]
    running = service_playbill_orient(instance, section="running")
    assert running.section == "running" and running.runs is not None
    assert [row.status for row in running.runs] == ["running"]
    assert whole.runs[1].run == finished.run_id
    assert whole.next[0] == f"cruxible playbill get ProcedureRun:{whole.runs[0].run}"

    first = service_playbill_orient(instance, section="runs", limit=1)
    assert first.truncated and first.next_cursor is not None
    assert first.runs is not None and first.runs[0].status == "running"
    second = service_playbill_orient(instance, section="runs", limit=1, cursor=first.next_cursor)
    assert second.runs is not None and [row.run for row in second.runs] == [finished.run_id]
    assert not second.truncated and second.next_cursor is None


def test_a_line_run_names_the_line_occurrence_and_arm_that_admitted_it(tmp_path: Path) -> None:
    from cruxible_core.runtime.line_arms import dispatch_armed_line
    from cruxible_core.service.procedures.line_dispatch import armed_work
    from tests.test_procedures.test_line_arming import _manager, _match

    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    dispatched = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )
    assert dispatched is not None
    (run_id,) = [item.run_id for item in dispatched.items if item.run_id is not None]

    card = _get(instance, run_id).card

    assert isinstance(card, GetProcedureRunCard)
    assert card.triggered_by is not None
    assert card.triggered_by.line == line.identity.qualified
    assert card.triggered_by.occurrence is not None
    assert card.triggered_by.arm == arm["arm_id"]
    assert card.triggered_by.armed_by == "operator"
    assert card.nodes_total >= card.nodes_done >= 1
    assert f'cruxible_playbill_get(ref="{line.identity.qualified}")' in card.next


def test_an_explicitly_dispatched_line_run_names_no_arm(tmp_path: Path) -> None:
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    later = start + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=start, until=later),
        actor=_actor(instance),
        now=later,
    )
    dispatched = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequest(limit=1),
        actor=_actor(instance),
        now=later + timedelta(seconds=1),
        caller_rung=3,
    )
    (run_id,) = [item.run_id for item in dispatched.items if item.run_id is not None]

    card = _get(instance, run_id).card

    assert isinstance(card, GetProcedureRunCard)
    assert card.triggered_by is not None and card.triggered_by.line == line.identity.qualified
    assert card.triggered_by.arm is None


def test_a_run_interrupted_mid_graph_names_the_node_it_is_on(tmp_path: Path) -> None:
    instance, _owner, procedure = _world(tmp_path)
    nodes = procedure.definition.nodes
    assert len(nodes) >= 2
    real = execution.ProcedureExecutor._append_event
    fired: list[str] = []

    def crash_after_first_node(self, admission, records, event_kind, payload):  # type: ignore[no-untyped-def]
        if event_kind == "node_fired" and fired:
            raise _Crash()
        result = real(self, admission, records, event_kind, payload)
        if event_kind == "node_fired":
            fired.append(payload["node_id"])
        return result

    patch = pytest.MonkeyPatch()
    patch.setattr(execution.ProcedureExecutor, "_append_event", crash_after_first_node)
    try:
        with pytest.raises(_Crash):
            service_run_playbill_procedure(
                instance,
                name=procedure.identity.name,
                request=ProcedureRunRequest(evaluation_time=READ_TIME, input={}),
                actor_context=_actor(instance),
            )
    finally:
        patch.undo()

    card = _get(instance, _running_id(instance), evaluation_time=READ_TIME).card

    assert isinstance(card, GetProcedureRunCard)
    assert card.status == "running" and card.nodes_done == 1
    assert [node.node for node in card.nodes] == [nodes[0].node_id]
    assert card.current_node is not None
    assert card.current_node.node == nodes[1].node_id
    assert card.current_node.kind == nodes[1].kind
    assert card.elapsed_us == 0 and card.elapsed_basis == "read_time"
