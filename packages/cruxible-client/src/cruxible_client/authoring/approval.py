"""Review-bound approval conveniences; authority remains with the served verifier."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import TypeAdapter

from cruxible_client import contracts as api
from cruxible_client.authoring.sdk_types import PlaybillSdkError
from cruxible_client.authoring.signing import ApprovalSigner
from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    approval_digest,
    approval_statement_bytes,
)
from cruxible_client.contracts.candidates import (
    CandidateMemberEvidence,
    CandidateMemberLawEvidenceV2,
    CandidateRecordAnyVersion,
    candidate_digest,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.types import PrincipalRecord

if TYPE_CHECKING:
    from cruxible_client.authoring.sdk import Playbill

_CANDIDATE: TypeAdapter[CandidateRecordAnyVersion] = TypeAdapter(CandidateRecordAnyVersion)


class ApprovalReviewMismatch(PlaybillSdkError):
    """A local approval binding failed; no replacement candidate is auto-reviewed."""

    code = "playbill.sdk.approval_review_mismatch"
    repair = (
        "Review this proposal again and explicitly approve that review with the configured signer."
    )

    def __init__(self, message: str) -> None:
        super().__init__(f"{message}. Repair: {self.repair}")


@dataclass(frozen=True, slots=True)
class ReviewedProposal:
    """Immutable review snapshot bound to its originating SDK session and instance.

    Inspect ``details`` before approval. Each access returns a detached public
    contract; editing that copy cannot change the candidate this token identifies.
    """

    _owner: Playbill = field(repr=False, compare=False)
    _instance_id: str = field(repr=False)
    _snapshot: bytes = field(repr=False)
    proposal_id: str
    candidate_digest: str

    @property
    def details(self) -> api.PlaybillProposalReview:
        return api.PlaybillProposalReview.model_validate_json(self._snapshot)


def _checked_review(review: api.PlaybillProposalReview, proposal_id: str) -> None:
    candidate = _CANDIDATE.validate_python(review.candidate)
    if (
        review.proposal_id != proposal_id
        or review.candidate_digest != candidate.candidate_digest
        or review.candidate_digest != candidate_digest(candidate.candidate).tagged
        or review.parent_semantic_root != candidate.candidate.parent_semantic_root
        or review.settlement_base.semantic_root != review.parent_semantic_root
        or review.base_oid != review.settlement_base.git_oid
        or canonical_bytes(review.complete_members)
        != canonical_bytes([member.model_dump(mode="json") for member in candidate.members])
    ):
        raise ApprovalReviewMismatch("Review candidate, member roll, or settlement base differs")
    if [member.path for member in review.members] != [member.path for member in candidate.members]:
        raise ApprovalReviewMismatch("Review omits or duplicates candidate members")
    candidate_members: tuple[CandidateMemberEvidence | CandidateMemberLawEvidenceV2, ...] = (
        candidate.members
    )
    for rendered, member in zip(review.members, candidate_members, strict=True):
        expected_digest = (
            member.artifact_digest
            if isinstance(member, CandidateMemberEvidence)
            else member.candidate_artifact_digest
        )
        if (
            rendered.artifact_kind != member.artifact_kind
            or rendered.disposition != member.disposition
            or rendered.candidate_artifact_digest != expected_digest
        ):
            raise ApprovalReviewMismatch("Rendered member differs from candidate member")
    if [document.get("path") for document in review.documents] != [
        member.path for member in candidate.members if member.artifact_kind == "document"
    ]:
        raise ApprovalReviewMismatch("Review omits or duplicates candidate Documents")
    if review.redactions:
        raise ApprovalReviewMismatch("This approval helper requires a complete, unredacted review")


def review_proposal(playbill: Playbill, proposal_id: str) -> ReviewedProposal:
    raw = playbill._client.review_playbill_proposal(
        playbill._instance_id, proposal_id, include_body=True
    )
    review = api.PlaybillProposalReview.model_validate(raw.model_dump(mode="json"))
    try:
        _checked_review(review, proposal_id)
    except ValueError as exc:
        raise ApprovalReviewMismatch("Daemon returned an inconsistent review") from exc
    return ReviewedProposal(
        playbill,
        playbill._instance_id,
        canonical_bytes(review.model_dump(mode="json")),
        review.proposal_id,
        review.candidate_digest,
    )


def approve_reviewed(
    playbill: Playbill,
    proposal_id: str,
    *,
    signer: ApprovalSigner,
    reviewed: ReviewedProposal,
) -> api.PlaybillApprovalReceipt:
    if (
        not isinstance(reviewed, ReviewedProposal)
        or reviewed._owner is not playbill
        or reviewed._instance_id != playbill._instance_id
        or reviewed.proposal_id != proposal_id
    ):
        raise ApprovalReviewMismatch(
            "Review belongs to a different SDK session, instance, or proposal"
        )
    review = reviewed.details
    if review.candidate_digest != reviewed.candidate_digest:
        raise ApprovalReviewMismatch("Review token advertises a different candidate digest")
    signer_id, public_key = signer.signer_id, signer.public_key
    raw = playbill._client.prepare_playbill_approval(
        playbill._instance_id, proposal_id, signer_id=signer_id, include_body=True
    )
    try:
        challenge = api.PlaybillApprovalChallenge.model_validate(raw.model_dump(mode="json"))
        _checked_review(review, proposal_id)
        _checked_review(challenge.review, proposal_id)
        # Coverage and projection advice are live observations, not candidate identity.
        for name in (
            "candidate",
            "candidate_digest",
            "parent_semantic_root",
            "settlement_base",
            "base_oid",
            "complete_members",
            "members",
            "governance",
            "provenance",
            "documents",
        ):
            if getattr(review, name) != getattr(challenge.review, name):
                raise ApprovalReviewMismatch(f"Fresh challenge changed reviewed {name}")
        statement = ApprovalStatement.model_validate(challenge.statement)
        principal = PrincipalRecord.model_validate(challenge.signer_principal)
        history_ref = f"principals/{signer_id}.json@{review.parent_semantic_root}"
        if (
            challenge.proposal_id != proposal_id
            or statement.payload_digest != review.candidate_digest
            or statement.signing_semantic_root != review.parent_semantic_root
            or statement.signer_id != signer_id
            or principal.principal_id != signer_id
            or principal.public_key != public_key
            or principal.status != "active"
            or principal.kind == "daemon"
            or challenge.signer_key_history_ref != history_ref
        ):
            raise ApprovalReviewMismatch(
                "Challenge statement or signer identity differs from review"
            )
        if principal.kind == "recovery" and not all(
            member.get("artifact_kind") == "principal-lifecycle"
            for member in review.complete_members
        ):
            raise ApprovalReviewMismatch("Recovery signer cannot approve an ordinary candidate")
        expected = approval_statement_bytes(statement)
    except ValueError as exc:
        raise ApprovalReviewMismatch(
            "Fresh approval challenge does not match this review and signer"
        ) from exc
    # Capture immutable expectations before invoking arbitrary caller-owned code.
    signed = signer.sign(statement)
    try:
        attestation = ApprovalAttestation.model_validate(signed.model_dump(mode="json"))
        if approval_statement_bytes(attestation.statement) != expected:
            raise ApprovalReviewMismatch("Signer returned a different approval statement")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(attestation.sig), expected
        )
    except (ValueError, InvalidSignature, AttributeError) as exc:
        raise ApprovalReviewMismatch("Signer returned an invalid or substituted approval") from exc
    expected_digest = approval_digest(attestation).tagged
    raw_receipt = playbill._client.submit_playbill_approval(
        playbill._instance_id, proposal_id, attestation=attestation.model_dump(mode="json")
    )
    receipt = api.PlaybillApprovalReceipt.model_validate(raw_receipt.model_dump(mode="json"))
    if (
        receipt.proposal_id != proposal_id
        or receipt.candidate_digest != review.candidate_digest
        or receipt.signer_id != signer_id
        or receipt.signing_semantic_root != review.parent_semantic_root
        or receipt.attestation_digest != expected_digest
        or receipt.key_history_ref != history_ref
    ):
        raise ApprovalReviewMismatch(
            "Approval was submitted but receipt identity differs; "
            "inspect proposal status before retrying"
        )
    return receipt
