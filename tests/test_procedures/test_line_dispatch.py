"""Pending work survives loss of process/projection without implicit catch-up."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipalV1,
    LineDispatchRequestV1,
    LineEvaluateRequestV1,
    LineTriggerCheckRequestV1,
)
from cruxible_client.contracts.procedures.line_specs import CaptureLandingTriggerPolicyV2
from cruxible_core.exhaust.line_dispatch import LineDispatchStore
from cruxible_core.service.procedures.line_dispatch import (
    service_arm_line,
    service_dispatch_line,
    service_evaluate_line,
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

LOCAL_OPERATOR = LineArmPrincipalV1(kind="local_operator", label="local-operator")


def _active_segment(instance) -> str:  # type: ignore[no-untyped-def]
    """The one active arm segment's session id."""

    with LineDispatchStore(instance).locked() as conn:
        (row,) = conn.execute("SELECT session_id FROM sessions WHERE active=1").fetchall()
    return row[0]


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
        assert (
            conn.execute("SELECT count(*) FROM pending WHERE disposition='pending'").fetchone()[0]
            == 1
        )


def test_listener_restart_keeps_pending_and_leaves_downtime_for_explicit_evaluation(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    capture(instance, procedure)  # before subscribing: never auto-consumed
    actor = _actor(instance)
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
        actor=actor,
        now=start,
        daemon_id="first",
    )
    segment = _active_segment(instance)
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
            conn.execute("SELECT payload FROM sessions WHERE session_id=?", (segment,)).fetchone()[
                0
            ]
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
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
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
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
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
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
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

    from cruxible_core.consumers.runner import ConsumerRunner

    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    monkeypatch.setattr(
        "cruxible_core.consumers.runner.get_registry",
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
    listener = ConsumerRunner(SimpleNamespace(get=lambda _id: instance))
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
        service_arm_line(
            instance,
            line.identity.name,
            principal=LOCAL_OPERATOR,
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
def test_any_line_change_stops_the_arm_until_it_is_rearmed(tmp_path, change, monkeypatch):
    """An arm is pinned to the Line version it was armed under, epoch or not."""

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
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
        actor=actor,
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    segment = _active_segment(instance)
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
            "SELECT active,payload FROM sessions WHERE session_id=?", (segment,)
        ).fetchone()
        assert not row[0]
        expected = "epoch_changed" if new_epoch else "line_changed"
        assert json.loads(row[1])["stop_reason"] == expected
        # Nothing was matched under a version the arm was not bound to.
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
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
        service_arm_line(
            instance,
            line.identity.name,
            principal=LOCAL_OPERATOR,
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
        assert (
            conn.execute("SELECT count(*) FROM pending WHERE disposition='pending'").fetchone()[0]
            == 1
        )
    second_result = service_dispatch_line(
        instance, second.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=3
    )
    assert second_result.items[0].status == "admitted"
    assert first_result.items[0].run_id != second_result.items[0].run_id


def test_idle_coverage_is_checkpointed_and_restart_claims_only_durable_range(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    actor = _actor(instance)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL_OPERATOR,
        actor=actor,
        now=READ_TIME,
        daemon_id="first",
    )
    segment = _active_segment(instance)
    store = LineDispatchStore(instance)
    initial = store.journal.read_head(store.stream, "dispatch")
    for seconds in range(1, 60):
        service_match_listening_lines(
            instance, actor=actor, now=READ_TIME + timedelta(seconds=seconds), daemon_id="first"
        )
    assert store.journal.read_head(store.stream, "dispatch") == initial
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=60), daemon_id="first"
    )
    checkpoint = store.journal.read_head(store.stream, "dispatch")
    assert checkpoint != initial
    # Actual event progress is retained immediately, even inside the idle interval.
    capture(instance, procedure, at=READ_TIME + timedelta(seconds=61))
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=62), daemon_id="first"
    )
    with store.locked() as conn:
        durable = json.loads(conn.execute("SELECT payload FROM sessions").fetchone()[0])
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=63), daemon_id="first"
    )
    store.path.unlink()
    service_match_listening_lines(
        instance, actor=actor, now=READ_TIME + timedelta(seconds=65), daemon_id="second"
    )
    with store.locked() as conn:
        old = json.loads(
            conn.execute("SELECT payload FROM sessions WHERE session_id=?", (segment,)).fetchone()[
                0
            ]
        )
        assert old["stops_at"] == durable["evaluated_until"]
        assert conn.execute("SELECT count(*) FROM pending").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["rebind", "epoch", "race"])
def test_superseded_pending_requires_explicit_reconciliation_and_survives_rebuild(
    tmp_path, change, monkeypatch
):
    new_epoch = change == "epoch"
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
    capture(instance, procedure)
    now = READ_TIME + timedelta(seconds=2)
    actor = _actor(instance)
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=READ_TIME, until=now),
        actor=actor,
        now=now,
    )
    occurrence = evaluated.occurrences[0]
    successor = line.model_copy(
        update={
            "parameters": {"status": "closed"},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    if new_epoch:
        from cruxible_client.contracts.procedures.line_specs import WindowCloseTriggerPolicyV2
        from cruxible_client.contracts.procedures.windows import CaptureEventWindowV1

        successor = successor.model_copy(
            update={
                "occurrence_epoch": line.occurrence_epoch + 1,
                "trigger_policy": WindowCloseTriggerPolicyV2(
                    window=CaptureEventWindowV1(event=SELECTOR, duration_seconds=1)
                ),
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
            proposal_name="rebind-pending",
        )

    if change == "race":
        import cruxible_core.service.procedures.line_dispatch as dispatch_service

        original_run = dispatch_service.service_run_playbill_line

        def run_after_acceptance(*args, **kwargs):
            accept_successor()
            monkeypatch.setattr(dispatch_service, "service_run_playbill_line", original_run)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(dispatch_service, "service_run_playbill_line", run_after_acceptance)
    else:
        accept_successor()
    result = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequestV1(), actor=actor, now=now, caller_rung=3
    )
    assert result.items[0].status == "superseded"
    assert result.items[0].refusal.code == "line_binding_superseded"
    store = LineDispatchStore(instance)
    store.path.unlink()
    assert (
        service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=actor,
            now=now,
            caller_rung=3,
        ).items
        == ()
    )
    with store.locked() as conn:
        row = conn.execute("SELECT payload,disposition FROM pending").fetchone()
        assert row[1] == "superseded"
        assert json.loads(row[0])["line_artifact_digest"] == evaluated.line_artifact_digest
    request = LineDispatchRequestV1(occurrence_id=occurrence.occurrence_id, retry=True)
    retry = service_dispatch_line(
        instance, line.identity.name, request, actor=actor, now=now, caller_rung=3
    )
    if new_epoch:
        assert retry.items[0].status == "superseded"
        evaluated_new = service_evaluate_line(
            instance,
            line.identity.name,
            LineEvaluateRequestV1(since=READ_TIME, until=now),
            actor=actor,
            now=now,
        )
        assert evaluated_new.occurrences[0].occurrence_id != occurrence.occurrence_id
        fresh = service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=actor,
            now=now,
            caller_rung=3,
        )
        assert fresh.items[0].status == "admitted", fresh
        return
    assert retry.items[0].status == "admitted", retry
    store.path.unlink()
    again = service_dispatch_line(
        instance, line.identity.name, request, actor=actor, now=now, caller_rung=3
    )
    assert again.items[0].run_id == retry.items[0].run_id
    with store.locked() as conn:
        row = json.loads(conn.execute("SELECT payload FROM pending").fetchone()[0])
        assert row["line_artifact_digest"] == line_spec_digest(successor).tagged
        assert row["occurrence"]["binding"] == occurrence.binding.model_dump(mode="json")


def test_retry_requires_one_explicit_occurrence():
    with pytest.raises(ValueError, match="retry requires"):
        LineDispatchRequestV1(retry=True)
    with pytest.raises(ValueError, match="retry requires"):
        LineDispatchRequestV1(retry=True, occurrence_id="one", limit=2)


@pytest.mark.parametrize(
    "failure", ["selector", "record", "run", "absent", "payload_absent", "future", "backend"]
)
def test_event_refusals_close_only_unusable_occurrences(tmp_path, monkeypatch, failure):
    from cruxible_client.contracts.errors import PlaybillExecutionError
    from cruxible_client.contracts.line_dispatch import LineTriggerOccurrenceV1
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from cruxible_client.contracts.procedures.windows import (
        LineTriggerBindingV1,
        TriggerEventReferenceV1,
    )
    from cruxible_core.service.procedures import resolution_contracts
    from cruxible_core.service.procedures.procedure_runs import (
        _accepted_line_by_reference,
        _line_occurrence,
    )

    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
    actor = _actor(instance)
    first = capture(
        instance,
        procedure,
        at=READ_TIME + timedelta(seconds=10) if failure == "future" else READ_TIME,
        digest="sha256:" + "b" * 64 if failure == "selector" else SELECTOR.capture_contract_digest,
    )
    second = capture(
        instance,
        procedure,
        at=READ_TIME + timedelta(seconds=1),
        partition="run:second",
        observed_at="2000-01-02T00:00:00Z",
    )
    now = READ_TIME + timedelta(seconds=2)
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line.identity.name
    )
    store = LineDispatchStore(instance)
    occurrence_ids = []
    for ordinal, stored in enumerate((first, second)):
        event = TriggerEventReferenceV1(
            run_id=stored.record.run_id,
            partition_id=stored.record.partition_id,
            sequence=stored.record.sequence,
            record_digest=stored.record_digest,
        )
        if ordinal == 0:
            updates = {
                "record": {"record_digest": "sha256:" + "c" * 64},
                "run": {"run_id": "RUN-other"},
                "absent": {"sequence": 999},
            }.get(failure, {})
            event = event.model_copy(update=updates)
        binding = LineTriggerBindingV1(kind="capture_landing", event=event)
        occurrence_id, _ = _line_occurrence(
            accepted, evaluation_time=READ_TIME, prior=(), binding=binding
        )
        occurrence_ids.append(occurrence_id)
        # Seed retained pending work with a bad reference, modeling a damaged or
        # incorrectly matched historical occurrence. Do not tamper with journal bytes.
        with store.locked() as conn:
            store.append(
                conn,
                "pending",
                {
                    "line": line.identity.qualified,
                    "line_identity_digest": line_identity_digest(line.identity),
                    "line_artifact_digest": accepted.artifact_digest,
                    "occurrence_epoch": line.occurrence_epoch,
                    "coordinate": stored.record.accepted_coordinate.model_dump(mode="json"),
                    "occurrence": LineTriggerOccurrenceV1(
                        occurrence_id=occurrence_id,
                        binding=binding,
                        eligible_at=READ_TIME + timedelta(seconds=ordinal),
                    ).model_dump(mode="json"),
                },
                actor=actor,
                now=now,
            )
    if failure == "payload_absent":
        instance.body_store().erase(first.record.payload_digest)
    if failure == "backend":

        def unavailable(*args, **kwargs):
            raise PlaybillExecutionError("temporary execution backend failure")

        monkeypatch.setattr(
            "cruxible_core.service.procedures.procedure_runs.capture_event_time", unavailable
        )
    item = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=now,
        caller_rung=3,
    ).items[0]
    assert item.occurrence_id == occurrence_ids[0]
    store.path.unlink()
    if failure in {"future", "backend"}:
        assert item.status == "blocked"
        if failure == "future":
            assert item.refusal.code == "trigger_capture_not_yet_observed"
            assert item.refusal.retryable
        else:
            assert item.refusal is None
            monkeypatch.setattr(
                "cruxible_core.service.procedures.procedure_runs.capture_event_time",
                resolution_contracts.capture_event_time,
            )
        later = service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=actor,
            now=READ_TIME + timedelta(seconds=11),
            caller_rung=3,
        ).items[0]
        assert later.status == "admitted" and later.occurrence_id == occurrence_ids[0]
    else:
        assert item.status == "rejected"
        assert item.refusal.code == (
            "trigger_capture_unavailable"
            if failure in {"absent", "payload_absent"}
            else "trigger_capture_invalid"
        )
        assert not item.refusal.retryable
        assert item.refusal.repair.hand_edit.required_change
    next_item = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=actor,
        now=READ_TIME + timedelta(seconds=12),
        caller_rung=3,
    ).items[0]
    assert next_item.status == "admitted" and next_item.occurrence_id == occurrence_ids[1]
