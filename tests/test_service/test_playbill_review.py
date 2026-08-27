"""PB-E structured review and client-held signing tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import PlaybillKeyError
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.service.documents import (
    service_propose_playbill_principal_change,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner
from cruxible_core.service.playbill_documents import (
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_review import (
    render_playbill_proposal_review,
    service_prepare_playbill_approval,
    service_review_playbill_proposal,
)
from tests.test_playbill._support import generate_client
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance, _shell


def test_review_and_signing_keep_private_key_outside_wire_contract(tmp_path: Path) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    body = service_store_playbill_body(instance, content=b"# Playbill\n\nGoverned prose.\n")
    proposed = service_propose_playbill_document(
        instance,
        shell=_shell(body.digest),
        actor_id="owner",
        proposal_name="review",
        timestamp=TIMESTAMP,
    )
    proposal_id = proposed.proposal.admission.proposal_id

    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert review.coordinate_kind == "provisional"
    assert review.base_oid == review.settlement_base.git_oid
    assert review.parent_semantic_root == review.settlement_base.semantic_root
    assert review.candidate_digest == review.candidate.candidate_digest
    assert review.complete_members == review.candidate.members
    assert review.documents[0].candidate_source_mapping is not None
    assert "+# Playbill" in (review.documents[0].readable_diff or "")
    assert review.attestation_coverage["coverage"] == "containing_change_set"
    rendered = render_playbill_proposal_review(review)
    assert f"Candidate: {review.candidate_digest}" in rendered
    assert f"Settlement base OID: {review.base_oid}" in rendered
    assert f"Proposal admission tier: {review.candidate.required_tier}" in rendered
    assert "Approve requires: graph_write" in rendered
    assert "Activate requires: graph_write" in rendered

    redacted = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=BodyAccessContext(principal_id="reader"),
    )
    assert redacted.documents[0].readable_diff is None
    assert redacted.documents[0].candidate_source_mapping is None
    assert "body" in redacted.redactions
    assert body.digest in redacted.model_dump_json()
    assert "Governed prose" not in redacted.model_dump_json()

    challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=proposal_id,
        signer_id="reviewer",
        access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
    )
    serialized = challenge.model_dump_json()
    assert str(reviewer.private_key_path) not in serialized
    assert challenge.statement.payload_digest == review.candidate_digest
    assert challenge.statement.signing_semantic_root == review.parent_semantic_root

    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=reviewer.private_key_path,
        expected_public_key=challenge.signer_principal.public_key,
        forbidden_roots=(instance.root, tmp_path / "workspace"),
    )
    attestation = signer.sign(challenge.statement)
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=attestation,
        authenticated_submitter="bearer-owner",
    )
    assert receipt.signer_id == "reviewer"
    assert receipt.submitted_by == "bearer-owner"


def test_local_signer_refuses_exposed_or_wrong_key(tmp_path: Path) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    with pytest.raises(PlaybillKeyError, match="does not match"):
        LocalEd25519ApprovalSigner.open(
            signer_id="owner",
            private_key_path=owner.private_key_path,
            expected_public_key=reviewer.principal.public_key,
            forbidden_roots=(instance.root, tmp_path / "workspace"),
        )

    os.chmod(owner.private_key_path, 0o644)
    with pytest.raises(PlaybillKeyError, match="permissions"):
        LocalEd25519ApprovalSigner.open(
            signer_id="owner",
            private_key_path=owner.private_key_path,
            expected_public_key=owner.principal.public_key,
            forbidden_roots=(instance.root, tmp_path / "workspace"),
        )


def test_lifecycle_review_names_the_proposing_actor(tmp_path: Path) -> None:
    instance, owner, _reviewer = _instance(tmp_path)
    keys = tmp_path / "keys-alice"
    keys.mkdir()
    record = generate_client(
        tmp_path, managed_root=tmp_path / "managed-alice", principal_id="alice", roles=("reviewer",)
    )
    proposal = service_propose_playbill_principal_change(
        instance,
        principal=record.principal,
        proposal_name="alice",
        actor_id=owner.principal.principal_id,
        timestamp=TIMESTAMP,
    )
    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal.proposal.admission.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    rendered = render_playbill_proposal_review(review)
    assert f"{owner.principal.principal_id}'s own signature" in rendered
    assert "Required approvals: none" not in rendered
