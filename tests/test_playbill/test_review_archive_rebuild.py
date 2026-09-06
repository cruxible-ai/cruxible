"""Settled review refs and notes reconstruct without a prior open advertisement."""

from __future__ import annotations

import threading

import pytest

from cruxible_core.playbill.git import NOTE_REFS
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_proposals import service_withdraw_playbill_proposal
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_ledger_mirror import WITHDRAWN_AT, _bare_remote, _remote_refs
from tests.test_playbill.test_proposal_notes import _submit


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


def _assert_archive(instance, remote, proposal):
    key = proposal.admission.proposal_id.removeprefix("sha256:")
    archived = "refs/settled/" + key
    assert archived in _remote_refs(remote)
    assert "refs/heads/proposals/" + key not in _remote_refs(remote)
    oid = instance._ledger._git(["rev-parse", archived]).decode().strip()
    evidence = instance.proposal_evidence()
    assert instance.read_proposal_note("evaluation", oid) == evidence.evaluation_note(
        proposal.admission.proposal_id
    )
    assert instance.read_proposal_note("approval", oid) == evidence.approval_note(
        proposal.candidate.candidate_digest
    )
    return archived, oid


@pytest.mark.parametrize("kind", ["withdrawal", "activation"])
def test_late_mirror_binding_rebuilds_never_open_settlement_and_notes(tmp_path, kind):
    instance, owner = initialize_local(tmp_path)
    proposal = _submit(instance)
    _settle(instance, owner, proposal, kind)
    assert instance._ledger.settled_proposal_refs() == ()
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    _assert_archive(instance, remote, proposal)


@pytest.mark.parametrize("kind", ["withdrawal", "activation"])
def test_deleted_derived_archive_and_notes_rebuild_from_evidence(tmp_path, kind):
    instance, owner = initialize_local(tmp_path)
    proposal = _submit(instance)
    _settle(instance, owner, proposal, kind)
    remote = _bare_remote(tmp_path, object_format=instance.descriptor.git_object_format)
    assert instance.set_ledger_mirror(str(remote)).status == "current"
    archived, oid = _assert_archive(instance, remote, proposal)
    instance._ledger._git(["update-ref", "-d", archived])
    for kind in ("evaluation", "approval"):
        instance._ledger._git(["update-ref", "-d", NOTE_REFS[kind]])
    # The remote still has the previous archive. Rebuilding produces its exact
    # OID rather than treating a missing disposable local ref as a deletion.
    assert instance.publish_ledger_mirror(timeout=20).status == "current"
    assert _assert_archive(instance, remote, proposal) == (archived, oid)


def test_coalesced_submit_and_withdraw_rebuilds_archive_before_first_open_view(
    tmp_path, monkeypatch
):
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
    _assert_archive(instance, remote, proposal)
