"""PC-G5 ClaimType-v3 evidence freshness succession and service laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import (
    CanonicalDurationV1,
    DirectForeignSourceSelectionV1,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    ClaimType,
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultV2
from cruxible_client.contracts.claims import ClaimLawEvidenceV2
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_core.playbill.claim_type_migrations import (
    ClaimTypeDependentDispositionV1,
    ClaimTypeMigrationRequestV1,
    service_migrate_claim_type,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_claims import (
    PlaybillClaimExplanationV3,
    PlaybillClaimQueryResultV2,
    service_explain_playbill_claim,
    service_query_playbill_claims,
)
from cruxible_core.service.playbill_evidence import (
    PlaybillClaimVerdictQueryV2,
    service_evaluate_playbill_claim_verdict,
)
from cruxible_core.service.playbill_next import PlaybillNextRequestV1, service_playbill_next
from tests.test_playbill._claim_authoring_support import (
    TIMESTAMP,
    _activate_direct_claim,
    _authoring,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_preflight import _seed_claim_surface
from tests.test_playbill.test_claims import _claim_type


def _activate(instance, owner, proposal_id: str, candidate_digest: str) -> None:  # type: ignore[no-untyped-def]
    reviewer = client_material(instance.root.parent, instance)
    approval = _sign(reviewer, candidate_digest, instance.accepted_coordinate().semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=reviewer.principal.principal_id,
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"


def _fresh_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    source_id = "fixture.freshness"
    _seed_claim_surface(instance, owner, contract=foreign_source_capture_contract(source_id))
    body = instance.body_store().store(b"status: ready")
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_authoring().model_copy(
            update={
                "subject_shell": None,
                "claim_type_artifact": None,
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity=source_id,
                    span=ContentSpan(
                        content_digest=body.digest,
                        start_byte=0,
                        end_byte=len(b"status: ready"),
                    ),
                ),
            }
        ),
        actor_id="owner",
        proposal_name="freshness-initial",
        timestamp=TIMESTAMP,
    )
    _activate_direct_claim(instance, owner, proposed, sequence=len(instance.accepted_history()))

    path = claim_type_path(_claim_type().predicate)
    predecessor = parse_claim_type(
        instance.tree_at(instance.accepted_coordinate().git_oid)[path],
        path=path,
    )
    successor = ClaimType.model_validate(
        {
            **predecessor.model_dump(mode="json"),
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=10_000_000)
            ).model_dump(mode="json"),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(predecessor).tagged
            ).model_dump(mode="json"),
        }
    )
    migration = service_migrate_claim_type(
        instance,
        request=ClaimTypeMigrationRequestV1(
            successor=successor,
            dependents=(
                ClaimTypeDependentDispositionV1(
                    claim_id=proposed.claim_identity.removeprefix("Claim:"),
                    disposition="successor",
                ),
            ),
        ),
        actor=actor,
    )
    candidate = migration.proposal.proposal.candidate
    assert candidate is not None
    _activate(
        instance,
        owner,
        migration.proposal.proposal.admission.proposal_id,
        candidate.candidate_digest,
    )
    return instance, proposed.claim_identity.removeprefix("Claim:")


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="freshness-test",
        permitted_access_classes=("instance", "public"),
    )


def test_v3_freshness_succeeds_service_wires_and_next_queue(tmp_path: Path) -> None:
    instance, claim_id = _fresh_world(tmp_path)
    before_expiry = datetime(2026, 8, 16, 20, 0, 9, tzinfo=UTC)
    at_expiry = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)

    current = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=f"Claim:{claim_id}",
        evaluation_time=before_expiry,
    )
    expired = service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=f"Claim:{claim_id}",
        evaluation_time=at_expiry,
    )

    assert isinstance(current, PlaybillClaimVerdictQueryV2)
    assert isinstance(current.verdict, ClaimVerdictResultV2)
    assert current.verdict.verdict == "supported"
    assert isinstance(expired, PlaybillClaimVerdictQueryV2)
    assert expired.verdict.verdict == "stale_evidence"
    assert expired == service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=f"Claim:{claim_id}",
        evaluation_time=at_expiry,
    )

    queried = service_query_playbill_claims(
        instance,
        subject=SemanticAddress.whole_artifact("subjects/project.work_item/wi-42.yaml"),
        predicate=_claim_type().predicate,
        evaluation_time=at_expiry,
    )
    assert isinstance(queried, PlaybillClaimQueryResultV2)
    assert queried.verdicts[0].verdict == "stale_evidence"

    explanation = service_explain_playbill_claim(
        instance,
        identity=f"Claim:{claim_id}",
        evaluation_time=at_expiry,
    )
    assert isinstance(explanation, PlaybillClaimExplanationV3)
    assert isinstance(explanation.law_evidence, ClaimLawEvidenceV2)
    assert explanation.freshness[0].state == "expired"
    assert explanation.freshness[0].recapture_operation.operation == "playbill.authoring.bind"

    expiring = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=before_expiry,
            access_profile=_access(),
            expiring_within=CanonicalDurationV1(microseconds=2_000_000),
        ),
    )
    stale = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=at_expiry,
            access_profile=_access(),
        ),
    )
    assert [item.reason for item in expiring.items].count("evidence_expiring") == 1
    assert [item.reason for item in stale.items].count("claim_stale_evidence") == 1
    assert not any(item.reason == "evidence_expiring" for item in stale.items)
