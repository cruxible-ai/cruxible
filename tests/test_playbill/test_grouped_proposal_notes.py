"""Shared Git commits carry every admission without changing governed identities."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill.proposal_evidence import ProposalEvidenceStore
from cruxible_core.playbill.proposal_note_projection import ProposalNoteIndex
from cruxible_core.playbill.proposal_notes import proposal_evaluation_note
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposal_notes import TIMESTAMP
from tests.test_playbill.test_proposals import _proposal_tree, _shell


def _submit(instance, name, *, timestamp=TIMESTAMP, tree=None):
    body = instance.store_document_body(b"shared tree\n")
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell(body.digest)) if tree is None else tree,
        timestamp=timestamp,
    )


def _approve(instance, owner, proposal):
    candidate = proposal.candidate
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=_sign(
            owner, candidate.candidate_digest, candidate.candidate.parent_semantic_root
        ).attestation,
        authenticated_submitter="approval-relay",
    )


def _expected(*proposals):
    return b"".join(
        proposal_evaluation_note(admission=p.admission, evaluation=p.evaluation)
        for p in sorted(proposals, key=lambda item: item.admission.proposal_id)
    )


def test_distinct_admissions_sharing_commit_keep_both_records_and_can_activate(tmp_path):
    instance, owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    second = _submit(instance, "second")
    oid = first.admission.candidate_commit_oid
    assert oid == second.admission.candidate_commit_oid
    assert first.admission.proposal_id != second.admission.proposal_id
    assert instance.read_proposal_note("evaluation", oid) == _expected(first, second)
    _approve(instance, owner, first)
    instance._reconcile_proposal_review_refs()
    index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    for alias in index.proposal_ids_by_oid:
        assert (
            instance.read_proposal_note("evaluation", alias)
            == index.note_bytes(alias)["evaluation"]
        )
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=first.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted"


def test_subsecond_timestamps_share_git_oid_but_keep_distinct_signed_candidate_approvals(tmp_path):
    instance, owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    second = _submit(instance, "second", timestamp="2026-08-11T12:30:00.000001Z")
    assert first.admission.candidate_commit_oid == second.admission.candidate_commit_oid
    assert first.candidate.candidate_digest != second.candidate.candidate_digest
    _approve(instance, owner, first)
    _approve(instance, owner, second)
    oid = first.admission.candidate_commit_oid
    approvals = json.loads(instance.read_proposal_note("approval", oid))
    assert [row["attestation"]["payload_digest"] for row in approvals] == sorted(
        [
            first.candidate.candidate_digest,
            second.candidate.candidate_digest,
        ]
    )
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )


def test_original_and_advisory_aliases_share_one_group(tmp_path):
    instance, _owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    # Feeding back the evaluated tree can make a later original commit equal
    # an earlier advisory commit, despite different original admission OIDs.
    evaluated = instance.proposal_tree(first.evaluation.evaluated_tree_oid)
    second = _submit(instance, "second", tree=evaluated)
    assert first.admission.candidate_commit_oid != second.admission.candidate_commit_oid
    index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    assert (
        index.review_oids[first.admission.proposal_id]
        == index.review_oids[second.admission.proposal_id]
    )
    for oid in index.proposal_ids_by_oid:
        if instance._ledger.object_exists(oid):
            assert (
                instance.read_proposal_note("evaluation", oid)
                == index.note_bytes(oid)["evaluation"]
            )
    instance._reconcile_proposal_review_refs()
    for oid in index.review_oids.values():
        assert instance.read_proposal_note("evaluation", oid) == _expected(first, second)


def test_crash_after_evidence_before_note_reconciles_valid_subset_then_activates(
    tmp_path, monkeypatch
):
    instance, owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    oid = first.admission.candidate_commit_oid
    old_note = instance.read_proposal_note("evaluation", oid)
    with monkeypatch.context() as patch:

        def crash(*_args, **_kwargs):
            raise OSError("crash before note publication")

        patch.setattr(ProposalNoteIndex, "publish", crash)
        with pytest.raises(OSError, match="crash before note"):
            _submit(instance, "second")
    assert instance.read_proposal_note("evaluation", oid) == old_note
    # Strict settlement refuses an existing incomplete note until a projection
    # repair runs. It does not silently heal disagreement on the approval door.
    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        )
    instance._reconcile_proposal_review_refs()
    index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    assert instance.read_proposal_note("evaluation", oid) == index.note_bytes(oid)["evaluation"]
    assert len(instance.read_proposal_note("evaluation", oid).splitlines()) == 4
    _approve(instance, owner, first)
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )


@pytest.mark.parametrize("stage", ["evaluation", "candidate"])
def test_unrelated_incomplete_admission_does_not_block_later_authoring(
    tmp_path, monkeypatch, stage
):
    instance, _owner = initialize_local(tmp_path)
    with monkeypatch.context() as patch:

        def crash(*_args, **_kwargs):
            raise OSError("interrupted evidence")

        patch.setattr(ProposalEvidenceStore, f"write_{stage}", crash)
        with pytest.raises(OSError, match="interrupted evidence"):
            _submit(instance, "interrupted")
    later = _submit(instance, "later", timestamp="2026-08-11T12:30:01.000000Z")
    assert later.candidate is not None
    instance._reconcile_proposal_review_refs()
    index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    assert set(index.admissions) == {later.admission.proposal_id}
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=later.admission.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )


@pytest.mark.parametrize("corrupt", [b"edited verdict\n", b"{}\n{}\n", b"[]\n"])
def test_grouping_does_not_heal_corrupt_existing_notes(tmp_path, corrupt):
    instance, _owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    oid = first.admission.candidate_commit_oid
    instance.write_proposal_note("evaluation", oid, corrupt)
    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        _submit(instance, "second")
    assert instance.read_proposal_note("evaluation", oid) == corrupt
    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        )


def test_subset_recognition_refuses_duplicates_reordering_and_empty_approval_notes():
    pair1, pair2 = b'{"a":1}\n{"b":1}\n', b'{"a":2}\n{"b":2}\n'
    expected = pair1 + pair2
    assert ProposalNoteIndex._valid_subset("evaluation", pair1, expected)
    assert not ProposalNoteIndex._valid_subset("evaluation", pair1 + pair1, expected)
    assert not ProposalNoteIndex._valid_subset("evaluation", pair2 + pair1, expected)
    approval = canonical_bytes([{"signature": "one"}, {"signature": "two"}]) + b"\n"
    assert ProposalNoteIndex._valid_subset(
        "approval", canonical_bytes([{"signature": "one"}]) + b"\n", approval
    )
    assert not ProposalNoteIndex._valid_subset("approval", b"[]\n", approval)
    assert not ProposalNoteIndex._valid_subset("approval", b"[NaN]\n", approval)


def test_concurrent_shared_commit_submissions_publish_one_complete_group(tmp_path):
    instance, _owner = initialize_local(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: _submit(instance, name), ("first", "second")))
    first, second = results
    assert first.admission.candidate_commit_oid == second.admission.candidate_commit_oid
    assert instance.read_proposal_note(
        "evaluation", first.admission.candidate_commit_oid
    ) == _expected(first, second)


def test_crash_after_second_shared_candidate_approval_repairs_nonempty_subset(
    tmp_path, monkeypatch
):
    instance, owner = initialize_local(tmp_path)
    first = _submit(instance, "first")
    second = _submit(instance, "second", timestamp="2026-08-11T12:30:00.000001Z")
    _approve(instance, owner, first)
    oid = first.admission.candidate_commit_oid
    old_note = instance.read_proposal_note("approval", oid)
    with monkeypatch.context() as patch:

        def crash(*_args, **_kwargs):
            raise OSError("crash after durable approval")

        patch.setattr(ProposalNoteIndex, "publish", crash)
        with pytest.raises(OSError, match="crash after durable approval"):
            _approve(instance, owner, second)
    assert instance.read_proposal_note("approval", oid) == old_note
    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        )
    instance._reconcile_proposal_review_refs()
    expected = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger).note_bytes(
        oid
    )["approval"]
    assert instance.read_proposal_note("approval", oid) == expected
    assert len(json.loads(expected)) == 2
    assert (
        service_activate_playbill_proposal(
            instance, proposal_id=first.admission.proposal_id, activated_by="owner"
        ).status
        == "accepted"
    )


@pytest.mark.parametrize("kind", ["evaluation", "approval"])
def test_strict_activation_checks_advisory_alias_as_well_as_original(tmp_path, kind):
    instance, owner = initialize_local(tmp_path)
    proposal = _submit(instance, "first")
    _approve(instance, owner, proposal)
    instance._reconcile_proposal_review_refs()
    accepted = instance.accepted_coordinate()
    original = proposal.admission.candidate_commit_oid
    index = ProposalNoteIndex.build(instance.proposal_evidence(), instance._ledger)
    advisory = index.review_oids[proposal.admission.proposal_id]
    assert original != advisory
    original_note = instance.read_proposal_note(kind, original)
    instance.write_proposal_note(kind, advisory, b"edited advisory review note\n")
    assert instance.read_proposal_note(kind, original) == original_note
    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
        )
    assert instance.accepted_coordinate() == accepted
