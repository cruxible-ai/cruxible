"""Verified SQL proposal lookup replaces the independent review-note cache."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.indexes.proposals.proposal_note_projection import ProposalNoteIndex
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from cruxible_core.proposals.proposal_notes import admission_bytes
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from tests.core_support._support import initialize_local
from tests.test_proposals.test_grouped_proposal_notes import _submit


def _oracle(instance):
    # An unbound source store deliberately selects the independent cold verifier.
    expected = ProposalNoteIndex.build(
        ProposalEvidenceStore(instance.proposal_evidence().root), instance._ledger
    )
    actual = instance.proposal_note_index()
    for field in (
        "admissions",
        "evaluations",
        "candidates",
        "review_oids",
        "proposal_ids_by_oid",
        "proposal_ids_by_candidate",
    ):
        assert dict(getattr(actual, field)) == dict(getattr(expected, field))
    for oid in expected.proposal_ids_by_oid:
        assert actual.note_bytes(oid) == expected.note_bytes(oid)
    return expected


def test_warm_list_is_source_free_and_fixed_inspect_reads_only_selected(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "first")
    index = instance.proposal_evidence().index
    assert index is not None
    measurements = []
    for amount in (1, 5):
        for number in range(amount):
            _submit(
                instance,
                f"unrelated-{amount}-{number}",
                timestamp=f"2026-08-11T12:30:{amount + number:02d}.000000Z",
            )
        service_list_playbill_proposals(instance)
        reads = []
        original = ProposalEvidenceStore.read_record_bytes

        def counted(path):
            reads.append(path)
            return original(path)

        def forbidden(*args, **kwargs):
            raise AssertionError("warmed lookup must not enumerate evidence or rebuild")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "glob", forbidden)
            patch.setattr(index, "_rebuild", forbidden)
            patch.setattr(ProposalEvidenceStore, "read_record_bytes", staticmethod(counted))
            listed = service_list_playbill_proposals(instance)
            assert listed.entries
            assert reads == []
            service_inspect_playbill_proposal(instance, proposal_id=first.admission.proposal_id)
        measurements.append(len(reads))
        assert {p.parent.name for p in reads} == {"proposals", "evaluations"}
        assert first.admission.proposal_id.removeprefix("sha256:") in reads[0].name
    assert measurements == [2, 2]
    _oracle(instance)


@pytest.mark.parametrize("kind", ["proposals", "evaluations", "candidates"])
def test_selected_same_size_restored_mtime_corruption_is_refused(tmp_path, kind):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    path = next(getattr(evidence, kind).glob("*.json"))
    old = path.stat()
    raw = path.read_bytes()
    path.write_bytes(b"!" + raw[1:])
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(ProposalIntegrityError):
        service_inspect_playbill_proposal(instance, proposal_id=proposal.admission.proposal_id)
    with pytest.raises(ProposalIntegrityError):
        instance.proposal_note_index().note_bytes(proposal.admission.candidate_commit_oid)
    path.write_bytes(raw)
    _oracle(instance)


def test_duplicate_evaluation_foreign_names_and_conflicting_admissions(tmp_path):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    evaluation = next(evidence.evaluations.glob("*.json"))
    duplicate = evidence.evaluations / "foreign.json"
    duplicate.write_bytes(evaluation.read_bytes())
    with pytest.raises(ProposalIntegrityError, match="multiple evaluations"):
        evidence.read_evaluation(first.admission.proposal_id)
    duplicate.unlink()
    renamed = evidence.evaluations / "legacy.json"
    evaluation.rename(renamed)
    assert evidence.read_evaluation(first.admission.proposal_id) == first.evaluation
    admission = evidence.proposals / "foreign.json"
    admission.write_bytes(admission_bytes(first.admission))
    assert len(service_list_playbill_proposals(instance).entries) == 1
    _oracle(instance)
    admission.write_bytes(
        admission_bytes(first.admission.model_copy(update={"rationale": "conflict"}))
    )
    # Directory membership changed since the prior successful checkpoint only
    # for the duplicate addition; explicit recovery diagnoses an in-place edit.
    (evidence.root / ".proposal-source.json").unlink()
    with pytest.raises(ProposalIntegrityError, match="conflicting admissions"):
        evidence.read_admission(first.admission.proposal_id)


def test_missing_evaluation_is_not_a_refused_list_entry(tmp_path):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    next(evidence.evaluations.glob("*.json")).unlink()
    with pytest.raises(ProposalIntegrityError, match="incomplete"):
        service_list_playbill_proposals(instance)
    with pytest.raises(ProposalIntegrityError, match="exactly one"):
        evidence.read_evaluation(first.admission.proposal_id)


def test_source_write_crash_never_publishes_a_false_checkpoint(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    evidence = instance.proposal_evidence()
    index = evidence.index
    assert index is not None

    index.rows(evidence)

    def crash(*args):
        raise OSError("after evidence before index commit")

    with monkeypatch.context() as patch:
        patch.setattr(index, "_finish", crash)
        with pytest.raises(OSError, match="before index commit"):
            _submit(instance, "interrupted")
    assert index._marker(evidence.root)["clean"] is False
    # Source admission is the commit point even if derived publication crashed.
    result = service_list_playbill_proposals(instance)
    assert len(result.entries) == 1
    assert index._marker(evidence.root)["clean"] is True
    retried = _submit(instance, "interrupted")
    assert retried.admission.proposal_id in {
        item.proposal_id for item in service_list_playbill_proposals(instance).entries
    }
    assert (
        evidence.read_admission(result.entries[0].proposal_id).proposal_id
        == result.entries[0].proposal_id
    )
    _oracle(instance)


def test_missing_checkpoint_and_deleted_sql_row_reconstruct_before_miss(tmp_path):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    index = evidence.index
    assert index is not None
    with sqlite3.connect(index.path) as connection:
        connection.execute("DELETE FROM proposals")
    assert evidence.read_admission(first.admission.proposal_id) == first.admission
    (evidence.root / ".proposal-source.json").unlink()
    assert evidence.read_evaluation(first.admission.proposal_id) == first.evaluation
    _oracle(instance)


def test_git_context_change_replaces_alias_and_keeps_submitted_group(tmp_path):
    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    old = dict(instance.proposal_note_index().review_oids)
    instance._ledger._git(["config", "i18n.commitEncoding", "ISO-8859-1"])
    notes = instance.proposal_note_index()
    assert dict(notes.review_oids) != old
    assert (
        first.admission.proposal_id
        in notes.proposal_ids_by_oid[first.admission.candidate_commit_oid]
    )
    for oid in old.values():
        if oid != first.admission.candidate_commit_oid:
            assert oid not in notes.proposal_ids_by_oid
    _oracle(instance)


def test_reader_yield_holds_no_write_transaction_or_acquisition_lock(tmp_path):
    import threading

    instance, _ = initialize_local(tmp_path)
    _submit(instance, "one")
    evidence = instance.proposal_evidence()
    index = evidence.index
    assert index is not None
    completed = threading.Event()

    def writer():
        with index._lock:
            with sqlite3.connect(index.path, timeout=1) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE proposal_progress SET verified_sequence=verified_sequence"
                )
        completed.set()

    with index.read(evidence):
        thread = threading.Thread(target=writer)
        thread.start()
        assert completed.wait(3)
    thread.join()


def test_split_source_completion_refreshes_missing_evaluation_and_candidate(tmp_path):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    next(evidence.evaluations.glob("*.json")).unlink()
    next(evidence.candidates.glob("*.json")).unlink()
    with pytest.raises(ProposalIntegrityError, match="incomplete"):
        service_list_playbill_proposals(instance)
    evidence.write_evaluation(proposal.evaluation)
    assert evidence.read_evaluation(proposal.admission.proposal_id) == proposal.evaluation
    with pytest.raises(ProposalIntegrityError, match="incomplete"):
        service_list_playbill_proposals(instance)
    evidence.write_candidate(proposal.candidate)
    assert service_list_playbill_proposals(instance).entries[0].status == "open"
    _oracle(instance)


def test_graceful_process_exit_reuses_exact_checkpoint_on_restart(tmp_path):
    import subprocess
    import sys

    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    for number in range(3):
        _submit(instance, f"unrelated-{number}", timestamp=f"2026-08-11T12:31:0{number}.000000Z")
    evidence = instance.proposal_evidence()
    index_path = evidence.index.path
    instance._accepted_history_index.close()
    code = """
import sys
from pathlib import Path
from cruxible_core.indexes.history.history_index import AcceptedHistoryIndex
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
owner = AcceptedHistoryIndex(Path(sys.argv[1]))
evidence = ProposalEvidenceStore(Path(sys.argv[2]), index=owner.proposals)
def no_rebuild(*args, **kwargs):
    raise AssertionError('graceful restart must reuse the verified source inventory')
owner.proposals._rebuild = no_rebuild
assert evidence.read_admission(sys.argv[3]).proposal_id == sys.argv[3]
assert evidence.read_evaluation(sys.argv[3]).proposal_id == sys.argv[3]
# Normal process exit runs the single owner's finalizer.
"""
    for _ in range(2):
        subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(index_path),
                str(evidence.root),
                first.admission.proposal_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def test_close_never_blesses_foreign_database_or_source_corruption(tmp_path):
    from cruxible_core.indexes.history.history_index import AcceptedHistoryIndex

    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    index_path = evidence.index.path
    with sqlite3.connect(index_path) as connection:
        connection.execute("DELETE FROM proposals")
    instance._accepted_history_index.close()
    owner = AcceptedHistoryIndex(index_path)
    reopened = ProposalEvidenceStore(
        evidence.root, index=owner.proposals, transport=instance._ledger
    )
    assert reopened.read_admission(first.admission.proposal_id) == first.admission
    assert owner.proposals.reconstructions == 1
    evaluation = next(evidence.evaluations.glob("*.json"))
    raw = evaluation.read_bytes()
    evaluation.write_bytes(b"!" + raw[1:])
    owner.close()
    latest = AcceptedHistoryIndex(index_path)
    with pytest.raises(ProposalIntegrityError):
        ProposalEvidenceStore(evidence.root, index=latest.proposals).read_evaluation(
            first.admission.proposal_id
        )
    latest.close()


def test_verified_orphan_inventory_cannot_hide_later_duplicate_evaluation(tmp_path, monkeypatch):
    instance, _ = initialize_local(tmp_path)
    proposal = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    next(evidence.proposals.glob("*.json")).unlink()
    # A read may reconstruct an inventory with an unpublished evaluation tail.
    assert service_list_playbill_proposals(instance).entries == ()
    competing = proposal.evaluation.model_copy(
        update={"evaluated_at": "2026-08-11T12:31:00.000000Z"}
    )
    with pytest.raises(ProposalIntegrityError, match="multiple evaluations"):
        evidence.write_evaluation(competing)
    assert len(tuple(evidence.evaluations.glob("*.json"))) == 1
    evidence.write_evaluation(proposal.evaluation)
    evidence.write_admission(proposal.admission)
    assert (
        service_list_playbill_proposals(instance).entries[0].proposal_id
        == proposal.admission.proposal_id
    )
    assert evidence.index._marker(evidence.root)["orphan_evaluations"] is False

    def forbidden(*args, **kwargs):
        raise AssertionError("resolved orphan must permit bounded future writes")

    with monkeypatch.context() as patch:
        patch.setattr(evidence.index, "_rebuild", forbidden)
        evidence.write_evaluation(proposal.evaluation)


def test_another_process_publishes_selected_withdrawal_without_stale_absence(tmp_path):
    import subprocess
    import sys

    instance, _ = initialize_local(tmp_path)
    first = _submit(instance, "one")
    evidence = instance.proposal_evidence()
    assert evidence.read_withdrawal(first.admission.proposal_id) is None
    code = """
import sys
from pathlib import Path
from cruxible_core.indexes.history.history_index import AcceptedHistoryIndex
from cruxible_core.ledger.git import GitLedger
from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore
from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecordV1
owner = AcceptedHistoryIndex(Path(sys.argv[1]))
ledger = GitLedger(Path(sys.argv[3]), signing_key_path=Path('unused'),
                   allowed_signers_path=Path('unused'))
evidence = ProposalEvidenceStore(Path(sys.argv[2]), index=owner.proposals, transport=ledger)
evidence.write_withdrawal(ProposalWithdrawalRecordV1(
    proposal_id=sys.argv[4], actor_id='owner', reason='other writer',
    withdrawn_at='2026-08-11T12:40:00.000000Z'))
"""
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(evidence.index.path),
            str(evidence.root),
            str(instance._ledger.path),
            first.admission.proposal_id,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert evidence.read_withdrawal(first.admission.proposal_id).reason == "other writer"
    assert service_list_playbill_proposals(instance).entries[0].terminal_reason == "withdrawn"
    _oracle(instance)
