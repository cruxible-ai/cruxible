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
from cruxible_client.contracts.procedures.line_specs import CaptureLandingTriggerPolicyV2
from cruxible_core.runtime import line_arms
from cruxible_core.runtime.line_arms import arm_authority, dispatch_armed_line
from cruxible_core.runtime.line_listener import LineListener
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.credentials import RuntimeCredentialRecord
from cruxible_core.service.procedures.line_dispatch import (
    LineArmAuthorityLost,
    armed_work,
    service_arm_line,
    service_disarm_line,
    service_dispatch_line,
    service_evaluate_line,
    service_line_arm_status,
    service_match_listening_lines,
)
from cruxible_core.service.procedures.procedure_runs import _journal, _stream
from tests.test_procedures.test_line_triggers import SELECTOR, capture, line_world
from tests.test_procedures.test_procedure_run_surface import READ_TIME, _actor

LOCAL = LineArmPrincipalV1(kind="local_operator", label="local-operator")
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
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
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
    status = service_line_arm_status(instance, line.identity.name)
    assert status.state == "armed" and status.pending_automatic == 0


def test_the_daemon_listener_runs_armed_work_on_its_own(tmp_path, monkeypatch):
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
    listener = LineListener(_manager(instance))
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

    status = service_line_arm_status(instance, line.identity.name)
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
    assert service_line_arm_status(instance, line.identity.name).pending_explicit == 1


def test_arming_never_drains_a_backlog_that_explicit_evaluation_left(tmp_path):
    instance, line, procedure = line_world(tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR))
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
    status = service_line_arm_status(instance, line.identity.name)
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
    assert service_line_arm_status(instance, line.identity.name).pending_explicit == 1


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
    status = service_line_arm_status(instance, line.identity.name)
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
    status = service_line_arm_status(instance, line.identity.name)
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
    listener = LineListener(SimpleNamespace(), workers=2)
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
        listener._schedule("instance", {"line_id": "slow"})
        assert slow_entered.wait(5)
        listener._schedule("instance", {"line_id": "slow"})  # still draining: not rescheduled
        listener._schedule("instance", {"line_id": "fast"})
        assert fast_done.wait(5), "a slow Line's drain held up another Line"
        assert calls == ["slow", "fast"]
    finally:
        released.set()
        listener.close()


def test_local_operator_arms_stop_once_the_daemon_requires_authentication(monkeypatch):
    monkeypatch.setattr(line_arms, "is_server_auth_enabled", lambda: True)
    with pytest.raises(LineArmAuthorityLost) as lost:
        arm_authority("instance", LOCAL, now=datetime.now(UTC))
    assert lost.value.reason == "authentication_changed"


def test_an_automatic_run_acts_as_the_arming_credential(monkeypatch):
    _credential_store(monkeypatch, _credential(instance_id="instance"))
    actor, caller_rung = arm_authority("instance", CREDENTIAL, now=datetime.now(UTC))
    assert (actor.actor_type, actor.actor_id) == ("service_account", "line-operator")
    assert caller_rung == PermissionMode.GOVERNED_WRITE.value - 1


def _stalled(instance, at):  # type: ignore[no-untyped-def]
    from cruxible_core.service.discovery.next import LINE_STALL_AFTER
    from cruxible_core.service.procedures.line_dispatch import stalled_line_arms

    return stalled_line_arms(instance, now=at, stall_after=LINE_STALL_AFTER)


def test_a_deliberate_disarm_is_not_a_stall_but_undrained_armed_work_is(tmp_path):
    from cruxible_core.service.discovery.next import LINE_STALL_AFTER

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
    assert service_line_arm_status(instance, line.identity.name).pending_explicit == 1


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
        tmp_path, CaptureLandingTriggerPolicyV2(event=SELECTOR), with_owner=True
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
    status = service_line_arm_status(instance, line.identity.name)
    assert (status.state, status.stop_reason) == ("stopped", "line_changed")
