"""Detached previews initialize current private schema without durable repair."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.kits import KitAddRequest, KitBuildRequest
from cruxible_client.contracts.procedures.artifacts import render_procedure
from cruxible_core.indexes.history.history_index import AcceptedHistoryIndex, detached_history_reads
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from cruxible_core.service.kits import build_kit, service_add_kit
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from cruxible_core.storage.preview_fence import previewing
from tests.core_support._identity_support import accept_tree
from tests.core_support._support import initialize_local
from tests.test_procedures.test_procedure_run_surface import _slotless_procedure
from tests.test_proposals.test_proposal_index import _accept_document, _submit

STAMP = "2026-10-29T12:00:00.000000Z"


def _disk(root: Path) -> dict:
    """Include DB/WAL/SHM, checkpoints, immutable evidence and directory membership."""
    result = {}
    for path in (root, *root.rglob("*")):
        st = path.lstat()
        content = (
            os.readlink(path)
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else None
        )
        result[path.relative_to(root).as_posix()] = (
            st.st_mode,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            content,
        )
    return result


def _preview_disk(instance) -> dict:
    # The proposal repair owns these durable indexes/evidence. CAS opening in
    # unchanged code reapplies directory permissions (ctime), tracked separately
    # in the handoff; retain body bytes and every regular-file metadata check.
    snapshot = _disk(instance.root)
    cas_directory = "cas/sha256"
    if cas_directory in snapshot:
        mode, inode, size, mtime, _ctime, content = snapshot[cas_directory]
        snapshot[cas_directory] = mode, inode, size, mtime, content
    return snapshot


def _forbidden(*args, **kwargs):
    pytest.fail("detached preview attempted durable repair or a warm-path rebuild")


def test_first_mutable_kit_preview_is_private_and_matches_warmed_preview(tmp_path, monkeypatch):
    # This is the reviewer's real mutable Procedure probe, with success assertions
    # and a disk inventory instead of catching the baseline error as a pass.
    for name in ("publisher", "consumer"):
        (tmp_path / name).mkdir()
    publisher, _ = initialize_local(tmp_path / "publisher")
    consumer, _ = initialize_local(tmp_path / "consumer")
    accepted = _slotless_procedure("acme.cold")
    accept_tree(
        publisher, {accepted.path: render_procedure(accepted.procedure)}, name="cold-procedure"
    )
    bundle = build_kit(
        publisher.tree_at(publisher.accepted_coordinate().git_oid),
        KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)),
    )
    coordinate = consumer.accepted_coordinate()
    tree = dict(consumer.immutable_tree_at(coordinate.git_oid))
    index = consumer._accepted_history_index.proposals
    before = _preview_disk(consumer)
    with monkeypatch.context() as guard:
        guard.setattr(index, "_writer", _forbidden)
        guard.setattr(index, "_write_marker", _forbidden)
        guard.setattr(consumer._accepted_history_index, "_write_connection", _forbidden)
        cold = service_add_kit(
            consumer, KitAddRequest(bundle=bundle), actor_id="owner", timestamp=STAMP
        )
    assert cold.status == "would_propose"
    assert [(p.path, p.action) for p in cold.plan] == [(accepted.path, "add")]
    assert consumer.accepted_coordinate() == coordinate
    assert dict(consumer.immutable_tree_at(coordinate.git_oid)) == tree
    assert _preview_disk(consumer) == before
    # An ordinary read is only the AFTER-success control, never cold setup.
    service_list_playbill_proposals(consumer)
    before = _preview_disk(consumer)
    rebuilt = index.reconstructions
    with monkeypatch.context() as guard:
        guard.setattr(index, "_writer", _forbidden)
        guard.setattr(index, "_rebuild", _forbidden)
        for _ in range(3):
            warm = service_add_kit(
                consumer, KitAddRequest(bundle=bundle), actor_id="owner", timestamp=STAMP
            )
            assert warm.status == cold.status and warm.plan == cold.plan
    assert index.reconstructions == rebuilt
    assert consumer.accepted_coordinate() == coordinate
    assert _preview_disk(consumer) == before


@pytest.mark.parametrize("file_state", ["absent", "empty", "wrong_schema", "corrupt"])
def test_detached_schema_initialization_is_private_and_fail_closed(
    tmp_path, monkeypatch, file_state
):
    path = tmp_path / "working.sqlite"
    root = tmp_path / "evidence"
    root.mkdir()
    if file_state == "empty":
        with sqlite3.connect(path):
            pass
    elif file_state == "wrong_schema":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE proposals (wrong TEXT)")
    elif file_state == "corrupt":
        path.write_bytes(b"not a SQLite database")
    owner = AcceptedHistoryIndex(path)
    index = owner.proposals
    before = _disk(tmp_path)
    with monkeypatch.context() as guard, detached_history_reads(), previewing():
        guard.setattr(index, "_writer", _forbidden)
        guard.setattr(index, "_write_marker", _forbidden)
        evidence = ProposalEvidenceStore(root, index=index)
        if file_state in {"wrong_schema", "corrupt"}:
            with pytest.raises(ProposalIntegrityError):
                index.rows(evidence)
        else:
            assert index.rows(evidence) == ()
            assert index.rows(evidence) == ()
    assert _disk(tmp_path) == before
    owner.close()


@pytest.mark.parametrize("missing_schema", [False, True])
def test_private_reconstruction_carries_real_source_and_accepted_history(
    tmp_path, monkeypatch, missing_schema
):
    instance, owner_key = initialize_local(tmp_path)
    settled = _accept_document(instance, owner_key, "accepted", 0)
    pending = _submit(instance, "pending")
    evidence = instance.proposal_evidence()
    with instance.accepted_history_reader():
        pass
    fields = (
        "proposal_id",
        "candidate_digest",
        "accepted_sequence",
        "admission_digest",
        "evaluation_digest",
    )

    def selected(rows):
        return {row["proposal_id"]: tuple(row[f] for f in fields) for row in rows}

    expected = selected(evidence.index.rows(evidence))
    assert expected[settled.proposal.admission.proposal_id][2] == 1
    assert expected[pending.admission.proposal_id][2] is None
    path = evidence.index.path
    instance._accepted_history_index.close()
    if missing_schema:
        # Simulate the derived proposal component not yet initialized; actual
        # accepted-history rows and canonical source evidence remain intact.
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE proposals")
            connection.execute("DROP TABLE proposal_progress")
    marker_path = evidence.root / ".proposal-source.json"
    marker = json.loads(marker_path.read_bytes())
    marker["clean"] = False  # a dirty checkpoint requires private source replay
    marker_path.write_text(json.dumps(marker))
    owner = AcceptedHistoryIndex(path)
    index = owner.proposals
    before = _disk(instance.root)
    with monkeypatch.context() as guard, detached_history_reads(), previewing():
        guard.setattr(index, "_writer", _forbidden)
        guard.setattr(index, "_write_marker", _forbidden)
        reopened = ProposalEvidenceStore(
            evidence.root,
            index=index,
            transport=instance._ledger,
            settled=instance._settled_candidate,
        )
        for _ in range(2):
            assert selected(index.rows(reopened)) == expected
    assert _disk(instance.root) == before
    owner.close()
