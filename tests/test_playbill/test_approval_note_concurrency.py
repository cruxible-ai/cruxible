"""Two signers on one candidate leave the store and the note saying one thing.

The approval note is a re-render of the whole canonical signer list, not a copy
of any one file, so writing it is a read-modify-write over the evidence store.
Serializing only the Git call left the render outside the lock: A could render
`[A]`, B render `[A, B]` and write, and A then force-write `[A]` -- store two,
Git one. Activation compares the two and refuses
`playbill.proposal.note_disagrees_with_evidence`, so a benign second approval,
which is exactly what an independent-approval instance exists to produce, could
wedge a proposal with a tamper refusal whose named repairs do not clear it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from cruxible_core.playbill.proposal_notes import proposal_approval_note
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposal_notes import _submit


def test_the_render_and_the_note_write_both_happen_inside_the_candidate_lock(
    tmp_path: Path,
) -> None:
    """The load-bearing assertion, and the one a real-concurrency test cannot make.

    Two threads racing may simply not interleave in the damaging order -- both
    can render after both store writes and agree by luck. Holding the lock from
    the outside settles it: while it is held, the approval door must not be able
    to reach its render, because the render is a read of the store whose result
    the note then claims. With the lock covering only the Git call, this
    completes and the assertion fires.
    """

    instance, owner = initialize_local(tmp_path)
    result = _submit(instance)
    assert result.candidate is not None
    digest = result.candidate.candidate_digest
    submission = _sign(owner, digest, result.candidate.candidate.parent_semantic_root)
    done = threading.Event()

    def approve() -> None:
        service_submit_playbill_approval(
            instance,
            proposal_id=result.admission.proposal_id,
            attestation=submission.attestation,
            authenticated_submitter="approval-relay",
        )
        done.set()

    thread = threading.Thread(target=approve)
    with instance.approval_note_lock(digest):
        thread.start()
        assert not done.wait(timeout=3), (
            "the approval door rendered and wrote its note while another holder "
            "had the candidate lock, so the read-modify-write is not serialized"
        )
    assert done.wait(timeout=60), "the approval never completed after the lock was released"
    thread.join(timeout=60)

    evidence = instance.proposal_evidence()
    approvals = evidence.read_approvals(digest)
    assert instance.read_proposal_note(
        "approval", result.admission.candidate_commit_oid
    ) == proposal_approval_note(approvals)


def test_two_concurrent_signers_leave_the_store_and_the_note_agreeing(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    reviewer = client_material(tmp_path, instance, principal_id="reviewer")
    result = _submit(instance)
    assert result.candidate is not None
    digest = result.candidate.candidate_digest
    root = result.candidate.candidate.parent_semantic_root
    submissions = [_sign(owner, digest, root), _sign(reviewer, digest, root)]

    start = threading.Barrier(len(submissions))
    failures: list[BaseException] = []

    def approve(index: int) -> None:
        start.wait(timeout=30)
        try:
            service_submit_playbill_approval(
                instance,
                proposal_id=result.admission.proposal_id,
                attestation=submissions[index].attestation,
                authenticated_submitter="approval-relay",
            )
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(exc)

    threads = [threading.Thread(target=approve, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not any(thread.is_alive() for thread in threads), "an approval never returned"
    assert failures == [], failures

    evidence = instance.proposal_evidence()
    approvals = evidence.read_approvals(digest)
    assert {item.attestation.signer_id for item in approvals} == {"owner", "reviewer"}
    note = instance.read_proposal_note("approval", result.admission.candidate_commit_oid)
    assert note == proposal_approval_note(approvals)

    # The point of the fix: settlement still proceeds. Before it, the loser's
    # late write left activation refusing a tamper nobody committed.
    receipt = service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )
    assert receipt.status == "accepted"


def test_the_approval_lock_is_per_candidate(tmp_path: Path) -> None:
    """Two candidates' signers do not queue behind one another."""

    instance, _owner = initialize_local(tmp_path)
    first = "sha256:" + "1a" * 32
    second = "sha256:" + "2b" * 32
    entered = threading.Event()

    with instance.approval_note_lock(first):

        def take_the_other() -> None:
            with instance.approval_note_lock(second):
                entered.set()

        thread = threading.Thread(target=take_the_other)
        thread.start()
        assert entered.wait(timeout=10), "a second candidate's approval lock blocked"
        thread.join(timeout=10)
