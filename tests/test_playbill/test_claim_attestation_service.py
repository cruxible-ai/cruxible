from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationStatementV2,
)
from cruxible_client.contracts.claims import (
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.signing import LocalEd25519ClaimAttestationSigner
from cruxible_core.service.playbill_claim_attestations import (
    ClaimAttestationRefusal,
    service_append_claim_attestation,
)
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world

RECORDED_AT = datetime(2026, 8, 28, 15, tzinfo=UTC)


def _request(instance, owner, claim_id: str, root: Path):  # type: ignore[no-untyped-def]
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    tree = instance.tree_at(coordinate.git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    subject_content = tree[claim.statement.subject.artifact_path]
    object_shell_digest = None
    if isinstance(claim.statement.object, SubjectClaimObject):
        object_path = claim.statement.object.address.artifact_path
        object_shell_digest = subject_digest(
            parse_subject(tree[object_path], path=object_path)
        ).tagged
    evidence_captures = tuple(
        sorted(
            {
                citation.capture_digest
                for citation in claim.backing.citations
                if citation.role == "evidence"
            },
            key=lambda item: item.encode("ascii"),
        )
    )
    statement = ClaimAttestationStatementV2(
        instance_id=instance.descriptor.instance_id,
        referent_coordinate=coordinate,
        claim_identity=claim.identity,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        subject_shell_digest=subject_digest(
            parse_subject(subject_content, path=claim.statement.subject.artifact_path)
        ).tagged,
        object_shell_digest=object_shell_digest,
        attesting_principal_id=owner.principal.principal_id,
        signing_key_digest=owner.principal.public_key_digest,
        attestation_basis="examined_existing",
        stance="support",
        cited_capture_digests=evidence_captures,
        attested_at=RECORDED_AT,
    )
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer=owner.principal.principal_id,
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(root / "workspace", instance.root),
    )
    return ClaimAttestationAppendRequestV1(
        attestation=signer.sign_claim_attestation_v2(statement),
    )


def test_served_append_verifies_and_duplicate_is_an_identical_read(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    first = service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    retry = service_append_claim_attestation(
        instance,
        request=request.model_copy(update={"note": "ignored on duplicate"}),
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )

    assert retry == first
    assert len(instance.claim_attestation_evidence_store().events()) == 1
    assert first.recorded_head == first.current_head


def test_served_append_refuses_actor_relay_before_store_disclosure(tmp_path: Path) -> None:
    instance, claim_id, owner = _accepted_claim_world(tmp_path)
    request = _request(instance, owner, claim_id, tmp_path)

    with pytest.raises(ClaimAttestationRefusal) as error:
        service_append_claim_attestation(
            instance,
            request=request,
            actor_id="reviewer",
            recorded_at=RECORDED_AT,
        )

    assert error.value.error_code == "playbill.claim_attestation.actor_signer_mismatch"
