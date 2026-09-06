"""Ledger publication is asynchronous, exact-snapshot work outside governance."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cruxible_core.playbill.git import NOTE_REFS, GitLedger
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.ledger_mirror import MIRROR_STATE_FILE
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_ledger_mirror import _bare_remote, _mirrored
from tests.test_playbill.test_proposal_notes import _submit


def _dirty_approval_note(instance, content: bytes = b"publication test\n") -> None:
    # A note-only write moves publication state without moving accepted main.
    instance._ledger.write_proposal_note("approval", instance._ledger.read_main(), content)


def _ref(instance, name: str) -> str:
    return instance._ledger._git(["rev-parse", name]).decode().strip()


class GatedPush:
    """Control completion without timing-based sleep assertions."""

    def __init__(self, count: int = 1) -> None:
        self.entered = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.snapshots: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def push(self, _ledger, _url, *, snapshot, expected_remote=None, environment=None, **kwargs):
        with self._lock:
            index = len(self.snapshots)
            self.snapshots.append(dict(snapshot))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if index < len(self.entered):
                self.entered[index].set()
                assert self.release[index].wait(20), "test did not release mirror push"
            return None
        finally:
            with self._lock:
                self.active -= 1

    def open_all(self):
        for event in self.release:
            event.set()


def _install_gate(monkeypatch, gate):
    def push(ledger, url, **kwargs):
        return gate.push(ledger, url, **kwargs)

    monkeypatch.setattr(GitLedger, "push_mirror", push)


def test_slow_mirror_does_not_block_governed_submit(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    accepted = instance.accepted_coordinate()
    gate = GatedPush()
    _install_gate(monkeypatch, gate)
    submitted = threading.Event()
    result = []
    failures = []

    def submit():
        try:
            result.append(_submit(instance))
        except BaseException as exc:
            failures.append(exc)
        finally:
            submitted.set()

    worker = threading.Thread(target=submit)
    worker.start()
    try:
        assert gate.entered[0].wait(10), "submission did not enqueue publication"
        assert submitted.wait(10), "governed submit waited for the remote"
        assert not failures
        assert result[0].candidate is not None
        assert instance.accepted_coordinate() == accepted
        assert not gate.release[0].is_set()
        assert instance.ledger_mirror_state().status in {"pending", "publishing"}
    finally:
        gate.open_all()
        worker.join(20)
        instance.publish_ledger_mirror(timeout=20)
    assert not worker.is_alive()


def test_new_request_during_push_retains_note_lag_and_exact_published_snapshot(
    tmp_path, monkeypatch
):
    instance, _remote = _mirrored(tmp_path)
    accepted = instance.accepted_coordinate()
    gate = GatedPush(count=2)
    _install_gate(monkeypatch, gate)
    _dirty_approval_note(instance, b"first\n")
    try:
        first = instance.request_ledger_mirror()
        assert gate.entered[0].wait(10)
        old_snapshot = dict(gate.snapshots[0])
        _dirty_approval_note(instance, b"second\n")
        latest_note = _ref(instance, NOTE_REFS["approval"])
        assert old_snapshot[NOTE_REFS["approval"]] != latest_note
        second = instance.request_ledger_mirror()
        assert second.requested_sequence > first.requested_sequence
        assert instance.accepted_coordinate() == accepted
        assert instance.ledger_mirror_state().status != "current"
        gate.release[0].set()
        assert gate.entered[1].wait(10)
        state = instance.ledger_mirror_state()
        assert state.status in {"pending", "publishing"}
        assert state.published_sequence < state.requested_sequence
        assert dict(state.published_refs) == old_snapshot
        assert state.published_main_oid == old_snapshot["refs/heads/main"]
        assert gate.snapshots[1][NOTE_REFS["approval"]] == latest_note
    finally:
        gate.open_all()
        final = instance.publish_ledger_mirror(timeout=20)
    assert final.status == "current"
    assert final.published_sequence == final.requested_sequence
    assert final.published_refs[NOTE_REFS["approval"]] == latest_note
    assert gate.max_active == 1


def test_explicit_publication_timeout_reports_pending_and_does_not_cancel_push(
    tmp_path, monkeypatch
):
    instance, _remote = _mirrored(tmp_path)
    gate = GatedPush()
    _install_gate(monkeypatch, gate)
    _dirty_approval_note(instance)
    try:
        instance.request_ledger_mirror()
        assert gate.entered[0].wait(10)
        timed_out = instance.publish_ledger_mirror(timeout=0)
        assert timed_out.status in {"pending", "publishing"}
        assert timed_out.wait_sequence == timed_out.requested_sequence
        assert timed_out.published_sequence < timed_out.requested_sequence
        assert not gate.release[0].is_set()
    finally:
        gate.open_all()
        final = instance.publish_ledger_mirror(timeout=20)
    assert final.status == "current"


def test_failed_publication_reports_behind_without_rolling_back_local_authority(
    tmp_path, monkeypatch
):
    instance, _remote = _mirrored(tmp_path)
    accepted = instance.accepted_coordinate()
    calls = []

    def fail(_ledger, _url, **kwargs):
        calls.append(kwargs["snapshot"])
        return "injected remote failure"

    monkeypatch.setattr(GitLedger, "push_mirror", fail)
    result = _submit(instance)
    state = instance.publish_ledger_mirror(timeout=20)
    assert result.candidate is not None
    assert state.status == "behind"
    assert "injected remote failure" in state.detail
    assert state.published_sequence < state.requested_sequence
    assert instance.accepted_coordinate() == accepted
    assert instance.proposal_evidence().read_admission(result.admission.proposal_id) is not None
    thread = instance._mirror_thread
    if thread is not None:
        thread.join(20)
        assert not thread.is_alive(), "failed publication did not exhaust bounded retries"
    assert 1 <= len(calls) <= 6  # at most three attempts per submit/explicit request


def test_writable_reopen_repairs_missing_enqueue_for_approval_only_ref(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    accepted = instance.accepted_coordinate()
    _dirty_approval_note(instance, b"durable before enqueue crash\n")
    expected_note = _ref(instance, NOTE_REFS["approval"])
    gate = GatedPush()
    _install_gate(monkeypatch, gate)
    try:
        reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
        assert gate.entered[0].wait(10), "reopen did not reconcile durable mirror lag"
        assert reopened.accepted_coordinate() == accepted
        assert gate.snapshots[0][NOTE_REFS["approval"]] == expected_note
        assert reopened.ledger_mirror_state().status != "current"
    finally:
        gate.open_all()
        final = instance.publish_ledger_mirror(timeout=20)
    assert final.status == "current"


def test_writable_reopen_rebuilds_deleted_publication_state(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    (instance.root / MIRROR_STATE_FILE).unlink()
    gate = GatedPush()
    _install_gate(monkeypatch, gate)
    try:
        reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
        assert gate.entered[0].wait(10)
        assert reopened.ledger_mirror_state().status in {"pending", "publishing"}
    finally:
        gate.open_all()
        final = instance.publish_ledger_mirror(timeout=20)
    assert final.status == "current"


def test_two_handles_serialize_publication_for_one_instance_root(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    second = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    second.publish_ledger_mirror(timeout=20)
    gate = GatedPush()
    _install_gate(monkeypatch, gate)
    _dirty_approval_note(instance)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(handle.request_ledger_mirror) for handle in (instance, second)]
            for future in futures:
                assert future.result(timeout=10).status in {"pending", "publishing", "current"}
        assert gate.entered[0].wait(10)
        assert gate.max_active == 1
    finally:
        gate.open_all()
        first_state = instance.publish_ledger_mirror(timeout=20)
        second_state = second.publish_ledger_mirror(timeout=20)
    assert first_state.status == second_state.status == "current"
    assert first_state.published_refs == second_state.published_refs
    assert gate.max_active == 1


def test_older_main_snapshot_is_not_reported_as_the_newly_accepted_main(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    proposed = _submit(instance)
    attestation = _sign(
        instance._owner_material,
        proposed.candidate.candidate_digest,
        proposed.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=attestation.attestation,
        authenticated_submitter="approval-relay",
    )
    instance.publish_ledger_mirror(timeout=20)
    old_main = instance._ledger.read_main()
    gate = GatedPush(count=2)
    _install_gate(monkeypatch, gate)
    try:
        instance.request_ledger_mirror()
        assert gate.entered[0].wait(10)
        assert gate.snapshots[0]["refs/heads/main"] == old_main
        receipt = service_activate_playbill_proposal(
            instance,
            proposal_id=proposed.admission.proposal_id,
            activated_by="owner",
        )
        assert receipt.status == "accepted"
        new_main = instance._ledger.read_main()
        assert old_main != new_main
        gate.release[0].set()
        assert gate.entered[1].wait(10)
        state = instance.ledger_mirror_state()
        assert state.status != "current"
        assert state.published_main_oid == old_main
        assert state.published_refs["refs/heads/main"] == old_main
        assert gate.snapshots[1]["refs/heads/main"] == new_main
    finally:
        gate.open_all()
        state = instance.publish_ledger_mirror(timeout=20)
    assert state.status == "current"
    assert state.published_main_oid == new_main


@pytest.mark.parametrize("initially_configured", (False, True))
def test_stale_handle_cannot_restore_old_remote_configuration(tmp_path, initially_configured):
    if initially_configured:
        instance, old_remote = _mirrored(tmp_path)
    else:
        instance, _owner = initialize_local(tmp_path)
        old_remote = None
    stale = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    stale.publish_ledger_mirror(timeout=20)
    new_remote = _bare_remote(
        tmp_path,
        object_format=instance.descriptor.git_object_format,
        name="replacement.git",
    )
    assert instance.set_ledger_mirror(str(new_remote)).status == "current"
    assert stale.descriptor.mirror_url == (None if old_remote is None else str(old_remote))
    requested = stale.request_ledger_mirror()
    assert requested.url == str(new_remote)
    final = stale.publish_ledger_mirror(timeout=20)
    assert final.url == str(new_remote) and final.status == "current"
    assert instance.ledger_mirror_state().url == str(new_remote)


def test_request_arriving_after_worker_stop_decision_is_not_lost(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    previous_worker = instance._mirror_thread
    if previous_worker is not None:
        previous_worker.join(20)
        assert not previous_worker.is_alive()
    decided_to_stop = threading.Event()
    finalize = threading.Event()
    second_push = threading.Event()

    class StopBoundaryCondition(threading.Condition):
        intercepted = False

        def __exit__(self, *args):
            result = super().__exit__(*args)
            if (
                threading.current_thread().name.startswith("ledger-publisher-")
                and not self.intercepted
            ):
                self.intercepted = True
                decided_to_stop.set()
                assert finalize.wait(20), "test did not release worker finalization"
            return result

    instance._mirror_condition = StopBoundaryCondition()
    snapshots = []

    def push(_ledger, _url, *, snapshot, **kwargs):
        snapshots.append(dict(snapshot))
        if len(snapshots) >= 2:
            second_push.set()
        return None

    monkeypatch.setattr(GitLedger, "push_mirror", push)
    _dirty_approval_note(instance, b"before stop\n")
    try:
        instance.request_ledger_mirror()
        assert decided_to_stop.wait(10)
        # The old thread remains registered, so this request cannot start a
        # replacement itself. Finalization must notice its durable watermark.
        assert instance._mirror_thread is not None
        _dirty_approval_note(instance, b"after stop decision\n")
        requested = instance.request_ledger_mirror()
        assert requested.status == "pending"
        finalize.set()
        assert second_push.wait(10), "new request was stranded at worker exit"
    finally:
        finalize.set()
        state = instance.publish_ledger_mirror(timeout=20)
    assert state.published_sequence >= requested.requested_sequence
    assert state.status == "current"
