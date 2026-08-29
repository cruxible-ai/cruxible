"""The governed approval singleton derives requirements from the parent tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.approval_policy import (
    APPROVAL_POLICY_PATH,
    ApprovalPolicyV1,
    parse_approval_policy,
    render_approval_policy,
)
from cruxible_client.contracts.documents import render_document
from cruxible_client.contracts.errors import ApprovalIntegrityError
from cruxible_client.contracts.governance import INDEPENDENT_APPROVAL_REQUIREMENTS
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposals import DOCUMENT_PATH, TIMESTAMP, _shell


def _submit_tree(instance, tree, *, name: str):  # type: ignore[no-untyped-def]
    return instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}",
            proposed_base_oid=instance.accepted_coordinate().git_oid,
        ),
        candidate_tree=tree,
        timestamp=TIMESTAMP,
    )


def _activate(instance, result, *, tmp_path: Path, approve: bool = False):  # type: ignore[no-untyped-def]
    assert result.candidate is not None
    if approve:
        reviewer = client_material(tmp_path, instance)
        signed = _sign(
            reviewer,
            result.candidate.candidate_digest,
            instance.accepted_coordinate().semantic_root,
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=result.admission.proposal_id,
            attestation=signed.attestation,
            authenticated_submitter="reviewer",
        )
    return service_activate_playbill_proposal(
        instance,
        proposal_id=result.admission.proposal_id,
        activated_by="owner",
    )


def test_default_genesis_is_solo_capable_and_policy_is_governed(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)

    policy = parse_approval_policy(
        instance.tree_at(instance.accepted_coordinate().git_oid)[APPROVAL_POLICY_PATH],
        path=APPROVAL_POLICY_PATH,
    )

    assert policy == ApprovalPolicyV1(mode="self_approval_allowed")


def test_policy_tightening_and_loosening_follow_the_parent_policy(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tightening = _submit_tree(
        instance,
        {
            **tree,
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicyV1(mode="independent_approval_required")
            ),
        },
        name="tighten-policy",
    )
    assert tightening.candidate is not None
    assert tightening.candidate.approval_requirements == ()
    assert _activate(instance, tightening, tmp_path=tmp_path).status == "accepted"

    body = instance.store_document_body(b"# Independent review\n")
    governed = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            DOCUMENT_PATH: render_document(_shell(body.digest)),
        },
        name="independent-document",
    )
    assert governed.candidate is not None
    assert governed.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS
    with pytest.raises(ApprovalIntegrityError, match="requirement_unsatisfied"):
        _activate(instance, governed, tmp_path=tmp_path)
    creator = _sign(
        owner,
        governed.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    with pytest.raises(ApprovalIntegrityError, match="creator_forbidden"):
        service_submit_playbill_approval(
            instance,
            proposal_id=governed.admission.proposal_id,
            attestation=creator.attestation,
            authenticated_submitter="owner",
        )
    assert _activate(instance, governed, tmp_path=tmp_path, approve=True).status == "accepted"

    loosening = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicyV1(mode="self_approval_allowed")
            ),
        },
        name="loosen-policy",
    )
    assert loosening.candidate is not None
    assert loosening.candidate.approval_requirements == INDEPENDENT_APPROVAL_REQUIREMENTS
    with pytest.raises(ApprovalIntegrityError, match="requirement_unsatisfied"):
        _activate(instance, loosening, tmp_path=tmp_path)
    assert _activate(instance, loosening, tmp_path=tmp_path, approve=True).status == "accepted"
