"""Atomic one-Claim dependency closure for SDK cold-path authoring."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayloadV2,
    ClaimDependencyDraftsV1,
    authoring_payload_digest,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    capture_contract_path,
)
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.subjects import subject_path
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.authoring.preflight import compute_preflight
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _self_source_payload
from tests.test_playbill.test_claims import _claim_type, _subject


def _dependency_claim_type():
    contract_digest = capture_contract_digest(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).tagged
    return _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="coordinator-source",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(contract_digest,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _payload(*, subject: bool = True, claim_type: bool = True) -> ClaimAuthoringPayloadV2:
    base = _self_source_payload()
    return ClaimAuthoringPayloadV2.model_validate(
        {
            **base.model_dump(mode="json"),
            "tag": "playbill-claim-authoring-payload-v2",
            "dependency_drafts": ClaimDependencyDraftsV1(
                subject=_subject() if subject else None,
                claim_type=_dependency_claim_type() if claim_type else None,
            ).model_dump(mode="json"),
        }
    )


def test_cold_claim_dependency_closure_is_one_candidate(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    payload = _payload()
    intent = coordinator.create(
        actor=actor,
        payload=payload,
        canonical_timestamp=TIMESTAMP,
    ).intent

    computed = compute_preflight(instance, intent=intent, actor=actor)

    assert computed.result.verdict == "passed"
    assert computed.lowered is not None
    changed = {path for path, _content in computed.lowered.changed_members}
    assert {
        subject_path(_subject().subject_kind, _subject().subject_id),
        claim_type_path(_dependency_claim_type().predicate),
        claim_path(intent.semantic_identity),
        capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name),
    } == changed
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None


def test_empty_v2_dependency_closure_is_identity_distinct_from_v1() -> None:
    v1 = _self_source_payload()
    v2 = ClaimAuthoringPayloadV2.model_validate(
        {
            **v1.model_dump(mode="json"),
            "tag": "playbill-claim-authoring-payload-v2",
            "dependency_drafts": ClaimDependencyDraftsV1().model_dump(mode="json"),
        }
    )

    assert authoring_payload_digest(v1) != authoring_payload_digest(v2)


def test_missing_cold_dependencies_refuse_with_specific_repairs(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    subject_missing = coordinator.create(
        actor=actor,
        payload=_payload(subject=False),
        canonical_timestamp=TIMESTAMP,
    ).intent
    claim_type_missing = coordinator.create(
        actor=actor,
        payload=_payload(claim_type=False),
        canonical_timestamp=TIMESTAMP,
    ).intent

    first = coordinator.preflight(subject_missing.intent_id, actor=actor)
    second = coordinator.preflight(claim_type_missing.intent_id, actor=actor)

    assert first.frontier.diagnostics[0].code == ("playbill.authoring.dependency_subject_required")
    assert second.frontier.diagnostics[0].code == (
        "playbill.authoring.dependency_claim_type_required"
    )
