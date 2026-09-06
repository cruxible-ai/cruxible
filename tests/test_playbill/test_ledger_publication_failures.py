"""Publication barriers never turn scheduling or target changes into acknowledgement."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from cruxible_core.playbill import instance as instance_module
from cruxible_core.playbill.git import GitLedger
from tests.test_playbill.test_ledger_mirror import _bare_remote, _mirrored
from tests.test_playbill.test_ledger_publication_worker import (
    GatedPush,
    _dirty_approval_note,
    _install_gate,
)


def _join_publisher(instance):
    worker = instance._mirror_thread
    if worker is not None:
        worker.join(20)
        assert not worker.is_alive(), "publication worker did not terminate"


def test_failed_scheduling_has_no_barrier_watermark(tmp_path, monkeypatch):
    instance, _remote = _mirrored(tmp_path)
    _join_publisher(instance)
    previous = instance.ledger_mirror_state()

    def no_write(*args, **kwargs):
        raise OSError("injected scheduling persistence failure")

    def forbidden_push(*args, **kwargs):
        raise AssertionError("an unpersisted request cannot start publication")

    monkeypatch.setattr(instance_module, "write_mirror_state", no_write)
    monkeypatch.setattr(GitLedger, "push_mirror", forbidden_push)
    result = instance.publish_ledger_mirror(timeout=0)
    assert result.status == "behind"
    assert result.wait_sequence is None
    assert "scheduling failed" in result.detail
    assert instance._mirror_thread is None
    assert instance.ledger_mirror_state() == previous


def test_worker_storage_failure_stops_without_respawn_and_explicit_request_restarts(
    tmp_path, monkeypatch
):
    instance, _remote = _mirrored(tmp_path)
    _join_publisher(instance)
    _dirty_approval_note(instance)
    original_write = instance_module.write_mirror_state
    original_start = threading.Thread.start
    failed_write = threading.Event()
    threads = []
    attempts = []
    storage_broken = True

    def record_start(thread):
        if thread.name.startswith("ledger-publisher-"):
            threads.append(thread)
        return original_start(thread)

    def write(root, state):
        if storage_broken and threading.current_thread().name.startswith("ledger-publisher-"):
            attempts.append(state)
            failed_write.set()
            # Bound a broken implementation's test execution as well: after
            # repeated respawns it can finish, but the assertions still fail.
            if len(attempts) <= 10:
                raise OSError("injected worker status persistence failure")
        return original_write(root, state)

    monkeypatch.setattr(threading.Thread, "start", record_start)
    monkeypatch.setattr(instance_module, "write_mirror_state", write)
    try:
        queued = instance.request_ledger_mirror()
        assert queued.status == "pending"
        assert failed_write.wait(10)
        assert threads
        threads[0].join(10)
        assert not threads[0].is_alive()
        with instance._mirror_condition:
            assert instance._mirror_thread is None, "storage failure respawned a pending worker"
        assert len(threads) == 1
        assert len(attempts) <= 2  # publishing-state write and best-effort failure record
    finally:
        storage_broken = False
        restored = instance.publish_ledger_mirror(timeout=20)
        _join_publisher(instance)
    assert restored.status == "current"
    assert restored.wait_sequence is not None
    assert restored.published_sequence >= restored.wait_sequence
    assert len(threads) == 2


def test_remote_switch_interrupts_old_barrier_without_reusing_new_remote_watermark(
    tmp_path, monkeypatch
):
    instance, old_remote = _mirrored(tmp_path)
    _join_publisher(instance)
    new_remote = _bare_remote(
        tmp_path, object_format=instance.descriptor.git_object_format, name="new-target.git"
    )
    gate = GatedPush(count=2)
    _install_gate(monkeypatch, gate)
    original_write = instance_module.write_mirror_state
    replacement_requested = threading.Event()

    def observe_replacement(root, state):
        original_write(root, state)
        if state.url == str(new_remote) and state.requested_sequence:
            replacement_requested.set()

    monkeypatch.setattr(instance_module, "write_mirror_state", observe_replacement)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_wait = pool.submit(instance.publish_ledger_mirror, timeout=20)
        new_wait = None
        try:
            assert gate.entered[0].wait(10)
            old_sequence = instance.ledger_mirror_state().requested_sequence
            new_wait = pool.submit(instance.set_ledger_mirror, str(new_remote))
            assert replacement_requested.wait(10)
            old_result = old_wait.result(timeout=10)
            assert old_result.status == "behind"
            assert old_result.url == str(old_remote)
            assert old_result.wait_sequence == old_sequence
            assert old_result.published_sequence < old_result.wait_sequence
            assert "chang" in old_result.detail.lower() or "interrupt" in old_result.detail.lower()
            assert not gate.release[0].is_set()
        finally:
            gate.open_all()
            old_wait.result(timeout=20)
            if new_wait is not None:
                new_result = new_wait.result(timeout=20)
            _join_publisher(instance)
    assert new_result.status == "current"
    assert new_result.url == str(new_remote)
    assert new_result.wait_sequence is not None
    assert new_result.published_sequence >= new_result.wait_sequence
