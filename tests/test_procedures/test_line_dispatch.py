"""Pending work survives loss of process/projection without implicit catch-up."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.line_dispatch import (
    LineDispatchRequestV1,
    LineEvaluateRequestV1,
    LineListenRequestV1,
    LineTriggerCheckRequestV1,
)
from cruxible_client.contracts.procedures.line_specs import CaptureLandingTriggerPolicyV2
from cruxible_core.exhaust.line_dispatch import LineDispatchStore
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_evaluate_line,
    service_listen_line,
    service_match_listening_lines,
)
from cruxible_core.service.procedures.line_triggers import service_check_line_trigger
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequestV1,
    _journal,
    _stream,
    service_run_playbill_line,
)
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor


def queued_world(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    capture(instance, procedure)
    now = READ_TIME + timedelta(seconds=2)
    result = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=READ_TIME, until=now),
        actor=_actor(instance),
        now=now,
    )
    assert result.status == "met" and result.occurrences[0].pending
    return instance, line, procedure, result.occurrences[0], now


def test_explicit_evaluation_and_pending_rebuild_do_not_execute(tmp_path):
    instance, line, _, occurrence, now = queued_world(tmp_path)
    store = LineDispatchStore(instance)
    before = store.journal.read_head(store.stream, "dispatch")
    store.path.unlink()
    check = service_check_line_trigger(
        instance, line.identity.name, LineTriggerCheckRequestV1(), now=now
    )
    assert check.occurrences[0].pending
    assert check.occurrences[0].admitted_run_id is None
    assert store.journal.read_head(store.stream, "dispatch") == before
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=READ_TIME, until=now),
        actor=_actor(instance),
        now=now,
    )
    assert store.journal.read_head(store.stream, "dispatch") == before
    with store.locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending WHERE admitted=0").fetchone()[0] == 1


def test_listener_restart_keeps_pending_and_leaves_downtime_for_explicit_evaluation(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    capture(instance, procedure)  # before subscribing: never auto-consumed
    actor = _actor(instance)
    start = READ_TIME + timedelta(seconds=10)
    session = service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=start,
        daemon_id="first",
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    service_match_listening_lines(
        instance, actor=actor, now=start + timedelta(seconds=2), daemon_id="first"
    )
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    capture(instance, procedure, at=start + timedelta(seconds=3))  # daemon is stopped
    restarted = start + timedelta(seconds=10)
    service_match_listening_lines(instance, actor=actor, now=restarted, daemon_id="second")
    service_match_listening_lines(
        instance, actor=actor, now=restarted + timedelta(seconds=1), daemon_id="second"
    )
    with store.locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
        old = json.loads(
            conn.execute(
                "SELECT payload FROM sessions WHERE session_id=?", (session.session_id,)
            ).fetchone()[0]
        )
        assert old["stops_at"] == old["evaluated_until"]
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=start + timedelta(seconds=2), until=restarted),
        actor=actor,
        now=restarted,
    )
    assert len(evaluated.occurrences) == 1 and evaluated.occurrences[0].pending
    with store.locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 2


def test_dispatch_races_explicit_line_run_admits_exactly_once(tmp_path):
    instance, line, _, occurrence, now = queued_world(tmp_path)
    barrier = Barrier(2)
    actor = _actor(instance)

    def dispatch():
        barrier.wait()
        return service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=actor,
            now=now,
            caller_rung=3,
        )

    def explicit():
        barrier.wait()
        return service_run_playbill_line(
            instance,
            path_identity_digest=line.identity.name,
            request=LineRunRequestV1(
                line=line.identity.name, trigger_event=occurrence.binding.event
            ),
            actor_context=actor,
            caller_rung=3,
            daemon_clock=SimpleNamespace(now=lambda: now),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(dispatch), pool.submit(explicit)
        dispatched, run = left.result(), right.result()
    assert run.status == "succeeded", run
    assert dispatched.items[0].status == "admitted", dispatched
    assert dispatched.items[0].run_id == run.run_id
    journal, _ = _journal(instance)
    assert len(journal.select_records(_stream(instance), event_kind="admission_bound")) == 1


def test_lost_dispatch_completion_reuses_admission_after_projection_rebuild(tmp_path, monkeypatch):
    instance, line, _, occurrence, now = queued_world(tmp_path)
    original = LineDispatchStore.append

    def append(self, conn, kind, *args, **kwargs):
        if kind == "admitted":
            raise RuntimeError("lost completion")
        return original(self, conn, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(LineDispatchStore, "append", append)
        with pytest.raises(RuntimeError, match="lost completion"):
            service_dispatch_line(
                instance,
                line.identity.name,
                LineDispatchRequestV1(),
                actor=_actor(instance),
                now=now,
                caller_rung=3,
            )
    LineDispatchStore(instance).path.unlink()
    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=_actor(instance),
        now=now + timedelta(minutes=1),
        caller_rung=3,
    )
    assert result.items[0].status == "admitted"
    journal, _ = _journal(instance)
    assert len(journal.select_records(_stream(instance), event_kind="admission_bound")) == 1
    assert (
        service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=_actor(instance),
            now=now,
            caller_rung=3,
        ).items
        == ()
    )


def test_dispatch_refusal_leaves_exact_pending_binding(tmp_path):
    instance, line, _, occurrence, now = queued_world(tmp_path)
    now = now.replace(year=2028)  # the governed execution mandate has expired
    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=_actor(instance),
        now=now,
        caller_rung=0,
    )
    assert result.items[0].status == "blocked"
    check = service_check_line_trigger(
        instance, line.identity.name, LineTriggerCheckRequestV1(), now=now
    )
    assert check.occurrences[0].pending
    assert check.occurrences[0].binding == occurrence.binding


@pytest.mark.parametrize("event_relative", [False, True])
def test_listener_retains_window_boundaries_and_dispatches_only_when_closed(
    tmp_path, event_relative
):
    from cruxible_client.contracts.procedures.line_specs import WindowCloseTriggerPolicyV2
    from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1, FixedWindowV1

    window = (
        CaptureEventWindowV1(event=SELECTOR, duration_seconds=60)
        if event_relative
        else FixedWindowV1(starts_at=READ_TIME, duration_seconds=60)
    )
    instance, line, procedure = line_world(tmp_path, WindowCloseTriggerPolicyV2(window=window))
    actor = _actor(instance)
    service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    if event_relative:
        capture(instance, procedure)
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=1), daemon_id="daemon"
    )
    early = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=READ_TIME + timedelta(seconds=2),
        caller_rung=3,
    )
    if event_relative:
        assert early.items[0].status == "pending"
    else:
        assert early.items == ()
    end = READ_TIME + timedelta(seconds=60)
    service_match_listening_lines(
        instance, actor=actor, now=end + timedelta(seconds=1), daemon_id="daemon"
    )
    late = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=end + timedelta(hours=1),
        caller_rung=3,
    )
    assert late.items[0].status == "admitted", late
    check = service_check_line_trigger(
        instance, line.identity.name, LineTriggerCheckRequestV1(), now=end + timedelta(hours=1)
    )
    assert check.occurrences[0].binding.window.ends_at == end


def test_cadence_has_one_pending_occurrence_and_retains_its_first_due_instant(tmp_path):
    from cruxible_client.contracts.procedures.line_specs import CadenceTriggerPolicyV1

    instance, line, _ = line_world(
        tmp_path,
        CadenceTriggerPolicyV1(interval_seconds=60, cadence_policy_digest="sha256:" + "d" * 64),
    )
    actor = _actor(instance)
    service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    service_match_listening_lines(instance, actor=actor, now=READ_TIME, daemon_id="daemon")
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=30), daemon_id="daemon"
    )
    store = LineDispatchStore(instance)
    with store.locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    checked = service_check_line_trigger(
        instance,
        line.identity.name,
        LineTriggerCheckRequestV1(),
        now=READ_TIME + timedelta(seconds=30),
    )
    assert checked.occurrences[0].pending
    assert checked.occurrences[0].eligible_at == READ_TIME
    result = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=READ_TIME + timedelta(seconds=30),
        caller_rung=3,
    )
    assert result.items[0].status == "admitted", result


def test_event_index_rebuild_never_replays_history_or_loses_pending(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    actor = _actor(instance)
    service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    capture(instance, procedure)
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=1), daemon_id="daemon"
    )
    capture(instance, procedure, at=READ_TIME + timedelta(seconds=2))
    journal, _ = _journal(instance)
    journal.index.path.unlink()
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=3), daemon_id="daemon"
    )
    with LineDispatchStore(instance).locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sessions WHERE active=1").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM sessions WHERE active=0").fetchone()[0] == 1


def test_daemon_listener_matches_without_execution(tmp_path, monkeypatch):
    from datetime import UTC, datetime
    from threading import Event

    from cruxible_core.runtime.line_listener import LineListener

    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    monkeypatch.setattr(
        "cruxible_core.runtime.line_listener.get_registry",
        lambda: SimpleNamespace(
            list_instances=lambda: (
                SimpleNamespace(
                    instance_id=instance.descriptor.instance_id,
                    backend="governed_daemon",
                    location=str(instance.root),
                ),
            )
        ),
    )
    listener = LineListener(SimpleNamespace(get=lambda _id: instance))
    finished = Event()
    original = LineDispatchStore.append

    def append(self, conn, kind, *args, **kwargs):
        result = original(self, conn, kind, *args, **kwargs)
        if kind == "pending":
            finished.set()
        return result

    monkeypatch.setattr(LineDispatchStore, "append", append)
    listener.start()
    try:
        service_listen_line(
            instance,
            line.identity.name,
            LineListenRequestV1(action="start"),
            actor=_actor(instance),
            now=datetime.now(UTC),
            daemon_id=listener.daemon_id,
        )
        capture(instance, procedure)
        assert finished.wait(10), "listener did not match the new retained capture"
    finally:
        listener.close()
    journal, _ = _journal(instance)
    assert journal.select_records(_stream(instance), event_kind="admission_bound") == ()


@pytest.mark.parametrize("change", ["rebind", "epoch", "concurrent_epoch"])
def test_rebinding_keeps_subscription_but_new_epoch_requires_explicit_start(
    tmp_path, change, monkeypatch
):
    new_epoch = change != "rebind"
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, line, procedure, owner = line_world(
        tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR), with_owner=True
    )
    actor = _actor(instance)
    session = service_listen_line(
        instance,
        line.identity.name,
        LineListenRequestV1(action="start"),
        actor=actor,
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    from cruxible_client.contracts.procedures.line_specs import WindowCloseTriggerPolicyV2
    from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1

    successor = line.model_copy(
        update={
            "trigger_policy": WindowCloseTriggerPolicyV2(
                window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=1)
            )
            if new_epoch
            else line.trigger_policy,
            "parameters": {"status": "closed"},
            "occurrence_epoch": line.occurrence_epoch + int(new_epoch),
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[line_spec_path(line.identity.name)] = render_line_spec(successor)

    def accept_successor():
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp="2026-08-28T15:02:00.000000Z",
            proposal_name="rebind-line",
        )

    if change == "concurrent_epoch":
        import cruxible_core.service.procedures.line_dispatch as dispatch_service

        original = dispatch_service.service_check_line_trigger

        def check(*args, **kwargs):
            accept_successor()
            return original(*args, **kwargs)

        monkeypatch.setattr(dispatch_service, "service_check_line_trigger", check)
    else:
        accept_successor()
    capture(instance, procedure)
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=2), daemon_id="daemon"
    )
    with LineDispatchStore(instance).locked() as conn:
        row = conn.execute(
            "SELECT active,payload FROM sessions WHERE session_id=?", (session.session_id,)
        ).fetchone()
        assert bool(row[0]) == (not new_epoch)
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == (0 if new_epoch else 1)
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_one_capture_can_leave_independent_pending_work_for_two_lines(tmp_path):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, first, procedure, owner = line_world(
        tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR), with_owner=True
    )
    second = first.model_copy(update={"identity": ArtifactIdentity(kind="Line", name="other-line")})
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[line_spec_path(second.identity.name)] = render_line_spec(second)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="second-line"
    )
    actor = _actor(instance)
    for line in (first, second):
        service_listen_line(
            instance,
            line.identity.name,
            LineListenRequestV1(action="start"),
            actor=actor,
            now=READ_TIME - timedelta(seconds=1),
            daemon_id="daemon",
        )
    capture(instance, procedure)
    now = READ_TIME + timedelta(seconds=2)
    service_match_listening_lines(instance, actor=actor, now=now, daemon_id="daemon")
    first_result = service_dispatch_line(
        instance, first.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=3
    )
    assert first_result.items[0].status == "admitted"
    LineDispatchStore(instance).path.unlink()
    with LineDispatchStore(instance).locked() as conn:
        assert conn.execute("SELECT count(*) FROM pending WHERE admitted=0").fetchone()[0] == 1
    second_result = service_dispatch_line(
        instance, second.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=3
    )
    assert second_result.items[0].status == "admitted"
    assert first_result.items[0].run_id != second_result.items[0].run_id
