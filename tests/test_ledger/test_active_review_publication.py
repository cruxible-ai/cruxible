"""Closed proposals never become a permanent mirror inventory."""

from __future__ import annotations

import threading

import pytest

from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.proposals.proposals import service_withdraw_playbill_proposal
from tests.core_support._support import initialize_local
from tests.test_ledger.test_activation import _sign
from tests.test_ledger.test_ledger_mirror import WITHDRAWN_AT, _bare_remote, _remote_refs
from tests.test_proposals.test_proposal_notes import _submit


def _settle(instance, owner, proposal, kind):
    digest = proposal.candidate.candidate_digest
    signed = _sign(owner, digest, proposal.candidate.candidate.parent_semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="approval-relay",
    )
    if kind == "activation":
        service_activate_playbill_proposal(
            instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
        )
    else:
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
            actor_id="owner",
            reason="withdraw before first publication",
            withdrawn_at=WITHDRAWN_AT,
        )


def _assert_closed_absent(instance, remote, proposal):
    key = proposal.admission.proposal_id.removeprefix("sha256:")
    refs = _remote_refs(remote)
    assert "refs/settled/" + key not in refs
    assert "refs/heads/proposals/" + key not in refs
    assert not any(ref.startswith("refs/settled/") for ref in instance._ledger.mirror_refs())


@pytest.mark.parametrize("kind", ["withdrawal", "activation"])
def test_late_mirror_binding_omits_closed_proposals(tmp_path, kind):
    instance, owner = initialize_local(tmp_path)
    proposal = _submit(instance)
    _settle(instance, owner, proposal, kind)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    _assert_closed_absent(instance, remote, proposal)


@pytest.mark.parametrize("kind", ["withdrawal", "activation"])
def test_publication_does_not_revisit_closed_records(tmp_path, kind, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    proposal = _submit(instance)
    _settle(instance, owner, proposal, kind)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    from cruxible_core.proposals.proposal_evidence import ProposalEvidenceStore

    def forbidden(*args, **kwargs):
        pytest.fail("publication reopened closed proposal evidence")

    monkeypatch.setattr(ProposalEvidenceStore, "_read_located", forbidden)
    monkeypatch.setattr(
        ProposalEvidenceStore, "read_candidate_review_summary_if_present", forbidden
    )
    assert instance.publish_ledger_mirror(timeout=20).status == "current"
    _assert_closed_absent(instance, remote, proposal)


def test_coalesced_submit_and_withdraw_never_publishes_closed_branch(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    # Delay only the worker, while local evidence writers can take their common
    # projection lock. Both actions land before the first worker observation.
    entered = threading.Event()
    release = threading.Event()
    original = instance._publish_ledger_mirror_once

    def gated():
        entered.set()
        assert release.wait(20)
        return original()

    monkeypatch.setattr(instance, "_publish_ledger_mirror_once", gated)
    try:
        proposal = _submit(instance)
        assert entered.wait(10)
        _settle(instance, owner, proposal, "withdrawal")
    finally:
        release.set()
    assert instance.publish_ledger_mirror(timeout=20).status == "current"
    _assert_closed_absent(instance, remote, proposal)


def test_settled_history_does_not_grow_steady_publication(tmp_path, monkeypatch):
    from cruxible_core.ledger import git as git_module
    from tests.test_ledger.test_proposal_retention import submit

    instance, _owner = initialize_local(tmp_path)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    active = submit(instance, "still open")
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    command_sizes = []
    original = git_module._command

    def bounded_command(args, **kwargs):
        if "notes" in args and "list" in args:
            # A note listing must select one object, never the lifetime inventory.
            assert args[-2] == "list"
        if "push" in args:
            command_sizes.append(sum(len(value.encode()) + 1 for value in args))
        return original(args, **kwargs)

    monkeypatch.setattr(git_module, "_command", bounded_command)
    steady_sizes = []
    for i in range(8):
        closed = submit(instance, f"closed {i}")
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=closed.admission.proposal_id,
            actor_id="owner",
            reason="finished",
            withdrawn_at=WITHDRAWN_AT,
        )
        assert instance.publish_ledger_mirror(timeout=20).status == "current"
        command_sizes.clear()
        assert instance.publish_ledger_mirror(timeout=20).status == "current"
        assert len(command_sizes) == 1
        steady_sizes.append(command_sizes[0])
        proposals = [ref for ref in _remote_refs(remote) if ref.startswith("refs/heads/proposals/")]
        assert proposals == ["refs/heads/proposals/" + active.admission.proposal_id[7:]]
        assert len(instance._ledger.proposal_refs()) <= 1
    assert len(set(steady_sizes)) == 1
