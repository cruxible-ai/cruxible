"""``orient(section="activity")``: what just happened, newest first, over every source.

One world records each curated event once -- generations, a Line enabled and
stopped, a due occurrence, a run admitted and finished, a Capture landed, an
inbox item posted and acknowledged, a Capture erased -- and the feed lists
them merged exactly newest first, pages by key without repeating or skipping a
row that lands between pages, lists lost payloads as unavailable rows, and
costs its page whatever the history holds.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.capture_erasure import CaptureErasureRecord
from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
from cruxible_client.contracts.line_inbox import InboxAcknowledged
from cruxible_client.contracts.orient import OrientResult
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.evidence.capture_erasures import CaptureErasureStore
from cruxible_core.exhaust import ProcedureExhaustWriter
from cruxible_core.exhaust.line_dispatch import LineDispatchStore
from cruxible_core.exhaust.line_inbox import InboxPosting, LineInboxStore
from cruxible_core.service.discovery.orient import _keyset_cursor, service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_enable_line,
    service_evaluate_line,
    service_stop_line_arm,
)
from cruxible_core.service.procedures.procedure_runs import (
    PROCEDURE_RUN_FENCING_TOKEN,
    _activate_writer,
    _journal,
    _stream,
)
from tests.test_procedures.test_line_arming import LOCAL
from tests.test_procedures.test_line_dispatch import _active_segment
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor
from tests.test_scale._counters import counting, trace_sqlite

_DIGEST = "sha256:" + "a" * 64
#: Older history goes here: after the fixture's generations, before the live events.
_HISTORY = datetime.fromisoformat("2026-08-24T15:30:00+00:00")


def _coordinate(instance: Any) -> AcceptedCoordinate:
    return AcceptedCoordinate.from_internal(instance.accepted_coordinate())


def _post(instance: Any, line: str, payload: object, *, run: str, at: datetime) -> str:
    store = LineInboxStore(instance)
    (key,) = store.post(
        (
            InboxPosting(
                line=line,
                procedure="Procedure:trigger-method",
                procedure_artifact_digest=_DIGEST,
                run_id=run,
                node_id="post",
                item_key="item",
                payload=payload,  # type: ignore[arg-type]
                source_capture_digests=(),
                posted_at=at,
            ),
        ),
        actor=_actor(instance),
        now=at,
        coordinate=_coordinate(instance),
    )
    return "INB-" + key.removeprefix("sha256:")


def _erase(instance: Any, capture_digest: str, *, at: datetime) -> None:
    instance.recording_clock = lambda: at
    CaptureErasureStore(instance).record(
        CaptureErasureRecord(
            capture_digest=capture_digest,
            capture_contract="CaptureContract:trigger-profile",
            capture_contract_digest=_DIGEST,
            erasure_rule_digest=_DIGEST,
            body_digests=("sha256:" + "b" * 64,),
            erased_by="owner",
            erased_at=at,
            because="Erased for the activity feed.",
        ),
        actor_id="owner",
        coordinate=_coordinate(instance),
    )


def _synthetic(instance: Any, procedure: Any, kind: str, *, run: str, at: datetime) -> None:
    """One Procedure-run record of ``kind`` whose payload no page here decodes."""

    journal, _ = _journal(instance)
    stream = _stream(instance)
    partition = f"run:{run}"
    _activate_writer(journal, stream, partition)
    ProcedureExhaustWriter(
        journal=journal, bodies=instance.body_store(), fencing_token=PROCEDURE_RUN_FENCING_TOKEN
    ).append(
        stream=stream,
        partition_id=partition,
        event_kind=kind,
        accepted_coordinate=_coordinate(instance),
        procedure_artifact_digest=procedure.artifact_digest,
        definition_digest=procedure.procedure.definition_digest,
        actor_context=_actor(instance),
        recorded_at=at,
        run_id=f"RUN-{run}",
        payload={"synthetic": run},
    )


def _history(instance: Any, procedure: Any, line: str, events: int) -> None:
    """``events`` older records in every journal, dispatch noise included."""

    store = LineDispatchStore(instance)
    for index in range(events):
        at = _HISTORY + timedelta(milliseconds=index)
        _synthetic(instance, procedure, "admission_bound", run=f"old-{index}", at=at)
        # A landing Trigger discovery reads: a typed payload, outside the Line's range.
        capture(instance, procedure, at=at, partition=f"run:old-{index}")
        _synthetic(instance, procedure, "attempt_finalized", run=f"old-{index}", at=at)
        _post(instance, line, {"old": index}, run=f"RUN-old-{index}", at=at)
        _erase(instance, "sha256:" + f"{index:064x}", at=at)
        with store.locked() as conn:
            # A transition the feed never lists: a page must not scan past it.
            store.append(
                conn,
                "evaluated",
                {"line_id": "noise", "epoch": 1, "since": "a", "until": "b"},
                actor=_actor(instance),
                now=at,
            )


def _world(tmp_path: Path, *, history: int = 0) -> tuple[Any, Any, dict[str, Any]]:
    """Every curated event once, after ``history`` older records in every journal."""

    instance, line, procedure = line_world(tmp_path, CaptureLandingSchedule(event=SELECTOR))
    name = line.identity.qualified
    _history(instance, procedure, name, history)
    start = READ_TIME + timedelta(seconds=10)
    service_enable_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    capture(instance, procedure, at=start + timedelta(seconds=1), capture_digest=_DIGEST)
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
    (run,) = [item.run_id for item in dispatched.items if item.run_id]
    item = _post(instance, name, {"finding": 1}, run=str(run), at=later + timedelta(seconds=2))
    LineInboxStore(instance).record_disposition(
        item,
        InboxAcknowledged(actor_id="owner", decided_at=later + timedelta(seconds=3)),
        actor=_actor(instance),
        now=later + timedelta(seconds=3),
        coordinate=_coordinate(instance),
    )
    _erase(instance, _DIGEST, at=later + timedelta(seconds=4))
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="The arming credential was revoked.",
        actor=_actor(instance),
        now=later + timedelta(seconds=5),
    )
    return instance, procedure, {"line": name, "run": run, "item": item}


def _walk(instance: Any, *, limit: int) -> list[Any]:
    rows: list[Any] = []
    cursor = None
    while True:
        page = service_playbill_orient(instance, section="activity", limit=limit, cursor=cursor)
        assert page.activity is not None and page.live is not None
        rows.extend(page.activity)
        if page.next_cursor is None:
            return rows
        assert page.truncated and len(page.activity) == limit
        cursor = page.next_cursor


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any, dict[str, Any]]:
    return _world(tmp_path_factory.mktemp("activity"))


def test_every_source_lists_its_events_merged_newest_first(world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, names = world
    answer = service_playbill_orient(instance, section="activity", surface="cli")
    assert answer.section == "activity" and answer.activity is not None
    assert answer.live is not None and answer.live.fields == ("activity",)
    rows = answer.activity
    assert [row.event for row in rows[:9]] == [
        "line_state_changed",
        "capture_erased",
        "inbox_disposed",
        "inbox_posted",
        "run_finished",
        "run_started",
        "occurrence_due",
        "capture_landed",
        "line_state_changed",
    ]
    assert {row.event for row in rows[9:]} == {"generation_accepted"}
    # Exactly newest first across sources; a run's records share its instant.
    assert [row.at for row in rows] == sorted((row.at for row in rows), reverse=True)
    stopped, erased, decided, posted, finished, started, due, landed, enabled = rows[:9]
    line, run = names["line"], names["run"]
    assert (stopped.previous, stopped.state, stopped.reason) == (
        "running",
        "stopped",
        "credential_revoked",
    )
    assert (enabled.previous, enabled.state, enabled.ref) == (None, "running", line)
    assert erased.ref == f"Capture:{_DIGEST}" and erased.capture == "CAP-" + "a" * 12
    assert erased.by == "owner" and not erased.unavailable
    assert (decided.ref, decided.line, decided.disposition) == (
        names["item"],
        line,
        "acknowledged",
    )
    assert (posted.ref, posted.line, posted.run) == (names["item"], line, run)
    assert finished.ref == started.ref == f"ProcedureRun:{run}"
    assert finished.status == "succeeded" and finished.line == started.line == line
    assert started.procedure == finished.procedure == "Procedure:trigger-method"
    assert finished.at == started.at
    assert (due.ref, due.line) == (line, line) and due.trigger is not None
    assert landed.ref == f"Capture:{_DIGEST}" and landed.contract is not None
    # The landing names a Capture the store does not hold: its source is unreadable.
    assert landed.contract.startswith("CaptureContract:") and landed.unavailable
    sequences = [row.sequence for row in rows[9:]]
    assert sequences == list(range(sequences[0], -1, -1))
    assert rows[-1].ref is None and rows[-2].ref == rows[-2].proposal is not None
    # The first row with a get-readable ref is the next read.
    assert answer.next[0] == f"cruxible get {line}"
    # The wire form (what HTTP, the SDK and MCP carry) reads back as the same rows.
    assert OrientResult.model_validate_json(answer.model_dump_json()) == answer


def test_pages_continue_by_key_and_match_one_page(world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, _names = world
    whole = service_playbill_orient(instance, section="activity", limit=100)
    assert whole.activity is not None and whole.next_cursor is None
    for limit in (1, 2, 3):
        assert _walk(instance, limit=limit) == list(whole.activity)


def test_a_record_landing_between_pages_is_never_repeated_or_skipped(tmp_path: Path) -> None:
    instance, procedure, names = _world(tmp_path)
    before = list(service_playbill_orient(instance, section="activity", limit=100).activity or ())
    first = service_playbill_orient(instance, section="activity", limit=4)
    assert first.activity is not None and first.next_cursor is not None
    boundary = first.activity[-1].at
    # Newer than the page boundary: listed by the next fresh read, not by this walk.
    newest = READ_TIME + timedelta(hours=1)
    _post(instance, names["line"], {"late": 1}, run="RUN-late", at=newest)
    capture(instance, procedure, at=newest, partition="run:late")
    # Older than the boundary: the walk still reaches it.
    _synthetic(instance, procedure, "attempt_finalized", run="older", at=_HISTORY)
    rest = []
    cursor = first.next_cursor
    while cursor is not None:
        page = service_playbill_orient(instance, section="activity", limit=3, cursor=cursor)
        rest.extend(page.activity or ())
        cursor = page.next_cursor
    walked = [*first.activity, *rest]
    assert all(row.at <= boundary for row in rest)
    assert len(walked) == len(before) + 1
    late = [row for row in walked if row.at == newest]
    assert late == []
    (older,) = [row for row in walked if getattr(row, "run", None) == "RUN-older"]
    assert older.event == "run_finished" and older.unavailable
    assert [row for row in walked if row is not older] == before
    fresh = service_playbill_orient(instance, section="activity", limit=2).activity
    assert fresh is not None and {row.event for row in fresh} == {"inbox_posted", "capture_landed"}


def test_a_foreign_or_malformed_cursor_refuses(world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, _names = world
    page = service_playbill_orient(instance, section="activity", limit=1)
    with pytest.raises(ListCursorMismatch):
        service_playbill_orient(instance, section="runs", cursor=page.next_cursor)
    foreign = _keyset_cursor(view="activity", served=page.coordinate, last_key=["x", "1"])
    with pytest.raises(ListCursorMismatch):
        service_playbill_orient(instance, section="activity", cursor=foreign)


def test_lost_payloads_list_as_unavailable_rows(tmp_path: Path) -> None:
    instance, _procedure, names = _world(tmp_path)
    bodies = instance.body_store()
    journal, _ = _journal(instance)
    stream = _stream(instance)
    (admission,) = journal.select_records(stream, run_id=names["run"], event_kind="admission_bound")
    bodies._path(admission.record.payload_digest).unlink()
    dispatch = LineDispatchStore(instance)
    assert dispatch.journal is not None
    pending = [
        stored
        for stored in dispatch.journal.select_records(dispatch.stream, partition_id="dispatch")
    ]
    bodies._path(pending[0].record.payload_digest).unlink()
    # The projections replay from the journal: the dispatch one past the lost payload.
    dispatch.path.unlink()
    rows = service_playbill_orient(instance, section="activity", limit=100).activity
    assert rows is not None
    started = next(row for row in rows if row.event == "run_started")
    assert started.unavailable and started.procedure is None and started.run == names["run"]
    finished = next(row for row in rows if row.event == "run_finished")
    assert finished.unavailable and finished.status == "succeeded" and finished.procedure is None
    lost = [row for row in rows if row.event == "record_unavailable"]
    assert [row.journal for row in lost] == ["line_dispatch"]
    assert lost[0].at == pending[0].record.recorded_at


def test_an_incomplete_inbox_lists_up_to_its_lost_payload(tmp_path: Path) -> None:
    instance, _procedure, names = _world(tmp_path)
    store = LineInboxStore(instance)
    assert store.root.is_dir()
    journal = store._journal(create=False)
    assert journal is not None
    posted, decided = journal.select_records(store.stream, partition_id="inbox")
    instance.body_store()._path(decided.record.payload_digest).unlink()
    store.path.unlink()
    rows = service_playbill_orient(instance, section="activity", limit=100).activity
    assert rows is not None
    inbox = [
        row for row in rows if row.event.startswith("inbox") or row.event == "record_unavailable"
    ]
    assert [row.event for row in inbox] == ["record_unavailable", "inbox_posted"]
    assert inbox[0].journal == "line_inbox" and inbox[1].ref == names["item"]
    assert inbox[0].at == decided.record.recorded_at


def test_a_projection_from_before_the_feed_derives_its_rows_once(world) -> None:  # type: ignore[no-untyped-def]
    instance, _procedure, _names = world
    whole = service_playbill_orient(instance, section="activity", limit=100).activity
    store = LineDispatchStore(instance)
    connection = sqlite3.connect(store.path)
    connection.executescript(
        "DROP TABLE activity; DROP TABLE activity_lines; PRAGMA user_version=0;"
    )
    connection.close()
    assert service_playbill_orient(instance, section="activity", limit=100).activity == whole


# -- cost -------------------------------------------------------------------------------------


@pytest.fixture
def traced(tmp_path: Path) -> Iterator[Path]:
    with trace_sqlite(tmp_path):
        yield tmp_path


def test_a_page_costs_its_rows_not_the_history(traced: Path) -> None:
    """Ten times the older events in every journal: the same page does the same work."""

    counts: list[Counter[str]] = []
    pages = []
    for events in (5, 50):
        root = traced / f"history-{events}"
        root.mkdir()
        instance, _procedure, _names = _world(root, history=events)
        service_playbill_orient(instance, section="activity", limit=5)  # warm
        with counting(root) as window:
            page = service_playbill_orient(instance, section="activity", limit=5)
        assert window.unexplained == [], window.unexplained
        counts.append(window.counts)
        pages.append([row.event for row in page.activity or ()])
        cursor = page.next_cursor
        with counting(root) as window:
            # The rest of the live events: the history's own payloads are never read.
            service_playbill_orient(instance, section="activity", limit=4, cursor=cursor)
        counts.append(window.counts)
    small, small_next, large, large_next = counts
    assert pages[0] == pages[1]
    for name in ("sql_statements", "cas_body_reads", "projection_binds", "git_blob_reads"):
        assert large[name] == small[name], (name, small[name], large[name])
        assert large_next[name] == small_next[name], (name, small_next[name], large_next[name])
    for first, second in ((small, large), (small_next, large_next)):
        assert 0 < first["sql_vm_steps"]
        # Flat: a fixed allowance for B-tree depth, never a walk of the history.
        assert second["sql_vm_steps"] <= first["sql_vm_steps"] * 1.25 + 1024, (first, second)


def test_the_cli_prints_one_line_per_event(world) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.cli.commands.playbill import _render_orient

    instance, _procedure, names = world
    answer = service_playbill_orient(instance, section="activity", limit=3, surface="cli")
    rendered = _render_orient(answer.model_dump(mode="json")).splitlines()
    events = [line for line in rendered if line.startswith("2026-")]
    assert [line.split("  ")[1] for line in events] == [
        "line_state_changed",
        "capture_erased",
        "inbox_disposed",
    ]
    assert f"  {names['line']}  line={names['line']}" in events[0]
    assert "state=stopped" in events[0] and "reason=credential_revoked" in events[0]
    assert any(
        line.startswith("  cruxible orient --section activity --cursor ") for line in rendered
    )
