"""Public review-bound approval guard and custody compatibility checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client import (
    AccessProfile,
    ApprovalReviewMismatch,
    Cruxible,
    LocalEd25519ApprovalSigner,
)
from cruxible_client import contracts as api
from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_client.contracts.errors import ReviewedCandidateChanged
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner as CoreSigner
from tests.test_integration.test_approval_attestations import ROOT, _candidate


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
        self.review = api.ProposalReview(
            proposal_id="proposal-one",
            candidate=candidate.model_dump(mode="json"),
            candidate_digest=candidate.candidate_digest,
            parent_semantic_root=ROOT,
            settlement_base=api.AcceptedCoordinate(
                git_oid="a" * 40,
                semantic_root=ROOT,
                generation_root="sha256:" + "b" * 64,
                compiler_digest=candidate.compiler_digest,
            ),
            base_oid="a" * 40,
            complete_members=[m.model_dump(mode="json") for m in candidate.members],
            members=[
                api.ReviewedMember(
                    path=candidate.members[0].path,
                    artifact_kind=candidate.members[0].artifact_kind,
                    disposition=candidate.members[0].disposition,
                    closure_role=candidate.members[0].closure_role,
                    predecessor_artifact_digest=candidate.members[0].predecessor_artifact_digest,
                    candidate_artifact_digest=candidate.members[0].candidate_artifact_digest,
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
            documents=[
                {"path": member.path}
                for member in candidate.members
                if member.artifact_kind == "document"
            ],
            redactions=[],
        )
        self.changes: dict[str, Any] = {}
        self.submitted = 0
        self.prepared = 0
        self.receipt_change = False
        self.bound: list[str | None] = []
        self.recompiled: api.ProposalRecompiled | None = None

    def review_proposal(
        self,
        *_args: object,
        include_body: bool | None = None,
        detail: bool = False,
        groups: bool = True,
    ):
        if not detail:
            # The reviewer's view of the same candidate, which str(review) prints.
            assert include_body is None and groups is False
            return api.ProposalReviewSummary(
                proposal_id=self.review.proposal_id,
                candidate_digest=self.review.candidate_digest,
                settlement_base=self.review.settlement_base,
                counts={"created": 1},
                subject_kinds={},
                subject_kind_count=0,
                required_tier="governed_write",
                activation_policy="snapshot",
                approval_requirements=(),
                findings=(),
                finding_count=0,
                groups=(),
                group_count=0,
            )
        assert include_body
        return self.review

    def prepare_approval(
        self,
        *_args: object,
        signer_id: str,
        include_body: bool,
        reviewed_candidate_digest: str | None = None,
    ):
        assert include_body
        self.prepared += 1
        self.bound.append(reviewed_candidate_digest)
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
        return api.ApprovalChallenge.model_validate(raw)

    def submit_approval(
        self,
        *_args: object,
        attestation: dict[str, Any],
        hold: bool = False,
        reviewed_candidate_digest: str | None = None,
    ):
        self.submitted += 1
        self.held = hold
        self.bound.append(reviewed_candidate_digest)
        if self.recompiled is not None:
            return self.recompiled
        signed = ApprovalAttestation.model_validate(attestation)
        return api.ApprovalReceipt(
            proposal_id="wrong" if self.receipt_change else self.review.proposal_id,
            candidate_digest=signed.payload_digest,
            signer_id=signed.signer_id,
            submitted_by="relay",
            signing_semantic_root=signed.signing_semantic_root,
            attestation_digest=approval_digest(signed).tagged,
            key_history_ref=f"principals/{signed.signer_id}.json@{ROOT}",
            not_activated="held" if hold else "approvals_remaining",
            approvals_remaining=0 if hold else 1,
        )


@pytest.fixture
def setup(tmp_path: Path):
    signer = Signer()
    client = Client(signer)
    pb = Cruxible(
        client=client,
        instance_id="instance-one",
        workspace=tmp_path,
        access_profile=AccessProfile("test", (), False),
        clock=None,
    )  # type: ignore[arg-type]
    return pb, client, signer


def test_exact_approval_detaches_nested_review(setup) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    # print(review) shows the reviewer's view, as `cruxible proposal review` prints it.
    assert str(reviewed) == str(reviewed.summary)
    assert "Changes: 1 created / 0 revised / 0 retired" in str(reviewed)
    details = reviewed.details
    details.candidate["candidate"]["parent_semantic_root"] = "sha256:" + "f" * 64
    details.complete_members.clear()
    assert reviewed.details.complete_members
    client.review.attestation_coverage["attestations"] = ["another reviewer"]
    receipt = proposal.approve(signer=signer, reviewed=reviewed)
    assert receipt.submitted_by == "relay"  # submitter is not signer
    assert signer.calls == client.submitted == 1
    assert client.held is False  # the daemon activates a candidate this completes
    assert CoreSigner is LocalEd25519ApprovalSigner


def _recompiled(**changes: Any) -> api.ProposalRecompiled:
    values: dict[str, Any] = dict(
        status="pending_review",
        source_proposal_id="proposal-one",
        proposal_id="proposal-two",
        candidate_digest="sha256:" + "4" * 64,
        members_identical=True,
        accepted_coordinate=api.AcceptedCoordinate(
            git_oid="1" * 40,
            semantic_root="sha256:" + "5" * 64,
            generation_root="sha256:" + "6" * 64,
            compiler_digest="sha256:" + "7" * 64,
        ),
    )
    return api.ProposalRecompiled(**{**values, **changes})


def test_approval_is_bound_to_the_reviewed_candidate(setup) -> None:
    """coldmcp-10: the SDK passes the reviewed digest, and a successor is never
    returned as if approved: it is the refusal the CLI and MCP give, with the
    SDK's own review call as the repair."""

    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    proposal.approve(signer=signer, reviewed=reviewed)
    assert client.bound == [reviewed.candidate_digest, reviewed.candidate_digest]

    client.recompiled = _recompiled()
    with pytest.raises(ReviewedCandidateChanged) as refused:
        proposal.approve(signer=signer, reviewed=reviewed)
    assert str(refused.value) == (
        "cruxible.approval.reviewed_candidate_changed: proposal proposal-one was proposed "
        f"again at the head as proposal-two (candidate {'sha256:' + '4' * 64}), not the "
        f"reviewed {reviewed.candidate_digest}; members identical: true; nothing was signed. "
        "Review proposal-two and approve it"
    )
    assert refused.value.__notes__ == ['Repair: cx.proposal("proposal-two").review()']

    # Already live at the head: nothing is left to sign, and the receipt says so.
    client.recompiled = _recompiled(
        status="already_live", proposal_id=None, candidate_digest=None, members_identical=False
    )
    assert proposal.approve(signer=signer, reviewed=reviewed) == client.recompiled


def test_challenge_for_a_landed_proposal_is_refused_by_name(setup) -> None:
    """A signer registered after the base: the challenge names the landed proposal."""

    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    reviewed = proposal.review()
    client.changes = {
        "proposal_id": "proposal-two",
        "statement": ApprovalStatement(
            signer_id="reviewer", signing_semantic_root=ROOT, payload_digest="sha256:" + "4" * 64
        ).model_dump(),
    }
    with pytest.raises(ReviewedCandidateChanged, match="as proposal-two"):
        proposal.approve(signer=signer, reviewed=reviewed)
    assert signer.calls == client.submitted == 0


def test_held_approval_asks_the_daemon_to_sign_only(setup) -> None:
    pb, client, signer = setup
    proposal = pb.proposal("proposal-one")
    receipt = proposal.approve(signer=signer, reviewed=proposal.review(), hold=True)
    assert client.held is True
    assert receipt.activation is None and receipt.not_activated == "held"


@pytest.mark.parametrize("other", ["session", "instance", "proposal"])
def test_foreign_review_refuses_before_challenge_or_signer(setup, tmp_path, other) -> None:
    pb, client, signer = setup
    reviewed = pb.proposal("proposal-one").review()
    if other == "proposal":
        target = pb.proposal("proposal-two")
    else:
        target = Cruxible(
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
    if field == "members":
        client.review.members.clear()
    else:
        # The candidate carries no Document; a rendered one it lacks refuses.
        client.review.documents.append({"path": "documents/extra.json"})
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


def test_a_bound_approval_names_the_skew_when_an_older_daemon_rejects_the_field() -> None:
    """Review F-004: a daemon from before bound approvals forbids unknown request
    fields, so a request carrying ``reviewed_candidate_digest`` fails its
    validation. The client says that is version skew, typed, on both calls; it
    never drops the digest and retries unbound. An unbound request sends the
    bytes the older daemon accepts."""

    import json

    import httpx
    from pydantic import BaseModel, ConfigDict, ValidationError

    from cruxible_client import CruxibleClient
    from cruxible_client.contracts.errors import ReviewedBindingUnsupported

    class _Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class OlderChallengeRequest(_Strict):
        signer_id: str
        include_body: bool = False

    class OlderApprovalRequest(_Strict):
        attestation: dict[str, Any]
        hold: bool = False

    seen: list[tuple[str, dict[str, Any]]] = []

    def older_daemon(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[-1], body))
        model = (
            OlderChallengeRequest
            if request.url.path.endswith("approval-challenge")
            else OlderApprovalRequest
        )
        try:
            model.model_validate(body)
        except ValidationError as exc:
            return httpx.Response(
                422,
                json={
                    "error_type": "RequestValidationError",
                    "message": "Request validation failed",
                    "error_code": "cruxible.request.invalid",
                    "errors": [
                        f"body.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                        for error in exc.errors()
                    ],
                },
            )
        return httpx.Response(500, json={"error_type": "CoreError", "message": "accepted"})

    client = CruxibleClient(base_url="http://cruxible")
    client._client = httpx.Client(  # type: ignore[attr-defined]
        base_url="http://cruxible", transport=httpx.MockTransport(older_daemon)
    )
    digest = "sha256:" + "3" * 64
    attestation = {"signer_id": "reviewer", "sig": "0" * 128}
    for call in (
        lambda **bound: client.prepare_approval("inst_1", "PROP-1", signer_id="reviewer", **bound),
        lambda **bound: client.submit_approval(
            "inst_1", "PROP-1", attestation=attestation, **bound
        ),
    ):
        with pytest.raises(ReviewedBindingUnsupported) as refused:
            call(reviewed_candidate_digest=digest)
        assert refused.value.error_code == "cruxible.approval.reviewed_binding_unsupported"
        assert "the daemon is older than this client" in str(refused.value)
        assert "nothing was signed" in str(refused.value)
        assert "upgrade the daemon" in str(refused.value)
        # Unbound: the older daemon's own request, which it validates and answers.
        with pytest.raises(Exception, match="accepted"):
            call()
    assert [(name, "reviewed_candidate_digest" in body) for name, body in seen] == [
        ("approval-challenge", True),
        ("approval-challenge", False),
        ("approvals", True),
        ("approvals", False),
    ]
