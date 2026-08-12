"""Frozen Ed25519 approval preimage, key-history, and quorum tests."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    ApprovalSubmission,
    approval_digest,
    approval_statement_bytes,
    verify_approval,
    verify_candidate_approvals,
)
from cruxible_core.playbill.candidates import (
    CandidateMemberEvidence,
    CandidateRecord,
    SemanticCandidate,
    candidate_digest,
)
from cruxible_core.playbill.errors import ApprovalIntegrityError
from cruxible_core.playbill.governance import ApprovalRequirement
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot
from cruxible_core.playbill.types import PrincipalRecord

ROOT = "sha256:" + "11" * 32


def _key(principal_id: str, roles: tuple[str, ...]):
    private = Ed25519PrivateKey.generate()
    record = PrincipalRecord(
        principal_id=principal_id,
        public_key=private.public_key().public_bytes_raw().hex(),
        authority_roles=roles,
    )
    return private, record


def _candidate(*, parent: str = ROOT) -> CandidateRecord:
    semantic = SemanticCandidate(
        parent_semantic_root=parent,
        candidate_manifest_root="sha256:" + "22" * 32,
        semantic_diff_digest="sha256:" + "33" * 32,
        scope=("documents/design.yaml",),
        timestamp="2026-08-12T12:00:00.000000Z",
    )
    return CandidateRecord(
        candidate=semantic,
        candidate_digest=candidate_digest(semantic).tagged,
        required_tier="graph_write",
        approval_requirements=(
            ApprovalRequirement(role="owner"),
            ApprovalRequirement(role="reviewer"),
        ),
        activation_policy="snapshot",
        closure_paths=semantic.scope,
        members=(
            CandidateMemberEvidence(
                path=semantic.scope[0],
                artifact_kind="document",
                disposition="replacement",
                law_identifier="playbill.document.v1",
            ),
        ),
        law_digests={"playbill.document.v1": "sha256:" + "44" * 32},
        compiler_digest="sha256:" + "55" * 32,
    )


def _submission(
    private: Ed25519PrivateKey,
    candidate: CandidateRecord,
    *,
    signer_id: str,
    submitted_by: str = "api-client",
) -> ApprovalSubmission:
    statement = ApprovalStatement(
        signer_id=signer_id,
        signing_semantic_root=candidate.candidate.parent_semantic_root,
        payload_digest=candidate.candidate_digest,
    )
    signature = private.sign(approval_statement_bytes(statement)).hex()
    return ApprovalSubmission(
        submitted_by=submitted_by,
        attestation=ApprovalAttestation(**statement.model_dump(), sig=signature),
    )


def _registry(*records: PrincipalRecord) -> PrincipalRegistrySnapshot:
    daemon_private, daemon = _key("daemon", ("daemon",))
    del daemon_private
    return PrincipalRegistrySnapshot(
        semantic_root=ROOT,
        principals=tuple(sorted((daemon, *records), key=lambda item: item.principal_id)),
    )


def test_exact_public_attestation_verifies_and_keeps_submitter_separate() -> None:
    private, owner = _key("owner", ("owner",))
    candidate = _candidate()
    submission = _submission(private, candidate, signer_id="owner", submitted_by="relay")

    verified = verify_approval(
        submission,
        candidate=candidate.candidate,
        principals=_registry(owner),
    )

    assert verified.signer_id == "owner"
    assert verified.submission.submitted_by == "relay"
    assert verified.digest == approval_digest(submission.attestation)
    assert verified.signer_key_history_ref == f"principals/owner.yaml@{ROOT}"
    assert approval_statement_bytes(submission.attestation) == approval_statement_bytes(
        submission.attestation.statement
    )


def test_tampered_stale_foreign_revoked_and_recovery_approvals_refuse() -> None:
    private, owner = _key("owner", ("owner",))
    candidate = _candidate()
    valid = _submission(private, candidate, signer_id="owner")

    bad_sig = valid.model_copy(
        update={"attestation": valid.attestation.model_copy(update={"sig": "00" * 64})}
    )
    with pytest.raises(ApprovalIntegrityError, match="does not verify"):
        verify_approval(bad_sig, candidate=candidate.candidate, principals=_registry(owner))

    rebased = _candidate(parent="sha256:" + "99" * 32)
    with pytest.raises(ApprovalIntegrityError, match="registry"):
        verify_approval(valid, candidate=rebased.candidate, principals=_registry(owner))

    foreign_private, _foreign = _key("foreign", ("owner",))
    foreign = _submission(foreign_private, candidate, signer_id="foreign")
    with pytest.raises(ApprovalIntegrityError, match="absent"):
        verify_approval(foreign, candidate=candidate.candidate, principals=_registry(owner))

    revoked = owner.model_copy(update={"status": "revoked"})
    with pytest.raises(ApprovalIntegrityError, match="not active"):
        verify_approval(valid, candidate=candidate.candidate, principals=_registry(revoked))

    recovery_private, recovery = _key("recovery", ("recovery",))
    recovery_submission = _submission(recovery_private, candidate, signer_id="recovery")
    with pytest.raises(ApprovalIntegrityError, match="cannot approve"):
        verify_approval(
            recovery_submission,
            candidate=candidate.candidate,
            principals=_registry(recovery),
        )


def test_distinct_signers_must_fill_independent_roles() -> None:
    both_private, both = _key("both", ("owner", "reviewer"))
    reviewer_private, reviewer = _key("reviewer", ("reviewer",))
    candidate = _candidate()
    one = (_submission(both_private, candidate, signer_id="both"),)
    with pytest.raises(ApprovalIntegrityError, match="independent"):
        verify_candidate_approvals(candidate, one, principals=_registry(both))

    two = tuple(
        sorted(
            (
                one[0],
                _submission(reviewer_private, candidate, signer_id="reviewer"),
            ),
            key=lambda item: item.attestation.signer_id,
        )
    )
    verified = verify_candidate_approvals(
        candidate,
        two,
        principals=_registry(both, reviewer),
    )
    assert {item.signer_id for item in verified} == {"both", "reviewer"}


def test_principal_role_model_prevents_recovery_or_daemon_authority_expansion() -> None:
    with pytest.raises(ValueError, match="recovery authority"):
        _key("recovery", ("owner", "recovery"))
    with pytest.raises(ValueError, match="daemon authority"):
        _key("daemon", ("daemon", "owner"))
