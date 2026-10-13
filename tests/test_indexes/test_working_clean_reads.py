"""Warm acquisition, not just yielded readers, remains independent of writers."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._support import initialize_local
from tests.test_proposals.test_grouped_proposal_notes import _submit


@pytest.mark.parametrize("surface", ("proposal", "history", "list"))
def test_warm_read_acquires_while_unrelated_writer_and_acquisition_locks_are_held(
    tmp_path, surface
):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    service_list_playbill_proposals(instance)
    evidence = instance.proposal_evidence()
    owner = instance._accepted_history_index

    def read():
        if surface == "proposal":
            with evidence.index.read(evidence) as connection:
                return connection.execute("SELECT proposal_id FROM proposals").fetchone()[0]
        if surface == "history":
            with instance.accepted_history_reader() as history:
                return history.generation(0).git_oid
        return service_list_playbill_proposals(instance).entries[0].proposal_id

    foreign = sqlite3.connect(owner.path)
    descriptor = os.open(evidence.root / ".proposal-source.lock", os.O_RDWR)
    pool = ThreadPoolExecutor(max_workers=1)
    acquired = False
    try:
        foreign.execute("BEGIN IMMEDIATE")
        foreign.execute("UPDATE history_progress SET sequence=sequence")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with owner._lock:
            pending = pool.submit(read)
            try:
                result = pending.result(timeout=2)
                acquired = True
            except TimeoutError:
                pass
    finally:
        os.close(descriptor)
        foreign.rollback()
        foreign.close()
        pool.shutdown(wait=True)
    assert acquired, "a clean snapshot waited for unrelated writer/acquisition locks"
    assert result == (
        instance.accepted_coordinate().git_oid
        if surface == "history"
        else proposal.admission.proposal_id
    )


def test_warm_service_list_acquires_while_other_process_has_uncommitted_write(tmp_path):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    service_list_playbill_proposals(instance)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute('BEGIN IMMEDIATE'); "
            "c.execute('UPDATE history_progress SET sequence=sequence'); "
            "print('ready',flush=True); sys.stdin.readline(); c.rollback(); c.close()",
            str(instance._accepted_history_index.path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    pool = ThreadPoolExecutor(max_workers=1)
    acquired = False
    try:
        assert process.stdout.readline().strip() == "ready"
        pending = pool.submit(service_list_playbill_proposals, instance)
        try:
            result = pending.result(timeout=2)
            acquired = True
        except TimeoutError:
            pass
    finally:
        process.communicate("release\n", timeout=10)
        pool.shutdown(wait=True)
    assert acquired, "list acquisition waited for an unrelated cross-process transaction"
    assert result.entries[0].proposal_id == proposal.admission.proposal_id


def _finalizer_world(tmp_path):  # type: ignore[no-untyped-def]
    from cruxible_core.indexes.proposals.proposal_index import ProposalIndex

    instance, _ = initialize_local(tmp_path)
    _submit(instance, "one")
    service_list_playbill_proposals(instance)
    evidence = instance.proposal_evidence()
    marker = ProposalIndex._marker(evidence.root)
    return evidence, marker, sqlite3.connect(evidence.index.path, check_same_thread=False)


def _completes(action) -> bool:  # type: ignore[no-untyped-def]
    import threading

    done = threading.Event()

    def run() -> None:
        action()
        done.set()

    # A regression blocks this thread forever on its own flock; a daemon thread
    # keeps that from hanging the run past the assertion.
    threading.Thread(target=run, daemon=True).start()
    return done.wait(timeout=5)


# An unreachable index's finalizer runs on whatever thread triggers garbage
# collection, including one inside the source lock's critical section. The lock
# is an flock, so a fresh descriptor on the same lock file blocks forever.
def test_a_finalizer_on_the_thread_holding_the_source_lock_does_not_wait_on_itself(tmp_path):
    from cruxible_core.indexes.proposals.proposal_index import (
        ProposalIndex,
        close_working_database,
    )

    evidence, marker, stale = _finalizer_world(tmp_path)
    proof = {"root": str(evidence.root), "marker": marker}

    def collect_inside_the_critical_section() -> None:
        with evidence.index._source_lock(evidence):
            close_working_database(evidence.index.path, [stale], proof)

    assert _completes(collect_inside_the_critical_section)
    # It could not certify from inside the holder's section, so the old
    # checkpoint stands and the next reader reconstructs if the bytes moved.
    assert ProposalIndex._marker(evidence.root) == marker
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        stale.execute("SELECT 1")


def test_a_finalizer_the_moment_the_source_lock_is_acquired_does_not_wait_on_itself(
    tmp_path, monkeypatch
):
    from cruxible_core.indexes.proposals import proposal_index
    from cruxible_core.indexes.proposals.proposal_index import close_working_database

    evidence, marker, stale = _finalizer_world(tmp_path)
    proof = {"root": str(evidence.root), "marker": marker}
    flock = fcntl.flock
    fired = []

    def collect_right_after_acquisition(descriptor, operation):  # type: ignore[no-untyped-def]
        flock(descriptor, operation)
        if not fired and operation == fcntl.LOCK_EX:
            fired.append(True)
            close_working_database(evidence.index.path, [stale], proof)

    monkeypatch.setattr(proposal_index.fcntl, "flock", collect_right_after_acquisition)

    def acquire() -> None:
        with evidence.index._source_lock(evidence):
            pass

    assert _completes(acquire)
    assert fired


def test_a_finalizer_for_a_replaced_root_sharing_the_held_lock_file_does_not_wait(tmp_path):
    from cruxible_core.indexes.proposals.proposal_index import close_working_database

    evidence, marker, stale = _finalizer_world(tmp_path)
    root = evidence.root
    moved = root.with_name(root.name + "-moved")

    def collect_after_the_root_is_replaced() -> None:
        with evidence.index._source_lock(evidence):
            root.rename(moved)
            try:
                root.mkdir()
                os.link(moved / ".proposal-source.lock", root / ".proposal-source.lock")
                close_working_database(
                    evidence.index.path, [stale], {"root": str(root), "marker": marker}
                )
            finally:
                (root / ".proposal-source.lock").unlink(missing_ok=True)
                root.rmdir()
                moved.rename(root)

    assert _completes(collect_after_the_root_is_replaced)


def test_a_finalizer_that_opens_the_held_lock_after_the_root_is_restored_does_not_wait(
    tmp_path, monkeypatch
):
    from cruxible_core.indexes.proposals import proposal_index
    from cruxible_core.indexes.proposals.proposal_index import close_working_database

    evidence, marker, stale = _finalizer_world(tmp_path)
    root = evidence.root
    moved = root.with_name(root.name + "-moved")
    lock_name = ".proposal-source.lock"
    real_open = os.open
    restored = []

    # A replacement root carries a different lock file, so any check made on the
    # name before opening sees nothing held; another thread restores the original
    # root just before the open, which then lands on the held lock file.
    def restore_then_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not restored and os.fspath(path) == os.fspath(root / lock_name):
            restored.append(True)
            (root / lock_name).unlink()
            root.rmdir()
            moved.rename(root)
        return real_open(path, *args, **kwargs)

    def collect_while_the_root_moves() -> None:
        with evidence.index._source_lock(evidence):
            root.rename(moved)
            root.mkdir()
            (root / lock_name).touch()
            with monkeypatch.context() as patch:
                patch.setattr(proposal_index.os, "open", restore_then_open)
                close_working_database(
                    evidence.index.path, [stale], {"root": str(root), "marker": marker}
                )

    assert _completes(collect_while_the_root_moves)
    assert restored


@pytest.mark.parametrize("surface", ("proposal", "history"))
def test_a_lock_free_read_whose_file_moves_during_acquisition_takes_the_locked_path(
    tmp_path, monkeypatch, surface
):
    from cruxible_core.indexes.history import history_index
    from cruxible_core.indexes.proposals import proposal_index

    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    service_list_playbill_proposals(instance)
    evidence = instance.proposal_evidence()
    module = proposal_index if surface == "proposal" else history_index
    real = module.open_working_snapshot
    raced = []

    # An ordinary writer (a queued review-ref refresh, a settle) may move the
    # file between the lock-free reader's checks and its snapshot. The real
    # acquisition sees the stamp move; the reader must fall back, not fail.
    def writer_moves_the_file(path, *, expected_stamp, file_stamp):  # type: ignore[no-untyped-def]
        if raced:
            return real(path, expected_stamp=expected_stamp, file_stamp=file_stamp)
        raced.append(True)
        calls = iter((expected_stamp,))
        return real(
            path,
            expected_stamp=expected_stamp,
            file_stamp=lambda: next(calls, (*expected_stamp[:-1], -1)),
        )

    monkeypatch.setattr(module, "open_working_snapshot", writer_moves_the_file)
    if surface == "proposal":
        with evidence.index.read(evidence) as connection:
            read = connection.execute("SELECT proposal_id FROM proposals").fetchone()[0]
        assert read == proposal.admission.proposal_id
    else:
        with instance.accepted_history_reader() as history:
            assert history.generation(0).git_oid
    assert raced
