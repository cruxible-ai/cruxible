"""Review projection writers share approval locks and current accepted authority."""

from __future__ import annotations

from contextlib import contextmanager

from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposal_notes import proposal_approval_note
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposal_notes import _submit


def _approve(instance, owner, proposed):
    signed = _sign(
        owner,
        proposed.candidate.candidate_digest,
        proposed.candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="approval-relay",
    )


def test_review_note_reads_and_render_stay_inside_candidate_lock(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    proposed = _submit(instance)
    _approve(instance, owner, proposed)
    evidence = instance.proposal_evidence()
    digest = proposed.candidate.candidate_digest
    review_oid = proposed.admission.candidate_commit_oid
    expected = proposal_approval_note(evidence.read_approvals(digest))
    original_lock = instance.approval_note_lock
    original_read_approvals = evidence.read_approvals
    original_read_note = instance._ledger.read_proposal_note
    original_write_note = instance._ledger.write_proposal_note
    held = False
    reads = []
    writes = []

    @contextmanager
    def observed_lock(candidate_digest):
        nonlocal held
        assert candidate_digest == digest
        with original_lock(candidate_digest):
            held = True
            try:
                yield
            finally:
                held = False

    def read_approvals(candidate_digest):
        assert held, "review publication read approval evidence outside the candidate lock"
        reads.append(candidate_digest)
        return original_read_approvals(candidate_digest)

    def read_note(kind, oid):
        if kind == "approval":
            assert held
            # Force the derived note to require replacement so both sides of
            # the read-modify-write boundary are exercised deterministically.
            return None
        return original_read_note(kind, oid)

    def write_note(kind, oid, content):
        if kind == "approval":
            assert held, "review publication wrote its rendered note outside the lock"
            writes.append(content)
        return original_write_note(kind, oid, content)

    monkeypatch.setattr(instance, "approval_note_lock", observed_lock)
    monkeypatch.setattr(evidence, "read_approvals", read_approvals)
    monkeypatch.setattr(instance._ledger, "read_proposal_note", read_note)
    monkeypatch.setattr(instance._ledger, "write_proposal_note", write_note)
    instance._publish_review_commit_notes(
        evidence,
        review_oid=review_oid,
        proposal_id=proposed.admission.proposal_id,
        candidate_digest=digest,
    )
    assert len(reads) >= 2  # presence check and the complete signer-list render
    assert writes == [expected]
    assert original_read_note("approval", review_oid) == expected


def test_stale_handle_reconciliation_cannot_resurrect_a_settled_proposal(tmp_path):
    instance, owner = initialize_local(tmp_path)
    proposed = _submit(instance)
    stale = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    old_coordinate = stale.accepted_coordinate()
    instance._reconcile_proposal_review_refs()
    digest = proposed.admission.proposal_id.removeprefix("sha256:")
    review_ref = f"refs/heads/proposals/{digest}"
    assert review_ref in instance._ledger.mirror_refs()
    _approve(instance, owner, proposed)
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"
    instance._reconcile_proposal_review_refs()
    assert review_ref not in instance._ledger.mirror_refs()
    settled = instance._ledger.settled_proposal_refs()
    assert any(ref.endswith(digest) for ref in settled)
    assert stale.accepted_coordinate() == old_coordinate
    assert old_coordinate != instance.accepted_coordinate()
    stale._reconcile_proposal_review_refs()
    assert stale.accepted_coordinate() == instance.accepted_coordinate()
    assert review_ref not in stale._ledger.mirror_refs()
    assert stale._ledger.settled_proposal_refs() == settled
