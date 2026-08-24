from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.signing import LocalEd25519ClaimAttestationSigner
from cruxible_core.service.playbill_claims import (
    service_explain_playbill_claim,
    service_get_playbill_claim,
    service_propose_playbill_claim,
)
from cruxible_core.service.playbill_evidence import (
    service_evaluate_playbill_claim_verdict,
    service_prepare_claim_attestation,
    service_propose_claim_attestation,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_direct_claim_authoring import (
    TIMESTAMP,
    _activate_direct_claim,
    _authoring,
)


def _signer(instance, owner, tmp_path: Path) -> LocalEd25519ClaimAttestationSigner:
    return LocalEd25519ClaimAttestationSigner.open(
        signer="Principal:owner",
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(tmp_path / "workspace", instance.root),
    )


def test_exact_statement_attestations_compound_across_backing_successors(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    initial = service_propose_playbill_claim(
        instance,
        authoring=_authoring(),
        actor_id="owner",
        proposal_name="evidence-initial",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, initial)
    initial_coordinate = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    before = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=initial.claim_identity,
        evaluation_time=datetime(2026, 8, 16, 20, 0, 30, tzinfo=UTC),
    )
    assert before.verdict.verdict == "supported"

    prepared = service_prepare_claim_attestation(
        instance,
        claim_identity=initial.claim_identity,
        stance="contradict",
        signer=ArtifactIdentity(kind="Principal", name="owner"),
        signing_key_id=owner.principal.public_key_digest,
        capture_digests=(initial.capture_digest,),
        observed_at=datetime(2026, 8, 16, 20, 1, tzinfo=UTC),
    )
    attestation = _signer(instance, owner, tmp_path).sign_claim_attestation(prepared.statement)
    contradicted = service_propose_claim_attestation(
        instance,
        claim_identity=initial.claim_identity,
        attestation=attestation,
        actor_id="owner",
        proposal_name="evidence-contradict",
        timestamp="2026-08-16T20:01:00.000000Z",
    )
    assert contradicted.proposal.proposal.candidate is not None
    _activate_direct_claim(instance, owner, contradicted, sequence=2)

    current = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=initial.claim_identity,
        evaluation_time=datetime(2026, 8, 16, 20, 1, 30, tzinfo=UTC),
    )
    assert current.verdict.verdict == "contradicted"
    assert current.verdict.contradicting_evidence_digests == (contradicted.attestation_digest,)
    historical = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=initial.claim_identity,
        evaluation_time=datetime(2026, 8, 16, 20, 0, 30, tzinfo=UTC),
        at=initial_coordinate,
    )
    assert historical.verdict.verdict == "supported"

    unsure_prepared = service_prepare_claim_attestation(
        instance,
        claim_identity=initial.claim_identity,
        stance="unsure",
        signer=ArtifactIdentity(kind="Principal", name="owner"),
        signing_key_id=owner.principal.public_key_digest,
        capture_digests=(),
        observed_at=datetime(2026, 8, 16, 20, 2, tzinfo=UTC),
    )
    unsure = service_propose_claim_attestation(
        instance,
        claim_identity=initial.claim_identity,
        attestation=_signer(instance, owner, tmp_path).sign_claim_attestation(
            unsure_prepared.statement
        ),
        actor_id="owner",
        proposal_name="evidence-unsure",
        timestamp="2026-08-16T20:02:00.000000Z",
    )
    assert unsure.proposal.proposal.candidate is not None
    _activate_direct_claim(instance, owner, unsure, sequence=3)
    compounded = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=initial.claim_identity,
        evaluation_time=datetime(2026, 8, 16, 20, 2, 30, tzinfo=UTC),
    )
    assert compounded.verdict.verdict == "contradicted"
    assert set(compounded.verdict.unsure_evidence_digests) == {unsure.attestation_digest}

    explanation = service_explain_playbill_claim(
        instance,
        identity=initial.claim_identity,
    )
    assert explanation.law_evidence.verified_attestation_digests == tuple(
        sorted((contradicted.attestation_digest, unsure.attestation_digest))
    )
    view = service_get_playbill_claim(instance, identity=initial.claim_identity)
    facts = {str(item["schema_id"]): item["value"] for item in view.facts}
    assert facts["playbill.claim.current_verdict"]["verdict"] == "contradicted"
    exact = facts["playbill.claim.attestation_coverage"]["claim_attestations"]
    assert {item["coverage"] for item in exact} == {"exact_subject"}
