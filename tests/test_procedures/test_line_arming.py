"""An armed Line admits only what it matched itself, under authority rechecked each time."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.line_dispatch import (
    LineArmPrincipalV1,
    LineDispatchRequestV1,
    LineEvaluateRequestV1,
)
from cruxible_client.contracts.triggers import CadenceScheduleV1, CaptureLandingScheduleV1
from cruxible_core.consumers.lines import LINE_ARMS
from cruxible_core.consumers.protocol import ConsumerWork
from cruxible_core.consumers.runner import ConsumerRunner
from cruxible_core.runtime import line_arms
from cruxible_core.runtime.line_arms import arm_authority, dispatch_armed_line
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.credentials import RuntimeCredentialRecord
from cruxible_core.service.procedures.line_dispatch import (
    LineArmAuthorityLost,
    armed_work,
    service_arm_line,
    service_disarm_line,
    service_dispatch_line,
    service_evaluate_line,
    service_line_status,
    service_match_listening_lines,
)
from cruxible_core.service.procedures.procedure_runs import _journal, _stream
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor

LOCAL = LineArmPrincipalV1(kind="local_operator", label="operator")
CREDENTIAL = LineArmPrincipalV1(
    kind="runtime_credential", credential_id="cred-arm", label="line-operator"
)


def _manager(instance):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        get=lambda _id: instance,
        workspace_file_reader=lambda _id: None,
        provider_runtime_operator=lambda: None,
    )


def _admissions(instance) -> int:  # type: ignore[no-untyped-def]
    journal, _ = _journal(instance)
    return len(journal.select_records(_stream(instance), event_kind="admission_bound"))


def _armed_world(tmp_path, *, principal=LOCAL):  # type: ignore[no-untyped-def]
    instance, line, procedure = line_world(tmp_path, CaptureLandingScheduleV1(event=SELECTOR))
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=principal,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    return instance, line, procedure, start


def _match(instance, at, daemon_id="daemon"):  # type: ignore[no-untyped-def]
    service_match_listening_lines(instance, actor=_actor(instance), now=at, daemon_id=daemon_id)


def _credential(**update):  # type: ignore[no-untyped-def]
    record = RuntimeCredentialRecord(
        credential_id="cred-arm",
        instance_id="",
        label="line-operator",
        permission_mode=PermissionMode.GOVERNED_WRITE,
        token_hash="unused",
        created_at="2026-09-01T00:00:00Z",
        principal_id="owner",
    )
    return record if not update else record.__class__(**{**record.__dict__, **update})


def _credential_store(monkeypatch, record):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(get=lambda _id: record),
    )


def test_an_armed_line_admits_the_occurrence_its_daemon_matched(tmp_path, monkeypatch):
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    result = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )

    assert result is not None and [item.status for item in result.items] == ["admitted"]
    assert _admissions(instance) == 1
    status = service_line_status(instance, line.identity.name)
    assert status.state == "armed" and status.pending_automatic == 0


def test_the_daemon_listener_runs_armed_work_on_its_own(tmp_path, monkeypatch):
    instance, line, procedure = line_world(tmp_path, CaptureLandingScheduleV1(event=SELECTOR))
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
    listener = ConsumerRunner(_manager(instance))
    listener.start()
    try:
        service_arm_line(
            instance,
            line.identity.name,
            principal=LOCAL,
            actor=_actor(instance),
            now=datetime.now(UTC),
            daemon_id=listener.daemon_id,
        )
        capture(instance, procedure, at=datetime.now(UTC))
        deadline = datetime.now(UTC) + timedelta(seconds=20)
        while _admissions(instance) == 0 and datetime.now(UTC) < deadline:
            threading.Event().wait(0.2)
    finally:
        listener.close()
    assert _admissions(instance) == 1


def test_a_restart_keeps_the_arm_forward_only_and_leaves_earlier_work_explicit(tmp_path):
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    restarted = start + timedelta(seconds=10)
    capture(instance, procedure, at=start + timedelta(seconds=3))  # while the daemon is down
    _match(instance, restarted, daemon_id="restarted")

    status = service_line_status(instance, line.identity.name)
    assert status.state == "armed"
    # What the pre-restart segment matched is never run implicitly.
    assert status.pending_automatic == 0 and status.pending_explicit == 1
    assert armed_work(instance, now=restarted + timedelta(seconds=1)) == ()
    # Downtime is not evaluated either: only the one pre-restart match exists.
    assert _admissions(instance) == 0

    capture(instance, procedure, at=restarted + timedelta(seconds=1))
    _match(instance, restarted + timedelta(seconds=2), daemon_id="restarted")
    (arm,) = armed_work(instance, now=restarted + timedelta(seconds=2))
    result = dispatch_armed_line(
        _manager(instance),
        instance.descriptor.instance_id,
        arm,
        now=restarted + timedelta(seconds=3),
    )
    assert result is not None and [item.status for item in result.items] == ["admitted"]
    assert service_line_status(instance, line.identity.name).pending_explicit == 1


def test_arming_never_drains_a_backlog_that_explicit_evaluation_left(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingScheduleV1(event=SELECTOR))
    capture(instance, procedure)
    now = READ_TIME + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=READ_TIME, until=now),
        actor=_actor(instance),
        now=now,
    )
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=now,
        daemon_id="daemon",
    )
    _match(instance, now + timedelta(seconds=5))

    assert armed_work(instance, now=now + timedelta(seconds=5)) == ()
    status = service_line_status(instance, line.identity.name)
    assert (status.pending_automatic, status.pending_explicit) == (0, 1)


def test_disarming_stops_admission_of_work_already_scheduled(tmp_path):
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    disarmed = service_disarm_line(
        instance, line.identity.name, actor=_actor(instance), now=start + timedelta(seconds=3)
    )
    assert disarmed.state == "stopped" and disarmed.stop_reason == "disarmed"

    assert (
        dispatch_armed_line(
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=4),
        )
        is None
    )
    assert _admissions(instance) == 0
    assert service_line_status(instance, line.identity.name).pending_explicit == 1


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("revoked", "credential_revoked"),
        ("missing", "credential_revoked"),
        ("other_instance", "credential_scope_changed"),
        ("read_only", "permission_insufficient"),
    ],
)
def test_a_credential_that_no_longer_holds_stops_the_arm_before_admission(
    tmp_path, monkeypatch, record, reason
):
    instance, line, procedure, start = _armed_world(tmp_path, principal=CREDENTIAL)
    instance_id = instance.descriptor.instance_id
    current = _credential(instance_id=instance_id)
    _credential_store(
        monkeypatch,
        {
            "revoked": _credential(instance_id=instance_id, revoked_at="2026-09-02T00:00:00Z"),
            "missing": None,
            "other_instance": _credential(instance_id="another-instance"),
            "read_only": _credential(
                instance_id=instance_id, permission_mode=PermissionMode.READ_ONLY
            ),
        }[record],
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    later = start + timedelta(seconds=4)
    assert dispatch_armed_line(_manager(instance), instance_id, arm, now=later) is None
    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", reason)
    assert status.pending_explicit == 1  # the occurrence stays for explicit dispatch

    # Explicit dispatch under a caller's own authority is unaffected.
    _credential_store(monkeypatch, current)
    explicit = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(),
        actor=_actor(instance),
        now=start + timedelta(seconds=5),
        caller_rung=3,
    )
    assert [item.status for item in explicit.items] == ["admitted"]


def test_a_revocation_between_two_admissions_stops_the_second(tmp_path, monkeypatch):
    instance, line, procedure, start = _armed_world(tmp_path, principal=CREDENTIAL)
    instance_id = instance.descriptor.instance_id
    records = [_credential(instance_id=instance_id)]
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(get=lambda _id: records[-1]),
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    capture(instance, procedure, at=start + timedelta(seconds=2))
    _match(instance, start + timedelta(seconds=3))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=3))

    import cruxible_core.service.procedures.line_dispatch as dispatch_service

    original = dispatch_service.service_run_playbill_line

    def revoke_after_first(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        records.append(_credential(instance_id=instance_id, revoked_at="2026-09-02T00:00:00Z"))
        return result

    monkeypatch.setattr(dispatch_service, "service_run_playbill_line", revoke_after_first)
    later = start + timedelta(seconds=4)
    assert dispatch_armed_line(_manager(instance), instance_id, arm, now=later) is None
    assert _admissions(instance) == 1
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", "credential_revoked")


def test_automatic_and_explicit_dispatch_admit_one_occurrence_once(tmp_path):
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    barrier = Barrier(2)
    now = start + timedelta(seconds=3)

    def automatic():  # type: ignore[no-untyped-def]
        barrier.wait()
        return dispatch_armed_line(
            _manager(instance), instance.descriptor.instance_id, arm, now=now
        )

    def explicit():  # type: ignore[no-untyped-def]
        barrier.wait()
        return service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=_actor(instance),
            now=now,
            caller_rung=3,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(automatic), pool.submit(explicit)]:
            future.result()
    assert _admissions(instance) == 1


def test_a_slow_line_never_stalls_another_lines_drain_or_runs_twice(monkeypatch):
    listener = ConsumerRunner(SimpleNamespace())
    listener.start()
    released, slow_entered, fast_done = Event(), Event(), Event()
    calls: list[str] = []

    def drain(_manager, _instance_id, arm, **_kwargs):  # type: ignore[no-untyped-def]
        calls.append(arm["line_id"])
        if arm["line_id"] == "slow":
            slow_entered.set()
            released.wait(10)
        else:
            fast_done.set()

    monkeypatch.setattr(line_arms, "dispatch_armed_line", drain)
    try:
        listener._schedule("instance", LINE_ARMS, ConsumerWork("slow", {"line_id": "slow"}))
        assert slow_entered.wait(5)
        # Still draining: not rescheduled.
        listener._schedule("instance", LINE_ARMS, ConsumerWork("slow", {"line_id": "slow"}))
        listener._schedule("instance", LINE_ARMS, ConsumerWork("fast", {"line_id": "fast"}))
        assert fast_done.wait(5), "a slow Line's drain held up another Line"
        assert calls == ["slow", "fast"]
    finally:
        released.set()
        listener.close()


def test_local_operator_arms_stop_once_the_daemon_requires_authentication(monkeypatch):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: True)
    with pytest.raises(LineArmAuthorityLost) as lost:
        arm_authority(_registry_instance("owner", "active"), LOCAL, now=datetime.now(UTC))
    assert lost.value.reason == "authentication_changed"


def test_an_automatic_run_acts_as_the_arming_credential(monkeypatch):
    _credential_store(monkeypatch, _credential(instance_id="instance"))
    actor, caller_rung = arm_authority(
        _registry_instance("owner", "active"), CREDENTIAL, now=datetime.now(UTC)
    )
    assert (actor.actor_type, actor.actor_id) == ("service_account", "owner")
    assert caller_rung == PermissionMode.GOVERNED_WRITE.value - 1


def _stalled(instance, at):  # type: ignore[no-untyped-def]
    from cruxible_core.consumers.lines import LINE_STALL_AFTER
    from cruxible_core.service.procedures.line_dispatch import line_arm_health

    return tuple(
        arm
        for state, arm in line_arm_health(instance, now=at, stall_after=LINE_STALL_AFTER)
        if state != "running"
    )


def test_a_deliberate_disarm_is_not_a_stall_but_undrained_armed_work_is(tmp_path):
    from cruxible_core.consumers.lines import LINE_STALL_AFTER

    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    assert _stalled(instance, start + timedelta(seconds=3)) == ()
    (stalled,) = _stalled(instance, start + LINE_STALL_AFTER + timedelta(seconds=2))
    assert (stalled.state, stalled.pending_automatic) == ("armed", 1)

    service_disarm_line(
        instance, line.identity.name, actor=_actor(instance), now=start + timedelta(minutes=30)
    )
    assert _stalled(instance, start + timedelta(minutes=31)) == ()


def test_each_automatic_admission_uses_the_authority_resolved_for_it(tmp_path, monkeypatch):
    import cruxible_core.service.procedures.line_dispatch as dispatch_service

    instance, line, procedure, start = _armed_world(tmp_path, principal=CREDENTIAL)
    instance_id = instance.descriptor.instance_id
    records = [_credential(instance_id=instance_id, permission_mode=PermissionMode.ADMIN)]
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(get=lambda _id: records[-1]),
    )
    for offset in (1, 2):
        capture(instance, procedure, at=start + timedelta(seconds=offset))
    _match(instance, start + timedelta(seconds=3))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=3))
    original = dispatch_service.service_run_playbill_line
    seen: list[int] = []

    def downgrade_after_first(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs["caller_rung"])
        result = original(*args, **kwargs)
        records.append(
            _credential(instance_id=instance_id, permission_mode=PermissionMode.GOVERNED_WRITE)
        )
        return result

    monkeypatch.setattr(dispatch_service, "service_run_playbill_line", downgrade_after_first)
    dispatch_armed_line(_manager(instance), instance_id, arm, now=start + timedelta(seconds=4))

    assert seen == [PermissionMode.ADMIN.value - 1, PermissionMode.GOVERNED_WRITE.value - 1]


def test_a_disarm_that_lands_before_the_admission_record_prevents_the_run(tmp_path, monkeypatch):
    import cruxible_core.service.procedures.line_dispatch as dispatch_service

    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    entered, proceed = Event(), Event()
    original = dispatch_service.service_run_playbill_line

    def paused(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Past every pre-admission check, before the admission is recorded.
        entered.set()
        assert proceed.wait(15)
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatch_service, "service_run_playbill_line", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            dispatch_armed_line,
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=3),
        )
        assert entered.wait(15)
        try:
            stopped = service_disarm_line(
                instance,
                line.identity.name,
                actor=_actor(instance),
                now=start + timedelta(seconds=4),
            )
            assert stopped.state == "stopped"
        finally:
            proceed.set()
        future.result()

    assert _admissions(instance) == 0
    assert service_line_status(instance, line.identity.name).pending_explicit == 1


def test_a_same_epoch_revision_accepted_during_matching_never_runs_under_the_old_arm(
    tmp_path, monkeypatch
):
    import cruxible_core.service.procedures.line_dispatch as dispatch_service
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, line, procedure, owner = line_world(
        tmp_path, CaptureLandingScheduleV1(event=SELECTOR), with_owner=True
    )
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    capture(instance, procedure)
    successor = line.model_copy(
        update={
            "parameters": {"status": "closed"},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[line_spec_path(line.identity.name)] = render_line_spec(successor)
    original = dispatch_service.service_check_line_trigger

    def revised_meanwhile(*args, **kwargs):  # type: ignore[no-untyped-def]
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp="2026-08-28T15:02:00.000000Z",
            proposal_name="revision-during-matching",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatch_service, "service_check_line_trigger", revised_meanwhile)
    _match(instance, READ_TIME + timedelta(seconds=2))
    for arm in armed_work(instance, now=READ_TIME + timedelta(seconds=3)):
        dispatch_armed_line(
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=READ_TIME + timedelta(seconds=3),
        )

    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", "line_changed")


def _cadence_world(tmp_path):  # type: ignore[no-untyped-def]
    return line_world(tmp_path, CadenceScheduleV1(interval_seconds=60))


def test_a_restart_lapses_the_pending_cadence_tick_and_the_arm_keeps_ticking(tmp_path):
    from cruxible_core.service.procedures.line_triggers import service_check_line_trigger

    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME)
    assert service_line_status(instance, line.identity.name).pending_automatic == 1
    lapsed_id = next(
        item.occurrence_id
        for item in service_check_line_trigger(
            instance,
            line.identity.name,
            LineEvaluateRequestV1(
                since=READ_TIME - timedelta(seconds=2), until=READ_TIME + timedelta(seconds=1)
            ),
            now=READ_TIME + timedelta(seconds=1),
        ).occurrences
        if item.dispatch_status == "pending"
    )

    restarted = READ_TIME + timedelta(seconds=120)
    for offset in (0, 60, 120):
        _match(instance, restarted + timedelta(seconds=offset), daemon_id="restarted")

    status = service_line_status(instance, line.identity.name)
    assert status.state == "armed"
    assert status.pending_automatic == 1 and status.pending_explicit == 0
    # The lapsed tick is retained and still runnable with an explicit retry.
    retried = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(occurrence_id=lapsed_id, retry=True),
        actor=_actor(instance),
        now=restarted + timedelta(seconds=121),
        caller_rung=3,
    )
    assert [item.status for item in retried.items] == ["admitted"]


def test_an_earlier_arms_cadence_tick_never_holds_back_a_new_arms_own(tmp_path):
    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME)
    service_disarm_line(instance, line.identity.name, actor=_actor(instance), now=READ_TIME)
    # The disarmed arm's tick lapses as the new arm opens, and the new arm ticks on.
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME + timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME + timedelta(seconds=62))

    status = service_line_status(instance, line.identity.name)
    assert (status.pending_automatic, status.pending_explicit) == (1, 0)


def test_a_restart_after_a_real_cadence_admission_keeps_ticking(tmp_path):
    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME)
    (arm,) = armed_work(instance, now=READ_TIME)
    admitted = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=READ_TIME
    )
    assert admitted is not None and admitted.items[0].status == "admitted"
    _match(instance, READ_TIME + timedelta(seconds=60))
    assert service_line_status(instance, line.identity.name).pending_automatic == 1

    for offset in (120, 180, 240):
        _match(instance, READ_TIME + timedelta(seconds=offset), daemon_id="restarted")

    # The chain's overdue tick lapsed; the resumed arm ticks from its own start.
    assert service_line_status(instance, line.identity.name).pending_automatic == 1


def test_explicit_evaluation_during_an_arm_never_starves_its_ticks(tmp_path):
    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    evaluated = service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(
            since=READ_TIME - timedelta(seconds=1), until=READ_TIME + timedelta(seconds=1)
        ),
        actor=_actor(instance),
        now=READ_TIME,
    )
    assert any(item.pending for item in evaluated.occurrences)
    for offset in (60, 120, 180):
        _match(instance, READ_TIME + timedelta(seconds=offset))

    status = service_line_status(instance, line.identity.name)
    assert (status.pending_automatic, status.pending_explicit) == (1, 1)


def test_a_lapsed_tick_is_retried_as_itself_after_a_newer_tick_ran(tmp_path):
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME)
    with LineDispatchStore(instance).locked() as conn:
        (lapsing,) = conn.execute("SELECT occurrence_id FROM pending").fetchone()
    later = READ_TIME + timedelta(seconds=180)
    _match(instance, READ_TIME + timedelta(seconds=120), daemon_id="restarted")
    _match(instance, later, daemon_id="restarted")
    (arm,) = armed_work(instance, now=later)
    newer = dispatch_armed_line(_manager(instance), instance.descriptor.instance_id, arm, now=later)
    assert newer is not None and newer.items[0].status == "admitted"

    retried = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(occurrence_id=lapsing, retry=True),
        actor=_actor(instance),
        now=later + timedelta(seconds=61),
        caller_rung=3,
    )
    assert [item.status for item in retried.items] == ["admitted"]


def test_a_rollover_waits_for_an_admission_already_inside_the_arm_boundary(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import cruxible_core.service.procedures.line_dispatch as dispatch_service

    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    inside, proceed = Event(), Event()
    original_gate = dispatch_service._segment_gate

    @contextmanager
    def paused_gate(*args):  # type: ignore[no-untyped-def]
        with original_gate(*args):
            # The segment check passed; the admission is about to be recorded.
            inside.set()
            assert proceed.wait(15)
            yield

    monkeypatch.setattr(dispatch_service, "_segment_gate", paused_gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitting = pool.submit(
            dispatch_armed_line,
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=3),
        )
        assert inside.wait(15)
        rolling = pool.submit(_match, instance, start + timedelta(seconds=4), "restarted")
        try:
            # The restart cannot end the segment under an admission in flight.
            with pytest.raises(TimeoutError):
                rolling.result(timeout=0.5)
        finally:
            proceed.set()
        admitting.result()
        rolling.result()

    # Admitted under the segment that matched it, then rolled over: nothing is
    # left behind as explicit work, and nothing ran after the rollover.
    assert _admissions(instance) == 1
    status = service_line_status(instance, line.identity.name)
    assert status.state == "armed" and status.pending_explicit == 0


def test_an_interrupted_rollover_leaves_the_arm_whole_and_the_next_pass_completes_it(
    tmp_path, monkeypatch
):
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    instance, line, procedure, start = _armed_world(tmp_path)
    original_append = LineDispatchStore.append

    def interrupted(self, conn, kind, data, **kwargs):  # type: ignore[no-untyped-def]
        if kind == "rollover":
            raise OSError("interrupted")
        return original_append(self, conn, kind, data, **kwargs)

    monkeypatch.setattr(LineDispatchStore, "append", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        _match(instance, start + timedelta(seconds=1), daemon_id="restarted")
    monkeypatch.setattr(LineDispatchStore, "append", original_append)

    # Nothing landed: the old segment still stands, so the arm still matches.
    assert service_line_status(instance, line.identity.name).state == "armed"
    _match(instance, start + timedelta(seconds=2), daemon_id="restarted")
    capture(instance, procedure, at=start + timedelta(seconds=3))
    _match(instance, start + timedelta(seconds=4), daemon_id="restarted")
    assert len(armed_work(instance, now=start + timedelta(seconds=4))) == 1


def test_a_retried_lapsed_tick_never_blocks_the_arms_own_ticks(tmp_path):
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    instance, line, _procedure = _cadence_world(tmp_path)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=READ_TIME - timedelta(seconds=1),
        daemon_id="daemon",
    )
    _match(instance, READ_TIME)
    with LineDispatchStore(instance).locked() as conn:
        (lapsing,) = conn.execute("SELECT occurrence_id FROM pending").fetchone()
    _match(instance, READ_TIME + timedelta(seconds=120), daemon_id="restarted")
    _match(instance, READ_TIME + timedelta(seconds=180), daemon_id="restarted")
    assert service_line_status(instance, line.identity.name).pending_automatic == 1
    retried = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(occurrence_id=lapsing, retry=True),
        actor=_actor(instance),
        now=READ_TIME + timedelta(seconds=181),
        caller_rung=3,
    )
    assert [item.status for item in retried.items] == ["admitted"]

    # The retry moved the chain past the arm's queued tick: it closes as
    # superseded instead of refusing forever, and the arm matches the next one.
    (arm,) = armed_work(instance, now=READ_TIME + timedelta(seconds=182))
    stale = dispatch_armed_line(
        _manager(instance),
        instance.descriptor.instance_id,
        arm,
        now=READ_TIME + timedelta(seconds=182),
    )
    assert stale is not None and [item.status for item in stale.items] == ["superseded"]
    at = READ_TIME + timedelta(seconds=300)
    _match(instance, at, daemon_id="restarted")
    (arm,) = armed_work(instance, now=at)
    ticked = dispatch_armed_line(_manager(instance), instance.descriptor.instance_id, arm, now=at)
    assert ticked is not None and [item.status for item in ticked.items] == ["admitted"]


def test_status_disarm_and_unknown_lines_refuse_with_codes_that_name_the_line(tmp_path):
    from cruxible_core.service.procedures.procedure_runs import (
        LineNeverArmed,
        LineRunNotAccepted,
    )

    instance, line, _procedure = line_world(tmp_path, CaptureLandingScheduleV1(event=SELECTOR))
    name = line.identity.name

    with pytest.raises(LineNeverArmed) as never:
        service_line_status(instance, name)
    assert never.value.error_code == "playbill.line.never_armed"
    assert repr(name) in str(never.value)
    assert never.value.repair.operation == "playbill.line.arm"

    with pytest.raises(LineNeverArmed):
        service_disarm_line(instance, name, actor=_actor(instance), now=READ_TIME)

    typo = name[:-1]
    with pytest.raises(LineRunNotAccepted) as unknown:
        service_line_status(instance, typo)
    assert f"no live accepted Line named {typo!r}" in str(unknown.value)
    assert f"nearest: {name}" in str(unknown.value)
    assert "sha256:" not in str(unknown.value)


def test_arm_and_disarm_are_idempotent_and_a_changed_arm_rebinds(tmp_path):
    instance, line, _procedure = line_world(tmp_path, CaptureLandingScheduleV1(event=SELECTOR))
    name = line.identity.name
    start = READ_TIME + timedelta(seconds=10)

    def arm(principal, at):  # type: ignore[no-untyped-def]
        return service_arm_line(
            instance, name, principal=principal, actor=_actor(instance), now=at, daemon_id="daemon"
        )

    armed = arm(LOCAL, start)
    assert armed.outcome == "armed"
    again = arm(LOCAL, start + timedelta(seconds=5))
    assert again.outcome == "already_armed"
    assert again.model_copy(update={"outcome": None}) == service_line_status(instance, name)
    assert (again.arm_id, again.armed_at) == (armed.arm_id, armed.armed_at)

    # A different credential is a different setting: the arm rebinds from now.
    rebound = arm(CREDENTIAL, start + timedelta(seconds=6))
    assert rebound.outcome == "rearmed" and rebound.arm_id != armed.arm_id
    assert rebound.armed_by == CREDENTIAL

    disarmed = service_disarm_line(
        instance, name, actor=_actor(instance), now=start + timedelta(seconds=7)
    )
    assert (disarmed.outcome, disarmed.state, disarmed.stop_reason) == (
        "disarmed",
        "stopped",
        "disarmed",
    )
    repeat = service_disarm_line(
        instance, name, actor=_actor(instance), now=start + timedelta(seconds=8)
    )
    assert repeat.outcome == "already_disarmed"
    stopped = disarmed.model_copy(update={"outcome": None})
    assert repeat.model_copy(update={"outcome": None}) == stopped
    assert service_line_status(instance, name) == stopped


def test_a_line_with_two_triggers_runs_each_ones_occurrences_exactly_once(tmp_path):
    from tests.support.lines import line_trigger
    from tests.test_procedures.test_line_triggers import TRIGGER

    ticking = "trigger-test-tick"
    instance, line, procedure = line_world(
        tmp_path,
        None,
        triggers=(
            line_trigger(
                TRIGGER, line="trigger-test", schedule=CaptureLandingScheduleV1(event=SELECTOR)
            ),
            line_trigger(
                ticking, line="trigger-test", schedule=CadenceScheduleV1(interval_seconds=60)
            ),
        ),
    )
    start = READ_TIME + timedelta(seconds=10)
    armed = service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    assert [item.trigger for item in armed.triggers] == [
        f"Trigger:{ticking}",
        f"Trigger:{TRIGGER}",
    ]
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))
    result = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )
    # The first tick and the landing are two occurrences, one per Trigger.
    assert result is not None and [item.status for item in result.items] == ["admitted"] * 2
    assert _admissions(instance) == 2
    journal, _ = _journal(instance)
    from cruxible_core.service.procedures.procedure_runs import _stored_line_admission

    fired = sorted(
        admission.trigger_binding.trigger.name
        for stored in journal.select_records(_stream(instance), event_kind="admission_bound")
        if (admission := _stored_line_admission(instance, stored)) is not None
    )
    assert fired == [ticking, TRIGGER]

    # Each Trigger keeps its own chain: the landing trigger firing again does not
    # push the cadence back, and nothing already admitted runs twice.
    capture(instance, procedure, at=start + timedelta(seconds=30))
    _match(instance, start + timedelta(seconds=31))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=31))
    dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=32)
    )
    assert _admissions(instance) == 3
    _match(instance, start + timedelta(seconds=63))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=63))
    ticked = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=63)
    )
    assert ticked is not None and [item.status for item in ticked.items] == ["admitted"]
    assert _admissions(instance) == 4


@pytest.mark.parametrize("change", ["added", "rescheduled", "retired"])
def test_a_trigger_change_while_armed_stops_the_arm_before_anything_runs(tmp_path, change):
    from cruxible_core.service.procedures.procedure_runs import line_triggers
    from tests.support.lines import line_trigger, successor, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree
    from tests.test_procedures.test_line_triggers import TRIGGER

    instance, line, procedure, owner = line_world(
        tmp_path, CaptureLandingScheduleV1(event=SELECTOR), with_owner=True
    )
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    current = line_trigger(
        TRIGGER, line=line.identity.name, schedule=CaptureLandingScheduleV1(event=SELECTOR)
    )
    changed = {
        "added": line_trigger(
            "another", line=line.identity.name, schedule=CadenceScheduleV1(interval_seconds=60)
        ),
        "rescheduled": successor(current, schedule=CadenceScheduleV1(interval_seconds=60)),
        "retired": successor(current, state="retired"),
    }[change]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(trigger_members(changed))
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="retrigger"
    )

    # Work matched under the old Triggers never runs automatically once they change.
    assert (
        dispatch_armed_line(
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=3),
        )
        is None
    )
    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", "trigger_changed")
    assert status.pending_explicit == 1

    # Rearming binds the arm to the Triggers that aim at the Line now.
    rearmed = service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start + timedelta(seconds=4),
        daemon_id="daemon",
    )
    assert rearmed.outcome == "armed" and rearmed.state == "armed"
    assert [item.trigger for item in rearmed.triggers] == [
        item.trigger.identity.qualified
        for item in line_triggers(
            instance, _accepted_line(instance, line), coordinate=instance.accepted_coordinate()
        )
    ]


def _accepted_line(instance, line):  # type: ignore[no-untyped-def]
    from cruxible_core.service.procedures.procedure_runs import _accepted_line_by_reference

    return _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line.identity.name
    )


def test_a_trigger_accepted_just_before_admission_stops_the_arm_instead_of_running(
    tmp_path, monkeypatch
):
    import cruxible_core.service.procedures.line_dispatch as dispatch_service
    from tests.support.lines import line_trigger, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, line, procedure, owner = line_world(
        tmp_path, CaptureLandingScheduleV1(event=SELECTOR), with_owner=True
    )
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update(
        trigger_members(
            line_trigger(
                "another", line=line.identity.name, schedule=CadenceScheduleV1(interval_seconds=60)
            )
        )
    )
    original = dispatch_service.service_run_playbill_line

    def added_meanwhile(*args, **kwargs):  # type: ignore[no-untyped-def]
        # Dispatch has already compared the arm's Trigger set; admission must again.
        _accept_tree(
            instance, owner, tree, timestamp="2026-08-28T15:02:00.000000Z", proposal_name="added"
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatch_service, "service_run_playbill_line", added_meanwhile)
    dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )

    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", "trigger_changed")
    assert status.pending_explicit == 1


@pytest.mark.parametrize(
    ("change", "reason"), [("trigger", "trigger_changed"), ("line", "line_changed")]
)
def test_an_acceptance_during_executor_preflight_never_records_an_admission(
    tmp_path, monkeypatch, change, reason
):
    import cruxible_core.service.procedures.procedure_runs as runs
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        render_line_spec,
    )
    from tests.support.lines import line_trigger, trigger_members
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    instance, line, procedure, owner = line_world(
        tmp_path, CaptureLandingScheduleV1(event=SELECTOR), with_owner=True
    )
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    if change == "trigger":
        tree.update(
            trigger_members(
                line_trigger(
                    "another",
                    line=line.identity.name,
                    schedule=CadenceScheduleV1(interval_seconds=60),
                )
            )
        )
    else:
        tree[line_spec_path(line.identity.name)] = render_line_spec(
            line.model_copy(
                update={
                    "parameters": {"status": "closed"},
                    "lifecycle": ArtifactLifecycle(
                        predecessor_digest=line_spec_digest(line).tagged
                    ),
                }
            )
        )
    original = runs._CurrentProcedureAuthority.current_procedure_digest
    injected = False

    def accept_after_the_last_head_check(self, identity, *, coordinate):  # type: ignore[no-untyped-def]
        # Executor preflight runs after the run's final head check and before
        # the admission append; an acceptance landing here must still stop the arm.
        nonlocal injected
        if not injected:
            injected = True
            _accept_tree(
                instance,
                owner,
                tree,
                timestamp="2026-08-28T15:02:00.000000Z",
                proposal_name="during-preflight",
            )
        return original(self, identity, coordinate=coordinate)

    monkeypatch.setattr(
        runs._CurrentProcedureAuthority,
        "current_procedure_digest",
        accept_after_the_last_head_check,
    )
    dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )

    assert injected
    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", reason)
    assert status.pending_explicit == 1


def test_an_armed_cron_line_ticks_on_calendar_instants_forward_only(tmp_path):
    from cruxible_client.contracts.temporal import parse_datetime
    from cruxible_client.contracts.triggers import CronScheduleV1
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore

    instance, line, _procedure = line_world(tmp_path, CronScheduleV1(expression="*/5 * * * *"))

    def ticks():  # type: ignore[no-untyped-def]
        # The calendar instant each admitted occurrence was due at.
        with LineDispatchStore(instance).locked() as conn:
            rows = conn.execute(
                "SELECT eligible_at FROM pending WHERE disposition='admitted' ORDER BY eligible_at"
            ).fetchall()
        return [parse_datetime(row[0]) for row in rows]

    def match_and_dispatch(at, daemon_id="daemon"):  # type: ignore[no-untyped-def]
        _match(instance, at, daemon_id=daemon_id)
        for arm in armed_work(instance, now=at):
            dispatch_armed_line(_manager(instance), instance.descriptor.instance_id, arm, now=at)

    # Armed at 16:02: the 16:00 instant precedes the arm and never runs.
    armed_at = READ_TIME + timedelta(minutes=2)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=armed_at,
        daemon_id="daemon",
    )
    match_and_dispatch(armed_at + timedelta(seconds=30))
    assert ticks() == []
    match_and_dispatch(READ_TIME + timedelta(minutes=5, seconds=1))
    assert ticks() == [READ_TIME + timedelta(minutes=5)]
    # An hour of daemon downtime: the instants it missed, 17:00 included, are
    # skipped; the restarted daemon ticks from its own start, not catching up.
    match_and_dispatch(READ_TIME + timedelta(hours=1, seconds=30), daemon_id="restarted")
    assert ticks() == [READ_TIME + timedelta(minutes=5)]
    match_and_dispatch(READ_TIME + timedelta(hours=1, minutes=5, seconds=1), daemon_id="restarted")
    assert ticks() == [READ_TIME + timedelta(minutes=5), READ_TIME + timedelta(hours=1, minutes=5)]
    assert _admissions(instance) == 2


def test_an_unbound_arming_credential_stops_the_arm(tmp_path, monkeypatch):
    instance, _line, procedure, start = _armed_world(tmp_path, principal=CREDENTIAL)
    _credential_store(
        monkeypatch, _credential(instance_id=instance.descriptor.instance_id, principal_id=None)
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    result = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )

    assert result is None
    assert _admissions(instance) == 0


def _registry_instance(principal_id: str, status: str):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.types import PrincipalRecord

    record = PrincipalRecord(
        principal_id=principal_id, public_key="1" * 64, kind="ordinary", status=status
    )
    return SimpleNamespace(
        descriptor=SimpleNamespace(instance_id="instance"),
        accepted_history=lambda: [SimpleNamespace(principals=SimpleNamespace(principals=[record]))],
    )


def test_a_credential_arm_stops_and_revokes_once_its_principal_is_revoked(monkeypatch):
    revoked: list[tuple[str, str]] = []
    record = _credential(instance_id="instance", principal_id="line-operator")
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(
            get=lambda _id: record,
            revoke_credentials_of_principal=lambda *, instance_id, principal_id: revoked.append(
                (instance_id, principal_id)
            ),
        ),
    )

    with pytest.raises(LineArmAuthorityLost) as lost:
        arm_authority(
            _registry_instance("line-operator", "revoked"), CREDENTIAL, now=datetime.now(UTC)
        )

    assert lost.value.reason == "principal_inactive"
    assert revoked == [("instance", "line-operator")]


def test_a_claimed_local_arm_stops_once_its_principal_is_no_longer_active(monkeypatch):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    claimed = LineArmPrincipalV1(kind="principal_claim", label="line-operator")

    with pytest.raises(LineArmAuthorityLost) as lost:
        arm_authority(
            _registry_instance("line-operator", "revoked"), claimed, now=datetime.now(UTC)
        )

    assert lost.value.reason == "principal_inactive"


def test_automatic_dispatch_stops_when_the_arming_principal_is_not_registered(
    tmp_path, monkeypatch
):
    instance, line, procedure, start = _armed_world(tmp_path, principal=CREDENTIAL)
    revoked: list[str] = []
    record = _credential(instance_id=instance.descriptor.instance_id, principal_id="ghost")
    monkeypatch.setattr(
        line_arms,
        "get_runtime_credential_store",
        lambda: SimpleNamespace(
            get=lambda _id: record,
            revoke_credentials_of_principal=lambda *, instance_id, principal_id: revoked.append(
                principal_id
            ),
        ),
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    (arm,) = armed_work(instance, now=start + timedelta(seconds=2))

    result = dispatch_armed_line(
        _manager(instance), instance.descriptor.instance_id, arm, now=start + timedelta(seconds=3)
    )

    assert result is None
    assert _admissions(instance) == 0
    assert revoked == ["ghost"]
    assert service_line_status(instance, line.identity.name).state != "armed"


def test_revoking_a_registered_principal_named_operator_stops_its_claimed_arm(monkeypatch):
    """A claimed principal named ``operator`` is not the implicit local operator."""

    from cruxible_core.server.auth import ResolvedAuthContext

    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    monkeypatch.setattr(
        line_arms,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            credential_id=None,
            credential_label=None,
            credential_type="principal_claim",
            instance_scope=None,
            role=None,
            effective_permission_mode=None,
            principal_id="operator",
        ),
    )
    armed = line_arms.current_arm_principal()
    assert armed.kind == "principal_claim" and armed.label == "operator"

    with pytest.raises(LineArmAuthorityLost) as lost:
        arm_authority(_registry_instance("operator", "revoked"), armed, now=datetime.now(UTC))

    assert lost.value.reason == "principal_inactive"


def test_the_implicit_local_operator_never_reads_a_registered_principals_standing(
    monkeypatch,
):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    monkeypatch.setattr(line_arms, "get_current_auth_context", lambda: None)
    implicit = line_arms.current_arm_principal()
    assert implicit.kind == "local_operator"

    # A revoked registered principal that happens to be named "operator" is a
    # different identity: the implicit operator's authority is the OS user's.
    actor, _rung = arm_authority(
        _registry_instance("operator", "revoked"), implicit, now=datetime.now(UTC)
    )

    assert actor.actor_type == "human_user" and actor.actor_id == "operator"


class _LegacyArmPrincipal:
    """An arm principal exactly as code before arm-record provenance persisted it."""

    def __init__(self, label: str) -> None:
        self._record = {"kind": "local_operator", "credential_id": None, "label": label}

    def model_dump(self, mode: str = "python") -> dict[str, object]:
        return dict(self._record)


@pytest.mark.parametrize("label", ["line-operator", "operator"])
def test_an_old_format_arm_is_stopped_on_recovery_never_rolled_over_as_the_operator(
    tmp_path, monkeypatch, label
):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    instance, line, _procedure, start = _armed_world(
        tmp_path,
        principal=_LegacyArmPrincipal(label),  # type: ignore[arg-type]
    )

    # A daemon restart: the next matching pass must not carry the arm across.
    _match(instance, start + timedelta(seconds=2), daemon_id="restarted-daemon")

    status = service_line_status(instance, line.identity.name)
    assert status.state == "stopped"
    assert status.stop_reason == "arm_requires_rearm"
    assert status.detail is not None and "rearm" in status.detail
    from cruxible_core.consumers.lines import LINE_ARMS

    (stopped,) = [
        health
        for health in LINE_ARMS.health(instance, now=start + timedelta(seconds=3))
        if health.state == "stopped"
    ]
    assert stopped.detail["stop_reason"] == "arm_requires_rearm"
    assert stopped.repair is not None and stopped.repair.operation == "playbill.line.arm"


def test_an_old_format_claimed_arm_admits_nothing_even_with_its_principal_revoked(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: False)
    instance, line, procedure, start = _armed_world(
        tmp_path,
        principal=_LegacyArmPrincipal("line-operator"),  # type: ignore[arg-type]
    )
    capture(instance, procedure, at=start + timedelta(seconds=1))
    _match(instance, start + timedelta(seconds=2))
    arms = armed_work(instance, now=start + timedelta(seconds=2))

    results = [
        dispatch_armed_line(
            _manager(instance),
            instance.descriptor.instance_id,
            arm,
            now=start + timedelta(seconds=3),
        )
        for arm in arms
    ]

    assert all(result is None for result in results)
    assert _admissions(instance) == 0
    status = service_line_status(instance, line.identity.name)
    assert status.state == "stopped" and status.stop_reason == "arm_requires_rearm"


def _accept_generation(instance, owner, name, instant):
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
    from tests.test_indexes.test_resolution_contracts import _accept_tree

    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"project.work_item/{name}"),
        subject_kind="project.work_item",
        subject_id=name,
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree[subject_path(shell.subject_kind, shell.subject_id)] = render_subject(shell)
    _accept_tree(
        instance,
        owner,
        tree,
        timestamp=instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        proposal_name=name,
    )


def test_generation_line_coalesces_and_skips_accepts_before_listening_or_restart(tmp_path):
    from cruxible_client.contracts.triggers import GenerationAcceptedScheduleV1

    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedScheduleV1(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    _accept_generation(instance, owner, "before-listening", start - timedelta(seconds=1))
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )
    _match(instance, start)
    assert armed_work(instance, now=start) == ()
    for offset in (1, 2, 3):
        _accept_generation(instance, owner, f"burst-{offset}", start + timedelta(seconds=offset))
    _match(instance, start + timedelta(seconds=4))
    assert service_line_status(instance, line.identity.name).pending_automatic == 1
    _accept_generation(instance, owner, "offline", start + timedelta(seconds=5))
    _match(instance, start + timedelta(seconds=6), daemon_id="restarted")
    status = service_line_status(instance, line.identity.name)
    assert (status.pending_automatic, status.pending_explicit) == (0, 1)
    assert armed_work(instance, now=start + timedelta(seconds=6)) == ()
    _accept_generation(instance, owner, "after-restart", start + timedelta(seconds=7))
    _match(instance, start + timedelta(seconds=8), daemon_id="restarted")
    assert service_line_status(instance, line.identity.name).pending_automatic == 1


def test_generation_line_accepts_once_then_reaches_a_fixed_point(tmp_path, monkeypatch):
    from cruxible_client.contracts.triggers import GenerationAcceptedScheduleV1
    from cruxible_core.service.procedures import line_dispatch

    instance, line, _, owner = line_world(tmp_path, GenerationAcceptedScheduleV1(), with_owner=True)
    start = READ_TIME + timedelta(seconds=10)
    service_arm_line(
        instance,
        line.identity.name,
        principal=LOCAL,
        actor=_actor(instance),
        now=start,
        daemon_id="daemon",
    )

    # A post-listening accept starts the chain. Later, the Line's first run
    # accepts once; its second settles nothing and leaves the head unchanged.
    _accept_generation(instance, owner, "start-generation", start + timedelta(seconds=1))
    run = line_dispatch.service_run_playbill_line
    runs = []

    def settling_once(*args, **kwargs):
        result = run(*args, **kwargs)
        runs.append(result)
        if len(runs) == 1:
            _accept_generation(instance, owner, "line-settlement", start + timedelta(seconds=2))
        return result

    monkeypatch.setattr(line_dispatch, "service_run_playbill_line", settling_once)
    for offset in (2, 3, 4):
        _match(instance, start + timedelta(seconds=offset))
        service_dispatch_line(
            instance,
            line.identity.name,
            LineDispatchRequestV1(),
            actor=_actor(instance),
            caller_rung=3,
            now=start + timedelta(seconds=offset),
        )
    assert len(runs) == 2
    assert _admissions(instance) == 2
    assert service_line_status(instance, line.identity.name).pending_automatic == 0
