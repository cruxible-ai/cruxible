"""Proposal evidence projected onto its own candidate commit as Git notes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.playbill.git import NOTE_REFS
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposal_notes import (
    admission_bytes,
    evaluation_bytes,
    proposal_approval_note,
    proposal_evaluation_note,
)
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposals import _proposal_tree, _shell

TIMESTAMP = "2026-08-11T12:30:00.000000Z"
TARGET_REF = "refs/proposals/owner/document"


def _git_notes_show(ledger: Path, ref: str, oid: str) -> bytes:
    """Read one note exactly as `git notes --ref=... show` hands it to a reviewer."""

    return subprocess.run(
        ["git", f"--git-dir={ledger}", "notes", f"--ref={ref}", "show", oid],
        capture_output=True,
        check=True,
    ).stdout


def _submit(instance: PlaybillInstance, *, body: bytes = b"# Design\n") -> object:
    stored = instance.store_document_body(body)
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=TARGET_REF,
            proposed_base_oid=instance.inspect().head_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell(stored.digest)),
        timestamp=TIMESTAMP,
    )


def test_the_evaluation_note_is_the_evidence_store_byte_for_byte(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _submit(instance)
    evidence = instance.proposal_evidence()

    note = _git_notes_show(
        instance._ledger.path,
        NOTE_REFS["evaluation"],
        result.admission.candidate_commit_oid,
    )

    stored = result.admission.proposal_id.removeprefix("sha256:")
    admission_file = evidence.proposals / f"{stored}.json"
    assert note.startswith(admission_file.read_bytes())
    assert note == admission_bytes(result.admission) + evaluation_bytes(result.evaluation)
    assert note == evidence.evaluation_note(result.admission.proposal_id)


def test_a_refused_proposal_projects_its_diagnostics_into_the_note(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    service = instance.proposal_service()

    result = service.submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=TARGET_REF,
            proposed_base_oid=instance.inspect().head_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell("sha256:" + "ff" * 32)),
        timestamp=TIMESTAMP,
    )

    assert result.evaluation.verdict == "refused"
    note = instance.read_proposal_note("evaluation", result.admission.candidate_commit_oid)
    assert note is not None
    assert result.evaluation.diagnostics[0].code.encode("utf-8") in note


def test_re_evaluating_one_ref_gives_each_submission_its_own_current_note(
    tmp_path: Path,
) -> None:
    """A resubmission extends the ref, so both commits keep the note they earned."""

    instance, _owner = initialize_local(tmp_path)
    first = _submit(instance, body=b"# First\n")
    second = _submit(instance, body=b"# Second\n")

    assert second.admission.candidate_commit_oid != first.admission.candidate_commit_oid
    assert instance.read_proposal_note(
        "evaluation", first.admission.candidate_commit_oid
    ) == proposal_evaluation_note(admission=first.admission, evaluation=first.evaluation)
    assert instance.read_proposal_note(
        "evaluation", second.admission.candidate_commit_oid
    ) == proposal_evaluation_note(admission=second.admission, evaluation=second.evaluation)


def test_one_commit_restates_rather_than_accumulates_its_note(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _submit(instance)
    oid = result.admission.candidate_commit_oid
    replacement = b"restated evaluation projection\n"

    instance.write_proposal_note("evaluation", oid, replacement)

    assert instance.read_proposal_note("evaluation", oid) == replacement


def test_activation_refuses_a_tampered_evaluation_note(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _submit(instance)
    instance.write_proposal_note(
        "evaluation",
        result.admission.candidate_commit_oid,
        b"a verdict the daemon never wrote\n",
    )

    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance,
            proposal_id=result.admission.proposal_id,
            activated_by="owner",
        )


def test_activation_repairs_a_missing_note_instead_of_stranding_the_proposal(
    tmp_path: Path,
) -> None:
    """A proposal admitted before the ref existed has no note; that is not tampering."""

    instance, _owner = initialize_local(tmp_path)
    result = _submit(instance)
    ledger = instance._ledger.path
    subprocess.run(
        ["git", f"--git-dir={ledger}", "update-ref", "-d", NOTE_REFS["evaluation"]],
        check=True,
        capture_output=True,
    )
    assert instance.read_proposal_note("evaluation", result.admission.candidate_commit_oid) is None

    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    assert instance.read_proposal_note(
        "evaluation", result.admission.candidate_commit_oid
    ) == proposal_evaluation_note(admission=result.admission, evaluation=result.evaluation)


def test_an_unapproved_candidate_is_left_without_an_approval_note(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _submit(instance)

    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    assert instance.read_proposal_note("approval", result.admission.candidate_commit_oid) is None


def test_the_approval_note_lists_every_signer_with_its_own_signature(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    result = _submit(instance)
    assert result.candidate is not None
    submission = _sign(
        owner,
        result.candidate.candidate_digest,
        result.candidate.candidate.parent_semantic_root,
    )

    service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=submission.attestation,
        authenticated_submitter="approval-relay",
    )

    note = _git_notes_show(
        instance._ledger.path,
        NOTE_REFS["approval"],
        result.admission.candidate_commit_oid,
    )
    approvals = instance.proposal_evidence().read_approvals(result.candidate.candidate_digest)
    assert note == proposal_approval_note(approvals)
    assert submission.attestation.sig.encode("ascii") in note
    assert b'"signer_id":"owner"' in note


def test_activation_refuses_an_approval_note_that_disagrees_with_the_store(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    result = _submit(instance)
    assert result.candidate is not None
    service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=_sign(
            owner,
            result.candidate.candidate_digest,
            result.candidate.candidate.parent_semantic_root,
        ).attestation,
        authenticated_submitter="approval-relay",
    )
    instance.write_proposal_note(
        "approval",
        result.admission.candidate_commit_oid,
        proposal_approval_note(()),
    )

    with pytest.raises(ProposalIntegrityError, match="note_disagrees_with_evidence"):
        service_activate_playbill_proposal(
            instance,
            proposal_id=result.admission.proposal_id,
            activated_by="owner",
        )


def test_the_note_kind_table_is_the_only_vocabulary(tmp_path: Path) -> None:
    from cruxible_client.contracts.errors import PlaybillGitError

    instance, _owner = initialize_local(tmp_path)
    head = instance.inspect().head_oid

    assert set(NOTE_REFS) == {"generation", "evaluation", "approval"}
    with pytest.raises(PlaybillGitError, match="unknown Playbill proposal note kind"):
        instance.read_proposal_note("generation", head)
    with pytest.raises(PlaybillGitError, match="unknown Playbill proposal note kind"):
        instance.write_proposal_note("verdict", head, b"x\n")
