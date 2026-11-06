"""Item 7: a run card serves each list call's members and the lists it ended incomplete.

The members come from the run's own ``source_acquisition`` records (one bounded
journal range per card), so the read grows with the run's list, never with
other runs or accepted state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.operational_reads import GetProcedureRunCard
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.storage.cas import BodyAccessContext
from tests.support.operation_world import Invoker
from tests.test_procedures.test_b2_operations import run_line
from tests.test_procedures.test_capture_lists import (
    LINE_NAME,
    ORIGIN,
    answer,
    bodies,
    page,
    pages_world,
)

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _card(instance: Any, run_id: str) -> GetProcedureRunCard:
    card = service_playbill_get(
        instance, request=GetRequest(ref=f"ProcedureRun:{run_id}"), access=_ACCESS
    ).card
    assert isinstance(card, GetProcedureRunCard)
    return card


def test_a_dropped_member_is_listed_omitted_and_its_list_incomplete(tmp_path: Path) -> None:
    instance = pages_world(tmp_path, acquisition={"doc.pages": {"origins": [ORIGIN]}})
    content = bodies(3)
    members = [page(body, index) for index, body in enumerate(content)]
    members[1] = page(content[1], 1, url="https://elsewhere.test/kev/1")
    state, _ = run_line(instance, invoker=Invoker(output=answer(members)), name=LINE_NAME)
    assert state.status == "succeeded", state.terminal
    (doc,) = [node for node in _card(instance, state.run_id).nodes if node.node == "doc"]
    assert [(m.field, m.index, m.status) for m in doc.members] == [
        ("pages", 0, "selected"),
        ("pages", 1, "omitted"),
        ("pages", 2, "selected"),
    ]
    assert doc.members[1].reason == "cruxible.acquisition.origin_ineligible"
    assert [m.request for m in doc.members] == [m["coordinate"]["request_digest"] for m in members]
    assert doc.members[0].capture == state.result["pages"][0]
    assert doc.incomplete_captures == ("pages",)


def test_a_list_the_provider_says_is_incomplete_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "said").mkdir()
    (tmp_path / "complete").mkdir()
    instance = pages_world(tmp_path / "said")
    members = [page(body, index) for index, body in enumerate(bodies(3))]
    invoker = Invoker(output=answer(members), incomplete_captures=["pages"])
    state, _ = run_line(instance, invoker=invoker, name=LINE_NAME)
    (doc,) = [node for node in _card(instance, state.run_id).nodes if node.node == "doc"]
    assert {m.status for m in doc.members} == {"selected"}
    assert doc.incomplete_captures == ("pages",)
    other = pages_world(tmp_path / "complete")
    complete, _ = run_line(other, invoker=Invoker(output=answer(members)), name=LINE_NAME)
    (whole,) = [node for node in _card(other, complete.run_id).nodes if node.node == "doc"]
    assert len(whole.members) == 3 and whole.incomplete_captures == ()


def _run_at(instance: Any, members: list[dict[str, Any]], *, minute: int) -> Any:
    """A manual run of the pages Line at its own instant (one occurrence each)."""

    from datetime import timedelta

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.support.operation_world import NOW, Operator, actor
    from tests.test_procedures.test_b2_operations import Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=LINE_NAME,
        request=LineRunRequest(line=LINE_NAME),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=Operator(Invoker(output=answer(members))),  # type: ignore[arg-type]
    )


@pytest.fixture
def traced(tmp_path: Path):  # type: ignore[no-untyped-def]
    from tests.test_scale._counters import trace_sqlite

    with trace_sqlite(tmp_path):
        yield


def test_the_members_read_is_flat_in_other_runs(tmp_path: Path, traced: None) -> None:
    """The card's member read is one range of this run's records, whatever else ran."""

    from tests.test_scale._counters import counting

    counts: dict[int, Any] = {}
    for size in (1, 5):
        root = tmp_path / str(size)
        root.mkdir()
        instance = pages_world(root)
        for other in range(size):
            others = [page(f'{{"other": {other}, "i": {i}}}'.encode(), i) for i in range(3)]
            done = _run_at(instance, others, minute=other)
            assert done.run_id is not None
        members = [page(body, index) for index, body in enumerate(bodies(3))]
        state = _run_at(instance, members, minute=59)
        assert len(_card(instance, state.run_id).nodes[0].members) == 3  # warm
        with counting(root) as window:
            card = _card(instance, state.run_id)
        assert not window.unexplained, window.unexplained
        assert [len(node.members) for node in card.nodes if node.node == "doc"] == [3]
        counts[size] = window.counts
    print("item 7 run card work", {size: dict(c) for size, c in counts.items()})
    small, large = counts[1], counts[5]
    assert large["sql_statements"] == small["sql_statements"], (small, large)
    assert large["file_opens"] <= small["file_opens"], (small, large)
    assert large["sql_vm_steps"] <= small["sql_vm_steps"] * 1.10 + 200, (small, large)
