"""Atomic one-Claim dependency closure for SDK cold-path authoring."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.authoring.models import (
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    authoring_payload_digest,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_path,
)
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claims import claim_path
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.proposals.proposals import AuthenticatedActor
from tests.core_support._claim_authoring_support import create_subjects
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_preflight import TIMESTAMP, _self_source_payload
from tests.test_claims.test_claims import _claim_type, _subject


def _dependency_claim_type():
    return _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="coordinator-source",
                        claim_roles=("normative", "observation"),
                        capture_contracts=(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name,),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _payload(*, subject: bool = True, claim_type: bool = True) -> ClaimAuthoringPayload:
    base = _self_source_payload()
    return ClaimAuthoringPayload.model_validate(
        {
            **base.model_dump(mode="json"),
            "tag": "cruxible-claim-authoring-payload-v4",
            "dependency_drafts": ClaimDependencyDrafts(
                subject=_subject() if subject else None,
                claim_type=_dependency_claim_type() if claim_type else None,
            ).model_dump(mode="json"),
        }
    )


def test_cold_claim_dependency_closure_is_one_candidate(tmp_path: Path) -> None:
    """A Claim drafts its ClaimType; its Subject draft restates an accepted Subject.

    Core names every new Subject (D1), so a draft never creates one: the work
    item is accepted first, and the draft equal to it installs nothing.
    """

    instance, _owner = initialize_local(tmp_path)
    create_subjects(instance, (_subject(),), timestamp=TIMESTAMP)
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
    # The self-source capture contract came in with the work item's key Claim.
    accepted = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name) in accepted
    assert {
        claim_type_path(_dependency_claim_type().predicate),
        claim_path(intent.semantic_identity),
    } == changed
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    assert submitted.status.candidate_digest is not None


def test_an_empty_dependency_closure_has_one_spelling() -> None:
    """Stating no drafts and stating empty drafts are one payload, one identity."""

    bare = _self_source_payload()
    drafted = ClaimAuthoringPayload.model_validate(
        {
            **bare.model_dump(mode="json"),
            "dependency_drafts": ClaimDependencyDrafts().model_dump(mode="json"),
        }
    )

    assert "dependency_drafts" not in drafted.model_dump(mode="json")
    assert authoring_payload_digest(bare) == authoring_payload_digest(drafted)


def test_missing_cold_dependencies_refuse_with_specific_repairs(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    subject_missing = coordinator.create(
        actor=actor,
        payload=_payload(subject=False),
        canonical_timestamp=TIMESTAMP,
    ).intent
    subject_drafted = coordinator.create(
        actor=actor,
        payload=_payload(),
        canonical_timestamp=TIMESTAMP,
    ).intent

    first = coordinator.preflight(subject_missing.intent_id, actor=actor)
    drafted = coordinator.preflight(subject_drafted.intent_id, actor=actor)
    assert first.frontier.diagnostics[0].code == ("cruxible.authoring.dependency_subject_required")
    assert '"kind": "subject"' in first.frontier.diagnostics[0].repairs[0].description
    assert drafted.frontier.diagnostics[0].code == "cruxible.authoring.subject_id_not_assigned"

    create_subjects(instance, (_subject(),), timestamp=TIMESTAMP)
    claim_type_missing = coordinator.create(
        actor=actor,
        payload=_payload(claim_type=False),
        canonical_timestamp=TIMESTAMP,
    ).intent
    second = coordinator.preflight(claim_type_missing.intent_id, actor=actor)
    assert second.frontier.diagnostics[0].code == (
        "cruxible.authoring.dependency_claim_type_required"
    )
