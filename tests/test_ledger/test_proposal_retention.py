"""Removing closed proposal roots never removes accepted authority or active work."""

from pathlib import Path

import pytest

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import ProposalContentUnavailable, ProposalIntegrityError
from cruxible_core.ledger import recovery
from cruxible_core.ledger.git import PROPOSAL_ARCHIVE_REF
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
from cruxible_core.service.proposals.review import service_review_playbill_proposal
from tests.core_support._support import initialize_local
from tests.test_ledger.test_active_review_publication import _settle
from tests.test_proposals.test_proposals import TIMESTAMP, _proposal_tree, _request, _shell


def submit(instance, text):
    body = instance.store_document_body(text.encode())
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=_request(instance),
        candidate_tree=_proposal_tree(instance, _shell(body.digest, title=text)),
        timestamp=TIMESTAMP,
    )


@pytest.mark.parametrize("kind", ["withdrawal", "activation"])
def test_archive_retains_closed_candidates_while_accepted_and_active_work_survives(
    tmp_path: Path, monkeypatch, kind: str
):
    instance, owner = initialize_local(tmp_path)
    closed = submit(instance, "closed candidate")
    _settle(instance, owner, closed, kind)
    # Use another Document identity after activation so the new candidate is a create.
    from cruxible_client.contracts.documents import render_document

    active_body = instance.store_document_body(b"active candidate")
    shell = _shell(active_body.digest).model_copy(update={"identity": "document:active"})
    active_tree = dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))
    active_tree["documents/active.json"] = render_document(shell)
    active = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=_request(instance),
        candidate_tree=active_tree,
        timestamp=TIMESTAMP,
    )
    assert active.candidate is not None
    instance._ledger._git(["reflog", "expire", "--expire=now", "--all"])
    instance._ledger._git(["gc", "--prune=now"])
    assert instance._ledger.object_exists(closed.admission.candidate_commit_oid)
    assert instance._ledger.object_exists(active.admission.candidate_commit_oid)
    # Force a full verification from genesis rather than trusting the local checkpoint.
    monkeypatch.setattr(recovery, "load_verified_checkpoint", lambda *args, **kwargs: None)
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    rows = {row.proposal_id: row for row in service_list_playbill_proposals(reopened).entries}
    assert rows[closed.admission.proposal_id].status == "settled"
    assert rows[active.admission.proposal_id].status == "open"
    access = BodyAccessContext(principal_id="owner", can_read_body=True)
    review = service_review_playbill_proposal(
        reopened, proposal_id=active.admission.proposal_id, access=access
    )
    assert review.candidate_digest == active.candidate.candidate_digest
    closed_review = service_review_playbill_proposal(
        reopened, proposal_id=closed.admission.proposal_id, access=access
    )
    assert closed_review.candidate_digest == closed.candidate.candidate_digest
    reopened._reconcile_proposal_review_refs()
    assert {ref for ref in reopened._ledger.mirror_refs() if ref.startswith("refs/settled/")} == {
        PROPOSAL_ARCHIVE_REF
    }


@pytest.mark.parametrize("closed", [False, True])
def test_missing_active_content_is_integrity_failure_not_expiration(tmp_path, closed):
    instance, owner = initialize_local(tmp_path)
    proposal = submit(instance, "missing bytes")
    if closed:
        _settle(instance, owner, proposal, "withdrawal")
        instance._ledger._git(["update-ref", "-d", PROPOSAL_ARCHIVE_REF])
    else:
        instance._ledger._git(["update-ref", "-d", proposal.admission.target_ref])
        instance._ledger._git(
            ["update-ref", "-d", "refs/heads/proposals/" + proposal.admission.proposal_id[7:]]
        )
    instance._ledger._git(["reflog", "expire", "--expire=now", "--all"])
    instance._ledger._git(["gc", "--prune=now"])
    expected = ProposalContentUnavailable if closed else ProposalIntegrityError
    with pytest.raises(expected):
        service_review_playbill_proposal(
            instance,
            proposal_id=proposal.admission.proposal_id,
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
        )
