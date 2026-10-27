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
from cruxible_core.service.discovery.runs import procedure_run_card
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
    # How long it took: the final record's observed wall time, as the run card reads it.
    card = procedure_run_card(
        instance, str(run), evaluation_time=READ_TIME, render=lambda ref, _view: ref
    )
    assert card.elapsed_basis == "measured_wall_clock"
    assert finished.elapsed_us == card.elapsed_us and finished.elapsed_us is not None
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
    # A final record that measures no wall time serves none.
    assert older.elapsed_us is None and "elapsed_us" not in older.model_dump(mode="json")
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
    # The final payload itself survives: its elapsed time still reads.
    assert finished.elapsed_us is not None
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


def test_a_projection_from_before_the_feed_derives_its_rows_in_chunks(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from cruxible_core.exhaust import line_dispatch
    from cruxible_core.storage.preview_fence import previewing

    instance, _procedure, _names = world
    whole = service_playbill_orient(instance, section="activity", limit=100).activity
    store = LineDispatchStore(instance)

    def forget() -> None:
        connection = sqlite3.connect(store.path)
        connection.executescript(
            "DROP TABLE activity; DROP TABLE activity_lines; DROP TABLE activity_progress;"
        )
        connection.close()

    def mark() -> object:
        connection = sqlite3.connect(store.path)
        try:
            return connection.execute("SELECT sequence FROM activity_progress").fetchone()
        except sqlite3.OperationalError:
            return None  # no table: nothing was derived into the file
        finally:
            connection.close()

    forget()
    # A preview's copy is discarded: it never derives, and the file keeps no mark.
    with previewing(), store.locked():
        pass
    assert mark() is None
    monkeypatch.setattr(line_dispatch, "_ACTIVITY_CHUNK", 2)
    assert service_playbill_orient(instance, section="activity", limit=100).activity == whole
    with store.locked() as conn:
        progress = conn.execute("SELECT sequence FROM progress").fetchone()[0]
    assert mark() == (progress,)


def test_transitions_an_older_binary_applied_are_derived_on_the_next_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that keeps no activity rows leaves a gap; the next open derives it."""

    from cruxible_core.exhaust import line_dispatch

    instance, _procedure, names = _world(tmp_path)
    service_playbill_orient(instance, section="activity", limit=1)
    enabled_at = READ_TIME + timedelta(minutes=5)
    monkeypatch.setattr(line_dispatch, "_note_activity", lambda *_args: None)
    service_enable_line(
        instance,
        names["line"].removeprefix("Line:"),
        principal=LOCAL,
        actor=_actor(instance),
        now=enabled_at,
        daemon_id="daemon",
    )
    monkeypatch.undo()
    (row, *_rest) = service_playbill_orient(instance, section="activity", limit=5).activity or ()
    assert row.event == "line_state_changed" and row.at == enabled_at
    assert (row.previous, row.state) == ("stopped", "running")


def test_a_concurrent_opener_never_rewrites_rows_it_already_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another opener advances the projection while this one derives: nothing is replayed twice.

    The interleaving is deterministic: right after this opener's chunked
    derive commits (releasing the write lock), another opener enables and then
    disables the Line. This opener must read progress again under its own
    write lock, so it replays neither transition, and the rows the other
    opener wrote keep the states they were derived against.
    """

    from cruxible_core.service.procedures.line_dispatch import service_disable_line

    instance, _procedure, names = _world(tmp_path)
    service_playbill_orient(instance, section="activity", limit=1)
    real = LineDispatchStore._derive_activity
    interleaved: list[bool] = []
    enabled_at = READ_TIME + timedelta(minutes=5)
    disabled_at = enabled_at + timedelta(minutes=1)
    replayed: list[int] = []
    real_apply = LineDispatchStore._apply

    def derive(self: LineDispatchStore, conn: Any, *, through: int) -> None:
        real(self, conn, through=through)
        if not interleaved:
            interleaved.append(True)
            name = names["line"].removeprefix("Line:")
            service_enable_line(
                instance,
                name,
                principal=LOCAL,
                actor=_actor(instance),
                now=enabled_at,
                daemon_id="daemon",
            )
            service_disable_line(instance, name, actor=_actor(instance), now=disabled_at)
            interleaved.append(False)

    def apply(self: LineDispatchStore, conn: Any, event: Any, sequence: int, at: str) -> None:
        if interleaved == [True, False]:
            replayed.append(sequence)
        real_apply(self, conn, event, sequence, at)

    monkeypatch.setattr(LineDispatchStore, "_derive_activity", derive)
    monkeypatch.setattr(LineDispatchStore, "_apply", apply)
    rows = service_playbill_orient(instance, section="activity", limit=2).activity
    assert interleaved == [True, False] and replayed == []
    assert rows is not None
    disabled, enabled = rows
    assert (disabled.at, disabled.previous, disabled.state) == (disabled_at, "running", "disabled")
    assert (enabled.at, enabled.previous, enabled.state) == (enabled_at, "stopped", "running")


def _accept_at(instance: Any, owner: Any, name: str, timestamp: str) -> int:
    from tests.support.queries import subject_query_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(subject_query_members(name))
    _accept_tree(instance, owner, tree, timestamp=timestamp, proposal_name=name)
    return instance.accepted_history()[-1].sequence


def _out_of_order(tmp_path: Path) -> tuple[Any, Any, Any]:
    """Generations whose acceptance stamps run backwards, between journal records."""

    instance, _line, procedure, owner = line_world(
        tmp_path, CaptureLandingSchedule(event=SELECTOR), with_owner=True
    )
    for name, minute in (("late-a", 40), ("late-b", 10), ("late-c", 50)):
        _accept_at(instance, owner, name, f"2026-08-24T15:{minute}:00.000000Z")
    for minute in (20, 45, 55):
        at = datetime.fromisoformat(f"2026-08-24T15:{minute}:00+00:00")
        _synthetic(instance, procedure, "attempt_finalized", run=f"j{minute}", at=at)
    return instance, procedure, owner


def _identity(row: Any) -> tuple[str, object]:
    return row.event, getattr(row, "sequence", None) or getattr(row, "run", None)


def test_out_of_order_acceptance_times_walk_like_one_page(tmp_path: Path) -> None:
    instance, _procedure, _owner = _out_of_order(tmp_path)
    whole = list(service_playbill_orient(instance, section="activity", limit=100).activity or ())
    assert len({_identity(row) for row in whole}) == len(whole)
    generations = [row.sequence for row in whole if row.event == "generation_accepted"]
    assert generations == sorted(generations, reverse=True)
    order = [_identity(row) for row in whole]
    head = generations[0]
    # Stamps 15:40, 15:10, 15:50 by sequence: 15:10 is placed at 15:40, the
    # latest at or below it, and each row keeps its own stamp as ``at``.
    expected = [
        ("run_finished", "RUN-j55"),
        ("generation_accepted", head),
        ("run_finished", "RUN-j45"),
        ("generation_accepted", head - 1),
        ("generation_accepted", head - 2),
        ("run_finished", "RUN-j20"),
    ]
    assert [item for item in order if item in expected] == expected
    assert [whole[order.index(item)].at.minute for item in expected] == [55, 50, 45, 10, 40, 20]
    for limit in (1, 2, 3):
        assert _walk(instance, limit=limit) == whole


def test_a_generation_accepted_between_pages_is_listed_once_when_below_the_boundary(
    tmp_path: Path,
) -> None:
    instance, _procedure, owner = _out_of_order(tmp_path)
    before = list(service_playbill_orient(instance, section="activity", limit=100).activity or ())
    head = before[1].sequence
    first = service_playbill_orient(instance, section="activity", limit=1)
    assert first.activity is not None and first.next_cursor is not None
    # Placed at 15:50 (the latest stamp at or below it): below the boundary, listed once.
    older = _accept_at(instance, owner, "between-old", "2026-08-24T15:30:00.000000Z")
    # Placed at 17:00: above the boundary, listed by a fresh read, never by this walk.
    newer = _accept_at(instance, owner, "between-new", "2026-08-24T17:00:00.000000Z")
    rest: list[Any] = []
    cursor = first.next_cursor
    while cursor is not None:
        page = service_playbill_orient(instance, section="activity", limit=1, cursor=cursor)
        rest.extend(page.activity or ())
        cursor = page.next_cursor
    walked = [*first.activity, *rest]
    listed = [row.sequence for row in walked if row.event == "generation_accepted"]
    assert listed.count(older) == 1 and newer not in listed
    assert [row for row in walked if getattr(row, "sequence", None) != older] == before
    order = [_identity(row) for row in walked]
    assert order[:3] == [
        ("run_finished", "RUN-j55"),
        ("generation_accepted", older),
        ("generation_accepted", head),
    ]
    fresh = service_playbill_orient(instance, section="activity", limit=1).activity
    assert fresh is not None and fresh[0].sequence == newer


def test_generations_accepted_mid_walk_are_read_once_and_pages_stay_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many generations accepted between pages: each is read once, and a page reads its rows."""

    from cruxible_core.service.discovery import activity, ledger_times

    reads: Counter[str] = Counter()
    real_time, real_row = ledger_times.generation_accepted_at, activity.generation_row

    def timed(*args: Any) -> Any:
        reads["indexed"] += 1
        return real_time(*args)

    def row(*args: Any) -> Any:
        reads["rows"] += 1
        return real_row(*args)

    monkeypatch.setattr(ledger_times, "generation_accepted_at", timed)
    monkeypatch.setattr(activity, "generation_row", row)
    observed = []
    for accepted in (5, 50):
        root = tmp_path / f"accepted-{accepted}"
        root.mkdir()
        instance, _procedure, owner = _out_of_order(root)
        first = service_playbill_orient(instance, section="activity", limit=1)
        for index in range(accepted):
            _accept_at(instance, owner, f"mid-{index}", f"2026-08-24T16:{index:02d}:00.000000Z")
        reads.clear()
        second = service_playbill_orient(
            instance, section="activity", limit=1, cursor=first.next_cursor
        )
        caught_up = dict(reads)
        reads.clear()
        third = service_playbill_orient(
            instance, section="activity", limit=1, cursor=second.next_cursor
        )
        rows = [_identity(row) for row in (*(second.activity or ()), *(third.activity or ()))]
        observed.append((caught_up, dict(reads), rows))
    (small_up, small_page, small_rows), (large_up, large_page, large_rows) = observed
    # The first read after the acceptances reads each new generation into the index once.
    assert (small_up["indexed"], large_up["indexed"]) == (5, 50)
    # Every later page reads only its own rows, however many were accepted mid-walk.
    assert small_page == large_page and small_page.get("indexed", 0) == 0
    assert small_rows == large_rows


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
    deeper = service_playbill_orient(instance, section="activity", limit=5, surface="cli")
    (finished,) = [
        line
        for line in _render_orient(deeper.model_dump(mode="json")).splitlines()
        if line.startswith("2026-") and line.split("  ")[1] == "run_finished"
    ]
    assert "status=succeeded" in finished and "elapsed_us=" in finished


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param(lambda index: f"2026-08-24T16:{index:02d}:00.000000Z", id="newer"),
        # Older than the head's 15:50: each is placed at 15:50, a plateau with the head.
        pytest.param(lambda index: "2026-08-24T15:10:00.000000Z", id="plateau"),
    ],
)
def test_a_reader_behind_the_indexed_head_seeks_below_its_own_head(traced: Path, stamp) -> None:  # type: ignore[no-untyped-def]
    """Another reader indexed generations past this one's head: a page never walks them."""

    from cruxible_core.service.discovery.ledger_times import newest_generations

    steps = []
    for accepted in (5, 50):
        root = traced / f"behind-{accepted}"
        root.mkdir()
        instance, _procedure, owner = _out_of_order(root)
        behind = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
        for index in range(accepted):
            _accept_at(instance, owner, f"ahead-{index}", stamp(index))
        with instance.accepted_history_reader() as history:
            newest_generations(instance, history, limit=1, before=None)  # indexes past the head
        with instance.accepted_history_reader(at=behind) as history:
            newest_generations(instance, history, limit=1, before=None)  # warm
            with counting(root) as window:
                (row,) = newest_generations(instance, history, limit=1, before=None)
            assert row.position == history.sequence
        assert window.unexplained == [], window.unexplained
        steps.append(window.counts["sql_vm_steps"])
    small, large = steps
    assert 0 < small and large <= small * 1.25 + 64, steps


def test_a_page_inside_one_instant_seeks_on_every_key_column(traced: Path) -> None:
    """A run's records share one instant: a cursor inside them never walks the tie."""

    from cruxible_core.service.discovery.runs import procedure_run_journal, procedure_run_stream

    steps = []
    for landed in (5, 50):
        root = traced / f"tie-{landed}"
        root.mkdir()
        instance, procedure, _names = _world(root)
        for _index in range(landed):
            capture(instance, procedure, at=_HISTORY, partition="run:tie")
        journal = procedure_run_journal(instance)
        assert journal is not None
        stream = procedure_run_stream(instance)
        (top,) = journal.index.newest_events(
            stream, event_kind="produced_capture", limit=1, before=(_record_at(_HISTORY), "~", 0)
        )
        key = (top.recorded_at, top.partition_id, top.position)
        journal.index.newest_events(stream, event_kind="produced_capture", limit=1, before=key)
        with counting(root) as window:
            (row,) = journal.index.newest_events(
                stream, event_kind="produced_capture", limit=1, before=key
            )
        assert window.unexplained == [], window.unexplained
        assert (row.recorded_at, row.partition_id, row.position) == (
            top.recorded_at,
            top.partition_id,
            top.position - 1,
        )
        steps.append(window.counts["sql_vm_steps"])
    small, large = steps
    assert 0 < small and large <= small * 1.25 + 64, steps


def _record_at(value: datetime) -> str:
    from cruxible_client.contracts.temporal import format_datetime

    return str(format_datetime(value))
