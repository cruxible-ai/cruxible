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
