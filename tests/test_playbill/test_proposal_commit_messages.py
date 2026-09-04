"""The ledger's commit messages are the review summary a reviewer reads in Git."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
)
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.errors import PlaybillGitError
from cruxible_core.playbill.proposal_message import (
    SUBJECT_LIMIT,
    generation_commit_message,
    proposal_commit_message,
)
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import service_activate_playbill_proposal
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-08-11T12:30:00.000000Z"
DOCUMENT_PATH = "documents/playbill-design.json"


def _message(ledger: Path, revision: str) -> str:
    """Read one commit's message with Git, the way a reviewer would."""

    raw = subprocess.run(
        ["git", f"--git-dir={ledger}", "log", "-1", "--format=%B", revision],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    # `%B` adds its own record separator after the stored message.
    return raw[: -len("\n")] if raw.endswith("\n\n") else raw


def _law_member(
    path: str,
    *,
    kind: str = "document",
    closure_role: str = "authored",
) -> CandidateMemberLawEvidenceV2:
    return CandidateMemberLawEvidenceV2(
        path=path,
        artifact_kind=kind,
        disposition="create",
        predecessor_artifact_digest=None,
        candidate_artifact_digest="sha256:" + "11" * 32,
        law_identifier="playbill.document.v1",
        law_digest="sha256:" + "22" * 32,
        law_evidence_digest="sha256:" + "33" * 32,
        closure_role=closure_role,  # type: ignore[arg-type]
    )


def test_one_member_reads_as_its_own_summary_without_repeating_itself() -> None:
    message = proposal_commit_message((_law_member(DOCUMENT_PATH),))

    assert message == f"create document {DOCUMENT_PATH}\n"


def test_many_members_tally_by_kind_and_roll_out_one_line_each() -> None:
    members = (
        _law_member("claims/ab/CLM-b.json", kind="claim"),
        _law_member("claims/ab/CLM-a.json", kind="claim"),
        _law_member(DOCUMENT_PATH),
    )

    message = proposal_commit_message(members)

    assert message.splitlines() == [
        "Propose 3 members: 2 claim, 1 document",
        "",
        "create claim claims/ab/CLM-a.json",
        "create claim claims/ab/CLM-b.json",
        f"create document {DOCUMENT_PATH}",
    ]


def test_a_derived_member_is_qualified_and_an_authored_one_is_not() -> None:
    members = (
        _law_member("cards/ab/CRD-a.json", kind="card", closure_role="generated_successor"),
        _law_member(DOCUMENT_PATH),
    )

    body = proposal_commit_message(members).splitlines()

    assert "create card cards/ab/CRD-a.json [generated_successor]" in body
    assert f"create document {DOCUMENT_PATH}" in body


def test_a_v1_member_is_qualified_by_its_governance_operation() -> None:
    member = CandidateMemberEvidence(
        path="principals/owner.json",
        artifact_kind="principal-lifecycle",
        artifact_digest="sha256:" + "44" * 32,
        disposition="replacement",
        law_identifier="playbill.principal.v1",
        governance_operation="revoke",
    )

    assert proposal_commit_message((member,)) == (
        "replacement principal-lifecycle principals/owner.json [revoke]\n"
    )


def test_an_oversize_summary_is_truncated_in_the_subject_and_kept_whole_below() -> None:
    members = tuple(
        _law_member(f"claims/ab/CLM-{index:040d}.json", kind=f"kind-{index}") for index in range(9)
    )

    lines = proposal_commit_message(members).splitlines()

    assert len(lines[0]) <= SUBJECT_LIMIT
    assert lines[0].endswith("...")
    assert lines[1] == ""
    assert lines[2].startswith("Propose 9 members: ")
    assert len(lines[2]) > SUBJECT_LIMIT


def test_a_refused_proposal_keeps_the_bare_ledger_subject() -> None:
    assert proposal_commit_message(()) == "Record Playbill proposal\n"


def test_a_generation_names_its_sequence_and_carries_the_same_member_roll() -> None:
    members = (_law_member(DOCUMENT_PATH),)

    assert generation_commit_message(members, sequence=4).splitlines() == [
        "Accept Playbill generation 4",
        "",
        f"create document {DOCUMENT_PATH}",
    ]


def test_the_candidate_commit_on_the_proposal_ref_reads_as_a_review_summary(
    tmp_path: Path,
) -> None:
    from tests.test_playbill.test_proposals import _proposal_tree, _shell

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Design\n")
    service = instance.proposal_service()

    result = service.submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/document",
            proposed_base_oid=instance.inspect().head_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell(body.digest)),
        timestamp=TIMESTAMP,
    )

    assert result.candidate is not None
    message = _message(instance._ledger.path, "refs/proposals/owner/document")
    assert message == proposal_commit_message(result.candidate.members)
    assert message.splitlines()[0] == f"create document {DOCUMENT_PATH}"


def test_the_advisory_review_branch_carries_byte_identical_prose(tmp_path: Path) -> None:
    from tests.test_playbill.test_proposals import _proposal_tree, _shell

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Design\n")
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/document",
            proposed_base_oid=instance.inspect().head_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell(body.digest)),
        timestamp=TIMESTAMP,
    )
    instance._reconcile_proposal_review_refs()

    key = result.admission.proposal_id.removeprefix("sha256:")
    assert _message(instance._ledger.path, f"refs/heads/proposals/{key}") == _message(
        instance._ledger.path, "refs/proposals/owner/document"
    )


def test_the_settled_generation_names_its_sequence_and_repeats_the_member_roll(
    tmp_path: Path,
) -> None:
    from tests.test_playbill.test_proposals import _proposal_tree, _shell

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Design\n")
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/document",
            proposed_base_oid=instance.inspect().head_oid,
        ),
        candidate_tree=_proposal_tree(instance, _shell(body.digest)),
        timestamp=TIMESTAMP,
    )
    assert result.candidate is not None

    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )

    assert receipt.status == "accepted"
    assert _message(instance._ledger.path, "refs/heads/main").splitlines() == [
        "Accept Playbill generation 1",
        "",
        f"create document {DOCUMENT_PATH}",
    ]


def test_the_ledger_refuses_a_blank_commit_message(tmp_path: Path) -> None:
    from tests.test_playbill.test_proposals import _proposal_tree, _shell

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Design\n")
    tree = _proposal_tree(instance, _shell(body.digest))
    tree[DOCUMENT_PATH] = render_document(_shell(body.digest))

    with pytest.raises(PlaybillGitError, match="nonblank prose summary"):
        instance._ledger.create_proposal_commit(
            tree,
            base_oid=instance.inspect().head_oid,
            target_ref="refs/proposals/owner/document",
            actor_id="owner",
            timestamp=TIMESTAMP,
            expected_ref_oid=None,
            message="   \n",
        )
