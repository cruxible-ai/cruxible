from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationError,
    ClaimAttestationStatement,
    claim_attestation_statement_bytes,
    read_claim_attestation,
    store_claim_attestation,
    verify_claim_attestation,
)
from cruxible_core.playbill.claims import (
    AcceptedClaim,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
)
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.providers import ProviderSigningKeyV1
from cruxible_core.playbill.signing import LocalEd25519ClaimAttestationSigner
from cruxible_core.playbill.subjects import subject_digest
from tests.test_playbill._pc_c_support import NOW, capture_contract, provider
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claims import OBSERVED_AT, _claim, _subject


def _accepted_claim(instance, claim_id: str = "CLM-0123456789abcdef0123456789abcdef"):
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    from cruxible_core.playbill.captures import build_direct_claim_capture

    capture = build_direct_claim_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        value="ready",
        rationale="The work item is ready for review.",
        observed_at=OBSERVED_AT,
        accepted_coordinate=coordinate,
    )
    assert capture.envelope.commitment.byte_length is not None
    claim = _claim(
        claim_id=claim_id,
        capture_digest=capture.capture_digest,
        source_digest=capture.source_body_digest,
        source_length=capture.envelope.commitment.byte_length,
    )
    accepted = AcceptedClaim(
        path=claim_path(claim_id),
        claim=claim,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
    )
    return coordinate, capture, accepted


def _principals(instance, coordinate: AcceptedCoordinate) -> PrincipalRegistrySnapshot:
    return PrincipalRegistrySnapshot(
        semantic_root=coordinate.semantic_root,
        principals=instance.trust_root.principals,
    )


def test_client_held_principal_signs_exact_claim_and_cas_round_trips(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinate, capture, claim = _accepted_claim(instance)
    statement = ClaimAttestationStatement(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        subject=claim.claim.statement.subject,
        subject_content_digest=subject_digest(_subject()).tagged,
        claim_statement_digest=claim.statement_digest,
        stance="support",
        provider_or_principal=ArtifactIdentity(kind="Principal", name="owner"),
        signing_key_id=owner.principal.public_key_digest,
        capture_digests=(capture.capture_digest,),
        observed_at=NOW,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer="Principal:owner",
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(tmp_path / "workspace", instance.root),
    )
    attestation = signer.sign_claim_attestation(statement)
    digest = store_claim_attestation(attestation, store=instance.body_store())
    assert read_claim_attestation(digest, store=instance.body_store()) == attestation
    verified = verify_claim_attestation(
        attestation,
        verification_time=NOW,
        expected_instance_id=instance.descriptor.instance_id,
        expected_coordinate=coordinate,
        claim=claim,
        referent_subject_content_digest=subject_digest(_subject()).tagged,
        referent_object_content_digest=None,
        principals=_principals(instance, coordinate),
        providers={},
        store=instance.body_store(),
    )
    assert verified.attestation_grade == "verified_principal"
    assert verified.coverage == "exact_subject"

    future = signer.sign_claim_attestation(
        statement.model_copy(update={"observed_at": NOW + timedelta(seconds=1)})
    )
    with pytest.raises(ClaimAttestationError, match="observed_at is in the future"):
        verify_claim_attestation(
            future,
            verification_time=NOW,
            expected_instance_id=instance.descriptor.instance_id,
            expected_coordinate=coordinate,
            claim=claim,
            referent_subject_content_digest=subject_digest(_subject()).tagged,
            referent_object_content_digest=None,
            principals=_principals(instance, coordinate),
            providers={},
            store=instance.body_store(),
        )


def test_wrong_instance_tamper_and_missing_capture_fail_closed(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    coordinate, capture, claim = _accepted_claim(instance)
    statement = ClaimAttestationStatement(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        subject=claim.claim.statement.subject,
        subject_content_digest=subject_digest(_subject()).tagged,
        claim_statement_digest=claim.statement_digest,
        stance="contradict",
        provider_or_principal=ArtifactIdentity(kind="Principal", name="owner"),
        signing_key_id=owner.principal.public_key_digest,
        capture_digests=(capture.capture_digest,),
        observed_at=NOW,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer="Principal:owner",
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(tmp_path / "workspace", instance.root),
    )
    attestation = signer.sign_claim_attestation(statement)
    arguments = {
        "verification_time": NOW,
        "expected_instance_id": "other-instance",
        "expected_coordinate": coordinate,
        "claim": claim,
        "referent_subject_content_digest": subject_digest(_subject()).tagged,
        "referent_object_content_digest": None,
        "principals": _principals(instance, coordinate),
        "providers": {},
        "store": instance.body_store(),
    }
    with pytest.raises(ClaimAttestationError, match="different instance"):
        verify_claim_attestation(attestation, **arguments)
    arguments["expected_instance_id"] = instance.descriptor.instance_id
    tampered = attestation.model_copy(update={"signature": "00" * 64})
    with pytest.raises(ClaimAttestationError, match="signature does not verify"):
        verify_claim_attestation(tampered, **arguments)
    missing = attestation.model_copy(update={"capture_digests": ("sha256:" + "ee" * 32,)})
    with pytest.raises(ClaimAttestationError, match="Capture is unavailable"):
        verify_claim_attestation(missing, **arguments)


def test_provider_key_rotation_and_shell_drift_are_evidence_not_authority(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinate, capture, claim = _accepted_claim(instance)
    private_key = Ed25519PrivateKey.generate()
    contract = capture_contract()
    provider_artifact = provider(
        contract,
        public_key=private_key.public_key().public_bytes_raw().hex(),
    )
    statement = ClaimAttestationStatement(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        subject=claim.claim.statement.subject,
        subject_content_digest=subject_digest(_subject()).tagged,
        claim_statement_digest=claim.statement_digest,
        stance="support",
        provider_or_principal=provider_artifact.identity,
        signing_key_id="primary-2026",
        capture_digests=(capture.capture_digest,),
        observed_at=NOW,
    )
    attestation = ClaimAttestation(
        **statement.model_dump(mode="json"),
        signature=private_key.sign(claim_attestation_statement_bytes(statement)).hex(),
    )
    verified = verify_claim_attestation(
        attestation,
        verification_time=NOW,
        expected_instance_id=instance.descriptor.instance_id,
        expected_coordinate=coordinate,
        claim=claim,
        referent_subject_content_digest=subject_digest(_subject()).tagged,
        referent_object_content_digest=None,
        principals=_principals(instance, coordinate),
        providers={provider_artifact.identity.qualified: provider_artifact},
        store=instance.body_store(),
        current_subject_content_digest="sha256:" + "ab" * 32,
    )
    assert verified.attestation_grade == "verified_provider"
    assert verified.coverage == "shell_stale"
    revoked_key = ProviderSigningKeyV1.model_validate(
        {
            **provider_artifact.signing_keys[0].model_dump(mode="json"),
            "status": "revoked",
        }
    )
    revoked = provider_artifact.model_copy(update={"signing_keys": (revoked_key,)})
    with pytest.raises(ClaimAttestationError, match="expired, or revoked"):
        verify_claim_attestation(
            attestation,
            verification_time=NOW,
            expected_instance_id=instance.descriptor.instance_id,
            expected_coordinate=coordinate,
            claim=claim,
            referent_subject_content_digest=subject_digest(_subject()).tagged,
            referent_object_content_digest=None,
            principals=_principals(instance, coordinate),
            providers={revoked.identity.qualified: revoked},
            store=instance.body_store(),
        )
