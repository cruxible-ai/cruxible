"""The approval that completes a candidate activates it in the same call."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.documents import render_document
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_principal_change,
    service_submit_playbill_approval,
)
from tests.core_support._support import client_material, initialize_local
from tests.test_ledger.test_activation import _sign
from tests.test_proposals.test_approval_policy import _submit_tree
from tests.test_proposals.test_proposals import DOCUMENT_PATH, _shell


def _document_proposal(instance, *, name: str, text: bytes = b"# Approve me\n"):  # type: ignore[no-untyped-def]
    body = instance.store_document_body(text)
    result = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            DOCUMENT_PATH: render_document(_shell(body.digest)),
        },
        name=name,
    )
    assert result.candidate is not None
    return result


def _approve(instance, result, material, **options):  # type: ignore[no-untyped-def]
    assert result.candidate is not None
    signed = _sign(
        material,
        result.candidate.candidate_digest,
        result.candidate.candidate.parent_semantic_root,
    )
    return service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter=material.principal.principal_id,
        **options,
    )


def test_completing_approval_activates_as_the_submitter(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _document_proposal(instance, name="approve-activates")
    before = instance.accepted_coordinate()

    receipt = _approve(
        instance,
        result,
        client_material(tmp_path, instance),
        activate_when_complete=True,
        may_activate=True,
    )

    assert receipt.not_activated is None and receipt.approvals_remaining == 0
    assert receipt.activation is not None
    assert receipt.activation.status == "accepted"
    assert receipt.activation.activated_by == "reviewer"
    assert instance.accepted_coordinate() != before


def test_held_or_unpermitted_approval_signs_only(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    reviewer = client_material(tmp_path, instance)
    held = _document_proposal(instance, name="held-approval")
    head = instance.accepted_coordinate()

    receipt = _approve(instance, held, reviewer, activate_when_complete=False, may_activate=True)

    assert receipt.activation is None and receipt.not_activated == "held"
    assert instance.accepted_coordinate() == head
    activated = service_activate_playbill_proposal(
        instance, proposal_id=held.admission.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"

    unpermitted = _document_proposal(instance, name="unpermitted", text=b"# Second\n")
    head = instance.accepted_coordinate()
    receipt = _approve(
        instance, unpermitted, reviewer, activate_when_complete=True, may_activate=False
    )
    assert receipt.activation is None and receipt.not_activated == "activation_not_permitted"
    assert instance.accepted_coordinate() == head


def test_stale_candidate_is_signed_but_reported_stale(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    reviewer = client_material(tmp_path, instance)
    first = _document_proposal(instance, name="first-of-two")
    second = _document_proposal(instance, name="second-of-two", text=b"# Other\n")
    assert _approve(
        instance, first, reviewer, activate_when_complete=True, may_activate=True
    ).activation

    receipt = _approve(instance, second, reviewer, activate_when_complete=True, may_activate=True)

    assert receipt.activation is None and receipt.not_activated == "stale"


def test_lifecycle_candidate_counts_its_actors_own_signature(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    newcomer = generate_client_principal_key(
        tmp_path / "newcomer-keys",
        principal_id="newcomer",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    registration = service_propose_playbill_principal_change(
        instance,
        principal=newcomer.principal,
        actor_id="owner",
        proposal_name="register-newcomer",
        timestamp="2026-08-16T17:02:00.000000Z",
    ).proposal
    head = instance.accepted_coordinate()

    voluntary = _approve(
        instance,
        registration,
        client_material(tmp_path, instance),
        activate_when_complete=True,
        may_activate=True,
    )
    assert voluntary.not_activated == "approvals_remaining"
    assert voluntary.approvals_remaining == 1
    assert instance.accepted_coordinate() == head

    completing = _approve(
        instance, registration, owner, activate_when_complete=True, may_activate=True
    )
    assert completing.activation is not None
    assert completing.activation.status == "accepted"
