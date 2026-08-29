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
from cruxible_client.contracts.principal_rendering import render_principal
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_principal_change,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.review import service_prepare_playbill_approval
from cruxible_core.service.playbill_proposals import service_list_playbill_proposals
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


def _activate_independent_principal_change(
    instance,
    result,
    *,
    tmp_path: Path,
    owner,  # type: ignore[no-untyped-def]
):
    assert result.candidate is not None
    for material in (owner, client_material(tmp_path, instance)):
        signed = _sign(
            material,
            result.candidate.candidate_digest,
            instance.accepted_coordinate().semantic_root,
        )
        service_submit_playbill_approval(
            instance,
            proposal_id=result.admission.proposal_id,
            attestation=signed.attestation,
            authenticated_submitter=material.principal.principal_id,
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
    assert instance.inspect().approval_policy_mode == "self_approval_allowed"


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
    instance.refresh()
    assert instance.inspect().approval_policy_mode == "independent_approval_required"

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
    with pytest.raises(
        ApprovalIntegrityError,
        match="independent_approval_required.*approver other than the candidate creator",
    ):
        service_prepare_playbill_approval(
            instance,
            proposal_id=governed.admission.proposal_id,
            signer_id="owner",
            access=BodyAccessContext(principal_id="owner", can_read_body=True),
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
    instance.refresh()
    assert instance.inspect().approval_policy_mode == "self_approval_allowed"


def test_independent_mode_refuses_revocation_below_two_ordinaries(tmp_path: Path) -> None:
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
        name="tighten-before-revoke",
    )
    _activate(instance, tightening, tmp_path=tmp_path)
    instance.refresh()
    reviewer = instance._recovered.head.principals.require_active("reviewer")

    refused = service_propose_playbill_principal_change(
        instance,
        principal=reviewer.model_copy(update={"status": "revoked"}),
        actor_id="owner",
        proposal_name="revoke-required-reviewer",
        timestamp="2026-08-16T17:01:00.000000Z",
    ).proposal

    assert refused.candidate is None
    (diagnostic,) = refused.evaluation.diagnostics
    assert diagnostic.code == "playbill.principal.independent_approval_minimum"
    assert (
        "register a replacement ordinary principal in its own ChangeSet first, then revoke"
        in diagnostic.message
    )
    assert "Policy loosening is unavailable until the coordinator convergence" in (
        diagnostic.message
    )
    assert "recovery re-keying" in diagnostic.message

    replacement = generate_client_principal_key(
        tmp_path / "replacement-keys",
        principal_id="replacement",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    combined = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            "principals/replacement.yaml": render_principal(replacement.principal),
            "principals/reviewer.yaml": render_principal(
                reviewer.model_copy(update={"status": "revoked"})
            ),
        },
        name="combined-replacement-and-revocation",
    )
    assert combined.candidate is None
    assert {item.code for item in combined.evaluation.diagnostics} == {
        "playbill.proposal.unregistered_semantic_kind"
    }

    registration = service_propose_playbill_principal_change(
        instance,
        principal=replacement.principal,
        actor_id="owner",
        proposal_name="register-replacement-first",
        timestamp="2026-08-16T17:02:00.000000Z",
    ).proposal
    assert registration.candidate is not None
    assert (
        _activate_independent_principal_change(
            instance,
            registration,
            tmp_path=tmp_path,
            owner=owner,
        ).status
        == "accepted"
    )
    instance.refresh()

    revocation = service_propose_playbill_principal_change(
        instance,
        principal=reviewer.model_copy(update={"status": "revoked"}),
        actor_id="owner",
        proposal_name="revoke-after-replacement",
        timestamp="2026-08-16T17:03:00.000000Z",
    ).proposal
    assert revocation.candidate is not None
    assert (
        _activate_independent_principal_change(
            instance,
            revocation,
            tmp_path=tmp_path,
            owner=owner,
        ).status
        == "accepted"
    )


def test_independent_principal_registration_needs_creator_binding_and_other_approver(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    tightening = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicyV1(mode="independent_approval_required")
            ),
        },
        name="tighten-before-registration",
    )
    _activate(instance, tightening, tmp_path=tmp_path)
    instance.refresh()
    newcomer = generate_client_principal_key(
        tmp_path / "newcomer-keys",
        principal_id="newcomer",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    proposed = service_propose_playbill_principal_change(
        instance,
        principal=newcomer.principal,
        actor_id="owner",
        proposal_name="register-newcomer",
        timestamp="2026-08-16T17:02:00.000000Z",
    ).proposal
    assert proposed.candidate is not None
    owner_approval = _sign(
        owner,
        proposed.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=owner_approval.attestation,
        authenticated_submitter="owner",
    )
    with pytest.raises(
        ApprovalIntegrityError,
        match="independent_approval_required.*approver other than the candidate creator",
    ):
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposed.admission.proposal_id,
            activated_by="owner",
        )
    reviewer = client_material(tmp_path, instance)
    reviewer_approval = _sign(
        reviewer,
        proposed.candidate.candidate_digest,
        instance.accepted_coordinate().semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=proposed.admission.proposal_id,
        attestation=reviewer_approval.attestation,
        authenticated_submitter="reviewer",
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=proposed.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )


def test_malformed_policy_is_a_typed_refusal_and_does_not_poison_inventory(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    refused = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            APPROVAL_POLICY_PATH: b"not canonical policy bytes\n",
        },
        name="malformed-policy",
    )

    assert refused.candidate is None
    assert tuple(item.code for item in refused.evaluation.diagnostics) == (
        "playbill.approval_policy.format_invalid",
    )
    listed = service_list_playbill_proposals(instance)
    matching = tuple(
        item for item in listed.entries if item.proposal_id == refused.admission.proposal_id
    )
    assert len(matching) == 1
    assert matching[0].status == "settled"
    assert matching[0].terminal_reason == "refused"
