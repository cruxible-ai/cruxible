"""PC-A2 closed Claim policy formats and evaluator parity tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.policies import (
    ActorRequirementV1,
    AdmissionActorV1,
    ClaimAdmissionCandidateContextV1,
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
    EvidenceAdmissionInputV1,
    EvidenceRequirementV1,
    FreezeRequirementV1,
    QueryEvidenceResultV1,
    ResolutionContenderV1,
    TransitionRequirementV1,
    VerifiedPolicySignerV1,
    evaluate_claim_admission_candidate,
    evaluate_claim_admission_settlement,
    evaluate_claim_evidence_admission,
    evaluate_claim_evidence_admission_trace,
    resolve_claim_contenders,
)

DIGEST_A = "sha256:" + "11" * 32
DIGEST_B = "sha256:" + "22" * 32


def _review_policy() -> ClaimAdmissionPolicyV1:
    return ClaimAdmissionPolicyV1(
        transition_requirements=(
            TransitionRequirementV1(
                requirement_id="approve-transition",
                when_predicate="review.status",
                from_values=("changes_requested", "open"),
                to_value="approved",
                require=("one-valid-approval", "reviewer-role"),
            ),
        ),
        actor_requirements=(
            ActorRequirementV1(
                requirement_id="reviewer-role",
                signer_roles=("reviewer",),
                signer_distinct_from_lineage_creation_actor=True,
            ),
        ),
        evidence_requirements=(
            EvidenceRequirementV1(
                requirement_id="one-valid-approval",
                query_definition_digest=DIGEST_A,
                min_count=1,
            ),
        ),
        freeze_requirements=(
            FreezeRequirementV1(
                requirement_id="approved-review-freeze",
                while_predicate="review.status",
                while_values=("approved",),
                frozen_predicates=("review.change_head", "review.summary"),
            ),
        ),
    )


def _context(
    *,
    parent_status: str = "open",
    candidate_status: str = "approved",
    truncated: bool = False,
) -> ClaimAdmissionCandidateContextV1:
    return ClaimAdmissionCandidateContextV1(
        evaluation_time="2026-08-16T12:00:00.000000Z",
        declared_predicates=("review.change_head", "review.status", "review.summary"),
        parent_values={
            "review.change_head": ("abc",),
            "review.status": (parent_status,),
            "review.summary": ("summary",),
        },
        candidate_values={
            "review.change_head": ("abc",),
            "review.status": (candidate_status,),
            "review.summary": ("summary",),
        },
        admission_actor=AdmissionActorV1(actor_id="owner", roles=("owner",)),
        lineage_creation_actor_id="owner",
        query_results=(
            QueryEvidenceResultV1(
                requirement_id="one-valid-approval",
                query_definition_digest=DIGEST_A,
                result_digest=DIGEST_B,
                matching_count=1,
                truncated=truncated,
            ),
        ),
    )


def test_review_request_policy_uses_parent_query_then_two_phase_signer_law() -> None:
    candidate = evaluate_claim_admission_candidate(_review_policy(), _context())

    assert candidate.verdict == "eligible"
    assert candidate.triggered_transitions == ("approve-transition",)
    assert tuple(item.requirement_id for item in candidate.required_signers) == ("reviewer-role",)

    accepted = evaluate_claim_admission_settlement(
        candidate,
        (
            VerifiedPolicySignerV1(
                signer_id="reviewer",
                roles=("reviewer",),
            ),
        ),
        lineage_creation_actor_id="owner",
    )
    assert accepted.verdict == "satisfied"

    creator_signing = evaluate_claim_admission_settlement(
        candidate,
        (VerifiedPolicySignerV1(signer_id="owner", roles=("reviewer",)),),
        lineage_creation_actor_id="owner",
    )
    assert creator_signing.verdict == "refused"
    assert creator_signing.refusal_codes == ("playbill.claim_policy.signer_constraint_unsatisfied",)

    substituted_lineage = evaluate_claim_admission_settlement(
        candidate,
        (VerifiedPolicySignerV1(signer_id="owner", roles=("reviewer",)),),
        lineage_creation_actor_id="someone-else",
    )
    assert substituted_lineage.refusal_codes == (
        "playbill.claim_policy.lineage_creation_actor_mismatch",
    )


def test_admission_refuses_truncated_query_freeze_bypass_and_unknown_predicate() -> None:
    truncated = evaluate_claim_admission_candidate(
        _review_policy(),
        _context(truncated=True),
    )
    assert truncated.refusal_codes == ("playbill.claim_policy.evidence_query_truncated",)

    frozen_context = _context(parent_status="approved", candidate_status="approved")
    frozen_context = frozen_context.model_copy(
        update={
            "candidate_values": {
                **frozen_context.candidate_values,
                "review.change_head": ("different",),
            }
        }
    )
    frozen = evaluate_claim_admission_candidate(_review_policy(), frozen_context)
    assert frozen.refusal_codes == ("playbill.claim_policy.freeze_active",)

    unknown = frozen_context.model_copy(update={"declared_predicates": ("review.status",)})
    refused = evaluate_claim_admission_candidate(_review_policy(), unknown)
    assert "playbill.claim_policy.unknown_predicate" in refused.refusal_codes


def _evidence_policy() -> ClaimEvidenceAdmissionPolicyV1:
    return ClaimEvidenceAdmissionPolicyV1(
        rules=(
            ClaimEvidenceAdmissionRuleV1(
                rule_id="direct-provider-observation",
                claim_roles=("observation",),
                capture_contract_digests=(DIGEST_A,),
                evidence_kinds=("source.observation",),
                admission="direct",
                subject_binding="contract_source_mapping",
                attestation_requirement="verified_provider",
            ),
        )
    )


def _evidence(**updates: object) -> EvidenceAdmissionInputV1:
    values: dict[str, object] = {
        "claim_role": "observation",
        "capture_contract_digest": DIGEST_A,
        "evidence_kind": "source.observation",
        "attestation_grade": "verified_provider",
        "source_subject_bound": True,
    }
    values.update(updates)
    return EvidenceAdmissionInputV1.model_validate(values)


def test_evidence_admission_is_conjunctive_and_never_grants_claim_authority() -> None:
    accepted = evaluate_claim_evidence_admission(_evidence_policy(), _evidence())
    assert accepted.verdict == "eligible"
    assert accepted.rule_id == "direct-provider-observation"

    undeclared = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(evidence_kind="source.other"),
    )
    assert undeclared.refusal_code == "playbill.evidence.undeclared_contract_kind"

    reducer = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(reducer_digest=DIGEST_B),
    )
    assert reducer.refusal_code == "playbill.evidence.reducer_not_allowed"

    laundering = evaluate_claim_evidence_admission(
        _evidence_policy(),
        _evidence(capture_claims_semantic_authority=True),
    )
    assert laundering.refusal_code == ("playbill.evidence.capture_cannot_grant_semantic_authority")


def test_evidence_admission_trace_chooses_the_closest_contract_rule_deterministically() -> None:
    direct = _evidence_policy().rules[0]
    alternative = direct.model_copy(
        update={
            "rule_id": "alternate-role",
            "claim_roles": ("derivation",),
            "evidence_kinds": ("source.other",),
            "attestation_requirement": "none",
        }
    )
    policy = ClaimEvidenceAdmissionPolicyV1(
        rules=tuple(sorted((alternative, direct), key=lambda item: item.rule_id.encode("utf-8")))
    )

    trace = evaluate_claim_evidence_admission_trace(
        policy,
        _evidence(attestation_grade="none"),
        subject_binding_by_rule={
            "alternate-role": True,
            "direct-provider-observation": True,
        },
    )

    assert trace.result.refusal_code == "playbill.evidence.attestation_grade_missing"
    assert trace.closest_rule_id == "direct-provider-observation"

    no_contract = evaluate_claim_evidence_admission_trace(
        policy,
        _evidence(capture_contract_digest=DIGEST_B),
    )
    assert no_contract.closest_rule_id is None


def test_evidence_policy_refuses_duplicate_rules_and_illegal_reducer_shapes() -> None:
    direct = _evidence_policy().rules[0]
    with pytest.raises(ValidationError, match="sorted and unique"):
        ClaimEvidenceAdmissionPolicyV1(rules=(direct, direct))
    with pytest.raises(ValidationError, match="only derivational"):
        ClaimEvidenceAdmissionRuleV1(
            **direct.model_dump(exclude={"allowed_reducer_digests"}),
            allowed_reducer_digests=(DIGEST_B,),
        )
    with pytest.raises(ValidationError, match="requires at least one"):
        ClaimEvidenceAdmissionRuleV1(
            **direct.model_dump(exclude={"admission"}),
            admission="derivational",
        )


def test_resolution_preserves_conflict_and_requires_exact_authority_proof() -> None:
    policy = ClaimResolutionPolicyV1(
        cardinality="one",
        eligible_verdicts=("supported",),
        selector="only_contender",
    )
    first = ResolutionContenderV1(
        claim_identity="CLM-" + "0a" * 16,
        object_value="open",
        verdict="supported",
    )
    second = ResolutionContenderV1(
        claim_identity="CLM-" + "0b" * 16,
        object_value="closed",
        verdict="supported",
    )
    assert resolve_claim_contenders(policy, (first,)).status == "resolved"
    conflict = resolve_claim_contenders(policy, (first, second))
    assert conflict.status == "unresolved"
    assert set(conflict.contender_claim_identities) == {
        "CLM-" + "0a" * 16,
        "CLM-" + "0b" * 16,
    }


def test_unknown_policy_requirement_tag_refuses_fail_closed() -> None:
    payload = _review_policy().model_dump(mode="json")
    payload["actor_requirements"][0]["tag"] = "playbill-actor-requirement-v2"
    with pytest.raises(ValidationError):
        ClaimAdmissionPolicyV1.model_validate(payload)


def test_requirement_ids_cannot_alias_across_policy_kinds() -> None:
    with pytest.raises(ValidationError, match="unique across"):
        ClaimAdmissionPolicyV1(
            actor_requirements=(
                ActorRequirementV1(
                    requirement_id="same-id",
                    signer_roles=("reviewer",),
                ),
            ),
            evidence_requirements=(
                EvidenceRequirementV1(
                    requirement_id="same-id",
                    query_definition_digest=DIGEST_A,
                    min_count=1,
                ),
            ),
        )
