"""Item 7: a run card serves each list call's members and the lists it ended incomplete.

The members come from the run's own ``source_acquisition`` records (one bounded
journal range per card), so the read grows with the run's list, never with
other runs or accepted state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.operational_reads import GetProcedureRunCard, GetRunListMember
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


def _member(field: str, index: int) -> GetRunListMember:
    return GetRunListMember(field=field, index=index, status="selected")


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


def test_an_empty_list_the_provider_says_is_incomplete_is_flagged_with_no_members() -> None:
    """F2: a wholly empty list still carries the provider's own ``incomplete_input``.

    The run's recorded ``CompletenessReason`` names the call's field even
    though no member of it ever reached the ``source_acquisition`` page (an
    empty ``list<capture>`` writes no member records at all).
    """

    from cruxible_client.contracts.claim_evidence import CompletenessReason, RunCompleteness
    from cruxible_client.contracts.operational_reads import GetRunNode
    from cruxible_core.service.discovery.runs import _with_members

    nodes = [GetRunNode(node="doc", kind="provider", sequence=1)]
    reason = CompletenessReason(node="doc", kind="incomplete_input", inputs=("doc.pages",))
    completeness = RunCompleteness(complete=False, reasons=(reason,))
    (doc,) = _with_members(nodes, {}, frozenset(), completeness)
    assert doc.members == ()
    assert doc.members_truncated is False
    assert doc.incomplete_captures == ("pages",)


def test_a_named_incomplete_field_is_flagged_despite_other_visible_lists() -> None:
    """F2b: a CompletenessReason's named field stays incomplete whatever else is visible.

    One call has two list fields: ``pages`` (one member served, on the page)
    and ``empty`` (no member at all, so nothing of it is ever on the page).
    The provider's own ``incomplete_captures`` names only ``empty``; it must
    not be dropped just because ``empty`` has no visible members of its own
    to intersect against.
    """

    from cruxible_client.contracts.claim_evidence import CompletenessReason, RunCompleteness
    from cruxible_client.contracts.operational_reads import GetRunNode
    from cruxible_core.service.discovery.runs import _with_members

    nodes = [GetRunNode(node="doc", kind="provider", sequence=1)]
    members = {"doc": [_member("pages", 0)]}
    reason = CompletenessReason(node="doc", kind="incomplete_input", inputs=("doc.empty",))
    completeness = RunCompleteness(complete=False, reasons=(reason,))
    (doc,) = _with_members(nodes, members, frozenset(), completeness)
    assert [m.field for m in doc.members] == ["pages"]
    assert doc.incomplete_captures == ("empty",)


@dataclass
class _FakeRecord:
    sequence: int
    payload_digest: str


@dataclass
class _FakeStored:
    record: _FakeRecord


class _FakeIndex:
    """A minimal stand-in for ``LocalJournalBackend.index``, in memory.

    Filters an in-memory list the same way the real index's ``select``
    does (sequence range and limit; ``run_id``/``event_kind`` are accepted
    but every fake record already belongs to the one run and kind under
    test). Counts calls and rows returned, so a test can assert the work
    stays bounded by the number of probes issued, not by how many records
    exist.
    """

    def __init__(self, records: list[_FakeStored]) -> None:
        self.records = records
        self.calls = 0
        self.rows_returned = 0

    def select(
        self,
        _stream: object,
        *,
        run_id: str | None = None,
        event_kind: str | None = None,
        first_sequence: int | None = None,
        last_sequence: int | None = None,
        limit: int | None = None,
        descending: bool = False,
        **_ignored: object,
    ) -> tuple[_FakeStored, ...]:
        self.calls += 1
        rows = [
            item
            for item in self.records
            if (first_sequence is None or item.record.sequence >= first_sequence)
            and (last_sequence is None or item.record.sequence <= last_sequence)
        ]
        rows.sort(key=lambda item: item.record.sequence, reverse=descending)
        if limit is not None:
            rows = rows[:limit]
        self.rows_returned += len(rows)
        return tuple(rows)


@dataclass
class _FakeJournal:
    index: _FakeIndex


def _stored_acquisition(
    instance: Any, *, node_id: str, field: str, index: int, sequence: int
) -> _FakeStored:
    """One real, body-store-backed ``source_acquisition`` payload at ``sequence``."""

    from cruxible_core.exhaust.records import journal_payload_bytes

    payload = {
        "field": f"{field}/{index}",
        "decision": {"disposition": "selected", "selected_capture_digests": []},
        "node_id": node_id,
    }
    content = journal_payload_bytes(payload)
    metadata = instance.body_store().store(content, journal_payload=True)
    return _FakeStored(record=_FakeRecord(sequence=sequence, payload_digest=metadata.digest))


def _three_call_world(instance: Any, *, second_members: int, third_members: int) -> Any:
    """Three calls in firing order: "first" fills the page; "second" and "third" don't.

    "first" fires right after its own ``RUN_CARD_LIST_MEMBERS_MAX`` members
    (exactly filling the page, none of its own left over); "second" and
    "third" each fire after their own ``second_members``/``third_members``,
    every one of which falls past the page for the run as a whole.
    """

    from cruxible_client.contracts.operational_reads import (
        RUN_CARD_LIST_MEMBERS_MAX,
        GetRunNode,
    )

    records = [
        _stored_acquisition(instance, node_id="first", field="pages", index=i, sequence=i + 1)
        for i in range(RUN_CARD_LIST_MEMBERS_MAX)
    ]
    sequence = RUN_CARD_LIST_MEMBERS_MAX + 1
    first_node = GetRunNode(node="first", kind="provider", sequence=sequence)
    sequence += 1
    for i in range(second_members):
        records.append(
            _stored_acquisition(
                instance, node_id="second", field="pages", index=i, sequence=sequence
            )
        )
        sequence += 1
    second_node = GetRunNode(node="second", kind="provider", sequence=sequence)
    sequence += 1
    for i in range(third_members):
        records.append(
            _stored_acquisition(
                instance, node_id="third", field="pages", index=i, sequence=sequence
            )
        )
        sequence += 1
    third_node = GetRunNode(node="third", kind="provider", sequence=sequence)
    return [first_node, second_node, third_node], records


def test_every_call_past_the_window_is_flagged_not_only_the_first(tmp_path: Path) -> None:
    """F2a: every displayed call with unserved members is found, not only the first.

    "first" fires after exactly ``RUN_CARD_LIST_MEMBERS_MAX`` members (filling
    the page, none left over); "second" and "third" each fire after one
    member, both past the page. All three must be told apart: the earlier
    fix only ever named the overflow record's own node ("second"), leaving
    "third" unmarked despite its own member never reaching the page either.
    """

    from cruxible_client.contracts.operational_reads import RUN_CARD_LIST_MEMBERS_MAX
    from cruxible_core.service.discovery.runs import _list_members, _with_members
    from tests.core_support._support import initialize_local

    instance, _owner = initialize_local(tmp_path)
    nodes, records = _three_call_world(instance, second_members=1, third_members=1)
    journal = _FakeJournal(_FakeIndex(records))
    found, truncated = _list_members(instance, journal, object(), "run-1", nodes)
    assert len(found["first"]) == RUN_CARD_LIST_MEMBERS_MAX
    assert truncated == frozenset({"second", "third"})
    out = {node.node: node for node in _with_members(nodes, found, truncated, None)}
    assert len(out["first"].members) == RUN_CARD_LIST_MEMBERS_MAX
    assert out["first"].members_truncated is False
    assert out["second"].members == ()
    assert out["second"].members_truncated is True
    assert out["third"].members == ()
    assert out["third"].members_truncated is True


def test_the_truncation_checks_are_flat_in_unrelated_member_growth(tmp_path: Path) -> None:
    """F2a: the per-node checks stay bounded by the displayed calls, not by records.

    "second" and "third" grow from one member each to a hundred each (a
    hundred-fold growth in unrelated record count, the same three displayed
    calls); the number of indexed queries ``_list_members`` issues, and the
    total rows they return, stay the same -- one bounded range for the page
    plus one bounded existence probe per call, each capped at ``limit=1``.
    """

    from cruxible_core.service.discovery.runs import _list_members
    from tests.core_support._support import initialize_local

    counts: dict[int, tuple[int, int]] = {}
    for size in (1, 100):
        root = tmp_path / str(size)
        root.mkdir()
        instance, _owner = initialize_local(root)
        nodes, records = _three_call_world(instance, second_members=size, third_members=size)
        journal = _FakeJournal(_FakeIndex(records))
        _found, truncated = _list_members(instance, journal, object(), "run-1", nodes)
        assert truncated == frozenset({"second", "third"})
        counts[size] = (journal.index.calls, journal.index.rows_returned)
    print("F2a truncation checks at 1/100 unrelated members per call", counts)
    assert counts[1] == counts[100]


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
