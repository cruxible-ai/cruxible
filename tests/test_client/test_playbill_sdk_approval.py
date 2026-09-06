"""Public review-bound approval guard and custody compatibility checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client import (
    AccessProfile,
    ApprovalReviewMismatch,
    LocalEd25519ApprovalSigner,
    Playbill,
)
from cruxible_client import contracts as api
from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner as CoreSigner
from tests.test_playbill.test_approval_attestations import ROOT, _candidate


class Signer:
    signer_id = "reviewer"

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.public_key = self.key.public_key().public_bytes_raw().hex()
        self.calls = 0
        self.mode = "normal"

    def sign(self, statement: ApprovalStatement) -> ApprovalAttestation:
        self.calls += 1
        if self.mode == "mutate":
            statement.__dict__["payload_digest"] = "sha256:" + "f" * 64
        signature = self.key.sign(approval_statement_bytes(statement)).hex()
        if self.mode == "invalid":
            signature = "0" * 128
        return ApprovalAttestation(**statement.model_dump(), sig=signature)


class Client:
    def __init__(self, signer: Signer) -> None:
        self.signer = signer
        candidate = _candidate()
        self.review = api.PlaybillProposalReview(
            proposal_id="proposal-one",
            candidate=candidate.model_dump(mode="json"),
            candidate_digest=candidate.candidate_digest,
            parent_semantic_root=ROOT,
            settlement_base=api.PlaybillAcceptedCoordinate(
                git_oid="a" * 40,
                semantic_root=ROOT,
                generation_root="sha256:" + "b" * 64,
                compiler_digest=candidate.compiler_digest,
            ),
            base_oid="a" * 40,
            complete_members=[m.model_dump(mode="json") for m in candidate.members],
            members=[
                api.PlaybillReviewedMember(
                    path=candidate.members[0].path,
                    artifact_kind="document",
                    disposition="replacement",
                    closure_role="authored",
                    predecessor_artifact_digest=None,
                    candidate_artifact_digest=candidate.members[0].artifact_digest,
                    base_semantic_artifact=None,
                    candidate_semantic_artifact=None,
                    semantic_delta=[],
                    law_identifier=candidate.members[0].law_identifier,
                    law_digest=candidate.law_digests[candidate.members[0].law_identifier],
                    law_evidence={},
                    dependency_proof_refs=[],
                )
            ],
            governance={},
            provenance={"actor_id": "creator"},
            attestation_coverage={},
            documents=[{"path": candidate.members[0].path}],
            redactions=[],
        )
        self.changes: dict[str, Any] = {}
        self.submitted = 0
        self.prepared = 0
        self.receipt_change = False

    def review_playbill_proposal(self, *_args: object, include_body: bool):
        assert include_body
        return self.review

    def prepare_playbill_approval(self, *_args: object, signer_id: str, include_body: bool):
        assert include_body
        self.prepared += 1
        raw = dict(
            proposal_id=self.review.proposal_id,
            signer_principal=dict(
                principal_id=signer_id,
                public_key=self.signer.public_key,
                kind="ordinary",
                status="active",
            ),
            signer_key_history_ref=f"principals/{signer_id}.json@{ROOT}",
            statement=ApprovalStatement(
                signer_id=signer_id,
                signing_semantic_root=ROOT,
                payload_digest=self.review.candidate_digest,
            ).model_dump(),
            review=self.review.model_dump(mode="json"),
        )
        raw.update(self.changes)
        return api.PlaybillApprovalChallenge.model_validate(raw)

    def submit_playbill_approval(self, *_args: object, attestation: dict[str, Any]):
        self.submitted += 1
        signed = ApprovalAttestation.model_validate(attestation)
        return api.PlaybillApprovalReceipt(
            proposal_id="wrong" if self.receipt_change else self.review.proposal_id,
            candidate_digest=signed.payload_digest,
            signer_id=signed.signer_id,
            submitted_by="relay",
            signing_semantic_root=signed.signing_semantic_root,
            attestation_digest=approval_digest(signed).tagged,
            key_history_ref=f"principals/{signed.signer_id}.json@{ROOT}",
        )


@pytest.fixture
def setup(tmp_path: Path):
    signer = Signer()
    client = Client(signer)
    pb = Playbill(
        client=client,
        instance_id="instance-one",
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=None,
    )  # type: ignore[arg-type]
    return pb, client, signer


def test_exact_approval_detaches_nested_review_and_never_activates(setup) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    details = reviewed.details
    details.candidate["candidate"]["parent_semantic_root"] = "sha256:" + "f" * 64
    details.complete_members.clear()
    assert reviewed.details.complete_members
    client.review.attestation_coverage["attestations"] = ["another reviewer"]
    receipt = proposal.approve(signer=signer, reviewed=reviewed)
    assert receipt.submitted_by == "relay"  # submitter is not signer
    assert signer.calls == client.submitted == 1
    assert CoreSigner is LocalEd25519ApprovalSigner


@pytest.mark.parametrize("other", ["session", "instance", "proposal"])
def test_foreign_review_refuses_before_challenge_or_signer(setup, tmp_path, other) -> None:
    pb, client, signer = setup
    reviewed = pb.proposal("proposal-one").review()
    if other == "proposal":
        target = pb.proposal("proposal-two")
    else:
        target = Playbill(
            client=client,
            instance_id="instance-two" if other == "instance" else "instance-one",
            workspace=tmp_path,
            access_profile=AccessProfile("test", (), False),
            clock=None,
        ).proposal("proposal-one")
    with pytest.raises(ApprovalReviewMismatch):
        target.approve(signer=signer, reviewed=reviewed)
    assert client.prepared == signer.calls == client.submitted == 0


@pytest.mark.parametrize(
    "field",
    ["proposal_id", "statement", "key", "history", "redactions", "members", "candidate", "root"],
)
def test_changed_challenge_refuses_before_signing(setup, field) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    if field == "proposal_id":
        client.changes[field] = "different"
    elif field == "statement":
        client.changes[field] = ApprovalStatement(
            signer_id="reviewer", signing_semantic_root=ROOT, payload_digest="sha256:" + "f" * 64
        ).model_dump()
    elif field == "key":
        client.changes["signer_principal"] = dict(
            principal_id="reviewer", public_key="0" * 64, kind="ordinary"
        )
    elif field == "history":
        client.changes["signer_key_history_ref"] = "wrong"
    else:
        changed = client.review.model_dump(mode="json")
        if field == "redactions":
            changed[field] = ["body"]
        elif field == "members":
            changed["complete_members"] = []
        elif field == "candidate":
            changed["candidate"]["candidate"]["timestamp"] = "2026-08-13T12:00:00.000000Z"
        else:
            changed["parent_semantic_root"] = "sha256:" + "f" * 64
        client.changes["review"] = changed
    with pytest.raises(ApprovalReviewMismatch):
        proposal.approve(signer=signer, reviewed=reviewed)
    assert signer.calls == client.submitted == 0


@pytest.mark.parametrize("mode", ["mutate", "invalid"])
def test_bad_signer_result_never_reaches_transport(setup, mode) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    signer.mode = mode
    with pytest.raises(ApprovalReviewMismatch):
        proposal.approve(signer=signer, reviewed=reviewed)
    assert signer.calls == 1
    assert client.submitted == 0


def test_bad_receipt_is_reported_after_submission(setup) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    client.receipt_change = True
    with pytest.raises(ApprovalReviewMismatch, match="submitted"):
        proposal.approve(signer=signer, reviewed=reviewed)
    assert client.submitted == 1


def test_redacted_initial_review_has_explicit_repair(setup) -> None:
    pb, client, _ = setup
    client.review.redactions.append("body")
    with pytest.raises(ApprovalReviewMismatch) as failure:
        pb.proposal("proposal-one").review()
    assert "Review" in failure.value.repair


@pytest.mark.parametrize("field", ["members", "documents"])
def test_missing_rendered_member_coverage_refuses_review(setup, field) -> None:
    pb, client, signer = setup
    getattr(client.review, field).clear()
    with pytest.raises(ApprovalReviewMismatch):
        pb.proposal("proposal-one").review()
    assert signer.calls == client.prepared == client.submitted == 0


def test_modified_token_digest_refuses_before_challenge(setup) -> None:
    from dataclasses import replace

    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = replace(proposal.review(), candidate_digest="sha256:" + "f" * 64)
    with pytest.raises(ApprovalReviewMismatch):
        proposal.approve(signer=signer, reviewed=reviewed)
    assert client.prepared == signer.calls == client.submitted == 0
