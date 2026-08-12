"""Externally produced Playbill approval attestations and quorum verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_core.playbill.candidates import CandidateRecord, SemanticCandidate, candidate_digest
from cruxible_core.playbill.canonical import (
    ApprovalDigest,
    CandidateDigest,
    SemanticRoot,
    canonical_bytes,
    typed_digest,
)
from cruxible_core.playbill.errors import ApprovalIntegrityError, PrincipalIntegrityError
from cruxible_core.playbill.governance import governance_identifier
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot
from cruxible_core.playbill.types import PrincipalRole

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
ApprovalPurpose = Literal["ordinary-artifact", "principal-lifecycle"]


class _StrictAttestationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalStatement(_StrictAttestationModel):
    """The exact unsigned `playbill-attest-v1` signature preimage."""

    tag: str = "playbill-attest-v1"
    signer_id: str
    signing_semantic_root: str
    payload_digest: str

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str) -> str:
        if value != "playbill-attest-v1":
            raise ValueError("approval statement tag is unsupported")
        return value

    @field_validator("signer_id")
    @classmethod
    def _signer_id(cls, value: str) -> str:
        return governance_identifier(value, label="approval signer_id")

    @field_validator("signing_semantic_root")
    @classmethod
    def _signing_semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("payload_digest")
    @classmethod
    def _payload_digest(cls, value: str) -> str:
        CandidateDigest.from_tagged(value)
        return value


class ApprovalAttestation(ApprovalStatement):
    """One public Ed25519 approval envelope; it contains no private material."""

    sig: str

    @field_validator("sig")
    @classmethod
    def _sig(cls, value: str) -> str:
        if not _SIGNATURE_RE.fullmatch(value):
            raise ValueError("approval signature must be 64 bytes of lowercase Ed25519 hex")
        return value

    @property
    def statement(self) -> ApprovalStatement:
        return ApprovalStatement(
            signer_id=self.signer_id,
            signing_semantic_root=self.signing_semantic_root,
            payload_digest=self.payload_digest,
        )


class ApprovalSubmission(_StrictAttestationModel):
    """Authentication context recorded separately from cryptographic signer authority."""

    tag: str = "playbill-approval-submission-v1"
    submitted_by: str
    attestation: ApprovalAttestation

    @field_validator("tag")
    @classmethod
    def _tag(cls, value: str) -> str:
        if value != "playbill-approval-submission-v1":
            raise ValueError("approval submission tag is unsupported")
        return value

    @field_validator("submitted_by")
    @classmethod
    def _submitted_by(cls, value: str) -> str:
        return governance_identifier(value, label="authenticated approval submitter")


def approval_statement_bytes(statement: ApprovalStatement | ApprovalAttestation) -> bytes:
    """Return exactly the four-field canonical statement signed by the client."""

    unsigned = statement.statement if isinstance(statement, ApprovalAttestation) else statement
    return canonical_bytes(unsigned.model_dump(mode="json"))


def approval_digest(attestation: ApprovalAttestation) -> ApprovalDigest:
    payload = attestation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(ApprovalDigest, "playbill-attest-v1", payload)


@dataclass(frozen=True)
class VerifiedApproval:
    submission: ApprovalSubmission
    digest: ApprovalDigest
    signer_roles: tuple[PrincipalRole, ...]
    signer_key_digest: str
    signer_key_history_ref: str

    @property
    def signer_id(self) -> str:
        return self.submission.attestation.signer_id


def verify_approval(
    submission: ApprovalSubmission,
    *,
    candidate: SemanticCandidate,
    principals: PrincipalRegistrySnapshot,
    purpose: ApprovalPurpose = "ordinary-artifact",
) -> VerifiedApproval:
    """Verify one public attestation against the exact signing-root registry."""

    attestation = submission.attestation
    expected_digest = candidate_digest(candidate).tagged
    if principals.semantic_root != candidate.parent_semantic_root:
        raise ApprovalIntegrityError("principal registry does not name the candidate parent root")
    if attestation.signing_semantic_root != candidate.parent_semantic_root:
        raise ApprovalIntegrityError("approval signing root differs from the candidate parent")
    if attestation.payload_digest != expected_digest:
        raise ApprovalIntegrityError("approval payload differs from the complete candidate digest")
    try:
        principal = principals.require_active(attestation.signer_id)
    except PrincipalIntegrityError as exc:
        raise ApprovalIntegrityError(str(exc)) from exc
    if principal.authority_roles == ("daemon",):
        raise ApprovalIntegrityError("the daemon principal cannot provide client approval")
    if purpose == "ordinary-artifact" and principal.authority_roles == ("recovery",):
        raise ApprovalIntegrityError("recovery principals cannot approve ordinary artifacts")
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(principal.public_key))
        public_key.verify(
            bytes.fromhex(attestation.sig),
            approval_statement_bytes(attestation),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ApprovalIntegrityError("approval Ed25519 signature does not verify") from exc
    return VerifiedApproval(
        submission=submission,
        digest=approval_digest(attestation),
        signer_roles=principal.authority_roles,
        signer_key_digest=principal.public_key_digest,
        signer_key_history_ref=principals.key_history_reference(principal.principal_id),
    )


def verify_candidate_approvals(
    candidate: CandidateRecord,
    submissions: tuple[ApprovalSubmission, ...],
    *,
    principals: PrincipalRegistrySnapshot,
    purpose: ApprovalPurpose = "ordinary-artifact",
) -> tuple[VerifiedApproval, ...]:
    """Verify the exact role quorum with one signer usable for at most one slot."""

    ordered = tuple(
        sorted(
            submissions,
            key=lambda item: (
                item.attestation.signer_id.encode("utf-8"),
                item.attestation.payload_digest,
            ),
        )
    )
    if submissions != ordered:
        raise ApprovalIntegrityError("approval submissions must be canonically sorted")
    signer_ids = [submission.attestation.signer_id for submission in submissions]
    if len(signer_ids) != len(set(signer_ids)):
        raise ApprovalIntegrityError("a signer may submit only one approval per candidate")
    verified = tuple(
        verify_approval(
            submission,
            candidate=candidate.candidate,
            principals=principals,
            purpose=purpose,
        )
        for submission in submissions
    )

    slots = tuple(
        role
        for requirement in candidate.approval_requirements
        for role in (requirement.role,) * requirement.minimum_distinct_signers
    )
    eligible = {
        role: tuple(
            index for index, approval in enumerate(verified) if role in approval.signer_roles
        )
        for role in dict.fromkeys(slots)
    }

    # Find a maximum one-to-one slot/signer assignment with deterministic
    # augmenting paths. This preserves the independent-role semantics without
    # the exponential worst case of enumerating every possible assignment.
    slot_to_signer: list[int | None] = [None] * len(slots)
    signer_to_slot: list[int | None] = [None] * len(verified)

    def augment(root_slot: int) -> bool:
        queued_slots = [root_slot]
        seen_slots = {root_slot}
        seen_signers: set[int] = set()
        signer_predecessor: dict[int, int] = {}
        cursor = 0

        while cursor < len(queued_slots):
            slot = queued_slots[cursor]
            cursor += 1
            for signer in eligible.get(slots[slot], ()):
                if signer in seen_signers:
                    continue
                seen_signers.add(signer)
                signer_predecessor[signer] = slot
                matched_slot = signer_to_slot[signer]
                if matched_slot is None:
                    current_signer = signer
                    while True:
                        current_slot = signer_predecessor[current_signer]
                        displaced_signer = slot_to_signer[current_slot]
                        slot_to_signer[current_slot] = current_signer
                        signer_to_slot[current_signer] = current_slot
                        if displaced_signer is None:
                            return True
                        current_signer = displaced_signer
                if matched_slot not in seen_slots:
                    seen_slots.add(matched_slot)
                    queued_slots.append(matched_slot)
        return False

    if len(slots) > len(verified) or any(not augment(slot) for slot in range(len(slots))):
        raise ApprovalIntegrityError(
            "verified approvals do not satisfy every independent candidate role"
        )
    return verified


__all__ = [
    "ApprovalAttestation",
    "ApprovalStatement",
    "ApprovalSubmission",
    "ApprovalPurpose",
    "VerifiedApproval",
    "approval_digest",
    "approval_statement_bytes",
    "verify_approval",
    "verify_candidate_approvals",
]
